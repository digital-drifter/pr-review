import fnmatch
import json
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple

from prreview.config import HOME, PROJECT_DIR, REPO, RUN_RETENTION_SECONDS, RUNS_DIR, SIZE_EXCLUDES, WORKTREES_DIR
from prreview.models import PullRequest

MEMORY_INDEX = str(HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(REPO)) / "memory" / "MEMORY.md")
RENDER_BIN = str(PROJECT_DIR / "bin" / "pr-review")


BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
RUN_DIR_NAME = re.compile(r"[0-9]+-[0-9a-f]{12}")
EXCLUDE_PATHSPECS = tuple(
    ":(exclude,glob)%s" % (pattern if "/" in pattern else "**/" + pattern) for pattern in SIZE_EXCLUDES
)
GIT_TIMEOUT = 120
FETCH_TIMEOUT = 300


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    pr: PullRequest
    mode: str
    base: str
    head: str
    full_fallback: bool
    worktree: Path
    run_dir: Path
    diff_command: str
    files_changed: int
    changed_lines: int
    commit_count: int


def _run_git(repo: Path, args: Sequence[str], timeout: int = GIT_TIMEOUT, **kwargs: Any) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(cmd, check=False, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise WorkspaceError("%s timed out after %ds" % (" ".join(cmd), timeout))


def _git(repo: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT) -> str:
    result = _run_git(repo, args, timeout, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise WorkspaceError("git -C %s %s failed (%d): %s" % (repo, " ".join(args), result.returncode, result.stderr.strip()))
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_ok(repo: Path, *args: str) -> bool:
    return _run_git(repo, args, capture_output=True, text=True).returncode == 0


def _git_to_file(repo: Path, path: Path, args: Sequence[str]) -> None:
    with open(str(path), "wb") as handle:
        result = _run_git(repo, args, stdout=handle, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise WorkspaceError("git -C %s %s failed (%d): %s" % (
            repo, " ".join(args), result.returncode, result.stderr.decode("utf-8", "replace").strip()))


def _validate_branch(repo: Path, name: str) -> None:
    if not BRANCH_PATTERN.match(name) or name.startswith("-") or not _git_ok(repo, "check-ref-format", "--branch", name):
        raise WorkspaceError("invalid branch name: %s" % name)


def _is_excluded(path: str) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in SIZE_EXCLUDES)


def _numstat(output: str) -> Tuple[int, int]:
    paths: Set[str] = set()
    lines = 0
    for row in output.splitlines():
        parts = row.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        paths.add(path)
        if _is_excluded(path):
            continue
        if added != "-" and deleted != "-":
            lines += int(added) + int(deleted)
    return len(paths), lines


def _remove_stale_worktree(repo: Path, worktree: Path) -> None:
    _run_git(repo, ["worktree", "remove", "--force", str(worktree)], capture_output=True, text=True)
    if worktree.exists():
        shutil.rmtree(str(worktree))
    _git(repo, "worktree", "prune")


SESSION_CONFIG_NAMES = (".claude", ".mcp.json")
FOLDED_SESSION_CONFIG_NAMES = frozenset(name.casefold() for name in SESSION_CONFIG_NAMES)


def _fold_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _is_session_config_name(name: str) -> bool:
    return _fold_name(name) in FOLDED_SESSION_CONFIG_NAMES


def _strip_session_config(worktree: Path) -> None:
    for root, dirnames, filenames in os.walk(str(worktree), topdown=True, followlinks=False):
        base = Path(root)
        for name in list(dirnames):
            if _fold_name(name) == ".git":
                dirnames.remove(name)
            elif _is_session_config_name(name):
                dirnames.remove(name)
                target = base / name
                if target.is_symlink():
                    target.unlink()
                else:
                    shutil.rmtree(str(target))
        for name in filenames:
            if _is_session_config_name(name):
                (base / name).unlink()


def prepare(
    pr: PullRequest,
    mode: str,
    base: Optional[str],
    repo: Path = REPO,
    runs_dir: Path = RUNS_DIR,
    worktrees_dir: Path = WORKTREES_DIR,
) -> Workspace:
    if mode not in ("full", "incremental"):
        raise ValueError("mode must be 'full' or 'incremental', got %r" % mode)
    src = pr.source_branch
    dest = pr.destination_branch
    _validate_branch(repo, src)
    _validate_branch(repo, dest)
    _git(
        repo, "fetch", "--no-write-fetch-head", "origin", "--",
        "+refs/heads/%s:refs/remotes/origin/%s" % (src, src),
        "+refs/heads/%s:refs/remotes/origin/%s" % (dest, dest),
        timeout=FETCH_TIMEOUT,
    )
    head = _git(repo, "rev-parse", "--verify", pr.head + "^{commit}")
    dest_sha = _git(repo, "rev-parse", "--verify", "refs/remotes/origin/%s^{commit}" % dest)

    worktree = worktrees_dir / str(pr.id)
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_worktree(repo, worktree)
    _git(
        repo, "-c", "core.symlinks=false", "-c", "core.hooksPath=/dev/null",
        "worktree", "add", "--detach", str(worktree), head,
    )

    try:
        _strip_session_config(worktree)
        run_dir = runs_dir / ("%d-%s" % (pr.id, pr.head))
        run_dir.mkdir(parents=True, exist_ok=True)
        os.utime(str(run_dir))

        full_base = ""
        full_fallback = False
        if mode == "incremental":
            resolved = _git(repo, "rev-parse", "--verify", (base or "") + "^{commit}", check=False) if base else ""
            if resolved and _git_ok(repo, "merge-base", "--is-ancestor", resolved, head):
                full_base = resolved
            else:
                full_fallback = True

        if full_base:
            final_mode = "incremental"
            range_spec = "%s..%s" % (full_base, head)
            diff_args = ["log", "--no-merges", "-p", "--reverse", range_spec, "^" + dest_sha]
            numstat = _git(
                repo, "-c", "core.quotepath=off", "log", "--no-merges", "--no-renames", "--numstat",
                "--format=", range_spec, "^" + dest_sha,
            )
            commit_range = [range_spec]
        else:
            final_mode = "full"
            full_base = _git(repo, "merge-base", dest_sha, head)
            diff_args = ["diff", full_base, head]
            numstat = _git(repo, "-c", "core.quotepath=off", "diff", "--no-renames", "--numstat", full_base, head)
            commit_range = [head]

        commit_count = int(_git(repo, "rev-list", "--count", "--no-merges", *commit_range, "^" + dest_sha))
        diff_args += ["--", *EXCLUDE_PATHSPECS]
        diff_command = "git " + " ".join(diff_args)
        _git_to_file(repo, run_dir / "diff.patch", [diff_args[0], "--no-color", "--no-ext-diff", *diff_args[1:]])
        files_changed, changed_lines = _numstat(numstat)
    except BaseException:
        _remove_stale_worktree(repo, worktree)
        raise

    return Workspace(
        pr=pr,
        mode=final_mode,
        base=full_base,
        head=head,
        full_fallback=full_fallback,
        worktree=worktree,
        run_dir=run_dir,
        diff_command=diff_command,
        files_changed=files_changed,
        changed_lines=changed_lines,
        commit_count=commit_count,
    )


def cleanup(ws: Workspace, repo: Path = REPO, worktrees_dir: Path = WORKTREES_DIR) -> None:
    worktree = ws.worktree.resolve()
    if worktree.parent != worktrees_dir.resolve() or worktree.name != str(int(ws.pr.id)):
        raise WorkspaceError("refusing to remove %s: not %s" % (ws.worktree, worktrees_dir / str(int(ws.pr.id))))
    _remove_stale_worktree(repo, ws.worktree)


def prune_runs(runs_dir: Path, now: float, max_age: float = RUN_RETENTION_SECONDS) -> List[str]:
    removed: List[str] = []
    try:
        entries = list(os.scandir(str(runs_dir)))
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not RUN_DIR_NAME.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime <= max_age:
                continue
            shutil.rmtree(entry.path)
        except OSError:
            continue
        removed.append(entry.name)
    return removed


def render_command(run_dir: Path) -> str:
    path = run_dir.resolve() if run_dir.is_absolute() else run_dir.absolute()
    return "/usr/bin/python3 -I %s render %s/context.json %s/review.json" % (RENDER_BIN, path, path)


def write_context(
    ws: Workspace,
    earlier_findings: List[dict],
    artifact_url: Optional[str],
    invoked_by: str,
) -> Path:
    if invoked_by not in ("watcher", "manual"):
        raise ValueError("invoked_by must be 'watcher' or 'manual', got %r" % invoked_by)
    context = {
        "pr": asdict(ws.pr),
        "mode": ws.mode,
        "full_fallback": ws.full_fallback,
        "base": ws.base,
        "head": ws.head,
        "worktree": str(ws.worktree),
        "run_dir": str(ws.run_dir),
        "diff_command": ws.diff_command,
        "diff_path": str(ws.run_dir / "diff.patch"),
        "files_changed": ws.files_changed,
        "changed_lines": ws.changed_lines,
        "commit_count": ws.commit_count,
        "earlier_findings": earlier_findings,
        "artifact_url": artifact_url,
        "invoked_by": invoked_by,
        "review_path": str(ws.run_dir / "review.json"),
        "memory_index": MEMORY_INDEX,
        "render_command": render_command(ws.run_dir),
    }
    path = ws.run_dir / "context.json"
    path.write_text(json.dumps(context, indent=2))
    return path

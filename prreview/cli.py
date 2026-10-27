import argparse
import json
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Callable, List, Optional, Tuple

from prreview.bitbucket import BitbucketAuthError, BitbucketTransientError
from prreview.keychain import SecretMissing
from prreview.models import PullRequest
from prreview.render import ReviewInvalid, apply_review, latest_counts, latest_fixed_count, load_page, open_findings, render_html
from prreview.runner import Deps, acquire_lock, finish, release_lock, run_one, seed, watch
from prreview.state import load_state, save_state
from prreview.workspace import Workspace, WorkspaceError, write_context

CLI_ERRORS = (BitbucketAuthError, BitbucketTransientError, SecretMissing, WorkspaceError, ValueError, OSError)
MANUAL_LOCK_WAIT = 60.0
BUSY = "A watcher review is running; try again in a few minutes."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr-review")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("watch")
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("--force", action="store_true")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("pr_id", type=int)
    run_parser.add_argument("--base")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("pr_id", type=int)
    render_parser = commands.add_parser("render")
    render_parser.add_argument("context", type=Path)
    render_parser.add_argument("review", type=Path)
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("context", type=Path)
    finish_parser.add_argument("--artifact-url", required=True)
    commands.add_parser("list")
    commands.add_parser("sync-dashboard")
    return parser


def run_dir_of(context_path: Path, runs_dir: Path) -> Path:
    run_dir = context_path.parent.resolve()
    runs = runs_dir.resolve()
    try:
        inside = len(run_dir.relative_to(runs).parts) == 1
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("%s is not a run directory under %s" % (run_dir, runs))
    return run_dir


def verified_run(context_path: Path, deps: Deps) -> Tuple[PullRequest, Path]:
    run_dir = run_dir_of(context_path, deps.runs_dir)
    try:
        doc = json.loads(context_path.read_text())["pr"]
        pr_id, head = int(doc["id"]), str(doc["head"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("%s is not a valid context file" % context_path)
    if run_dir.name != "%d-%s" % (pr_id, head):
        raise ValueError("run directory %s does not match PR #%d at %s" % (run_dir.name, pr_id, head))
    pr = deps.get_pr(pr_id)
    if pr.head != head:
        raise ValueError("PR #%d head is now %s, not %s; prepare it again" % (pr_id, pr.head, head))
    return pr, run_dir


def trusted_workspace(pr: PullRequest, run_dir: Path, deps: Deps) -> Workspace:
    return Workspace(
        pr=pr, mode="full", base="", head=pr.head, full_fallback=False,
        worktree=deps.worktrees_dir / str(int(pr.id)), run_dir=run_dir, diff_command="",
        files_changed=0, changed_lines=0, commit_count=0,
    )


def with_lock(deps: Deps, command: Callable[[], int]) -> int:
    lock = acquire_lock(deps.data_dir, int(deps.now().timestamp()), MANUAL_LOCK_WAIT)
    if lock is None:
        print(BUSY, file=sys.stderr)
        return 1
    try:
        return command()
    finally:
        release_lock(lock)


def cmd_list(deps: Deps) -> int:
    for pr in sorted(deps.list_prs(), key=lambda item: item.id):
        print("%d\t%s\t%s" % (pr.id, "draft" if pr.draft else "ready", pr.title))
    return 0


def cmd_prepare(pr_id: int, deps: Deps) -> int:
    pr = deps.get_pr(pr_id)
    entry = load_state(deps.data_dir / "state.json").prs.get(pr_id)
    reviewed = entry.reviewed_commit if entry else None
    if reviewed and reviewed != pr.head:
        ws = deps.prepare(pr, "incremental", reviewed)
    else:
        ws = deps.prepare(pr, "full", None)
    try:
        earlier = open_findings(load_page(pr_id, deps.data_dir))
        path = write_context(ws, earlier, entry.artifact_url if entry else None, "manual")
    except BaseException:
        deps.cleanup(ws)
        raise
    print(path)
    return 0


def cmd_render(context_path: Path, review_path: Path, deps: Deps) -> int:
    if context_path.name != "context.json" or review_path.name != "review.json":
        print("render takes a run directory's context.json and review.json", file=sys.stderr)
        return 2
    try:
        run_dir = run_dir_of(context_path, deps.runs_dir)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if review_path.resolve().parent != run_dir:
        print("review must be in %s" % run_dir, file=sys.stderr)
        return 2
    context = json.loads(context_path.read_text())
    try:
        review = json.loads(review_path.read_text())
    except (OSError, ValueError) as error:
        print("review is not valid JSON: %s" % error, file=sys.stderr)
        return 2
    try:
        page = apply_review(load_page(context["pr"]["id"], deps.data_dir), context, review, deps.now())
    except ReviewInvalid as error:
        print(str(error), file=sys.stderr)
        return 2
    (run_dir / "page.json").write_text(json.dumps(page, indent=2))
    html_path = run_dir / "page.html"
    html_path.write_text(render_html(page))
    print(html_path)
    return 0


def cmd_finish(context_path: Path, artifact_url: str, deps: Deps) -> int:
    pr, run_dir = verified_run(context_path, deps)
    state_path = deps.data_dir / "state.json"
    state = load_state(state_path)
    page = finish(pr, run_dir, artifact_url, state, deps.now(), deps.data_dir)
    save_state(state, state_path)
    deps.sync_dashboard()
    try:
        deps.cleanup(trusted_workspace(pr, run_dir, deps))
    except Exception as error:
        print("warning: worktree cleanup failed: %s" % error, file=sys.stderr)
    section = page["sections"][0]
    counts = latest_counts(page)
    line = "Critical %d · Should fix %d · Nit %d" % (counts.critical, counts.should_fix, counts.nit)
    if section["kind"] != "full":
        line += " · Fixed %d" % latest_fixed_count(page)
    print(section["one_line"])
    print(line)
    return 0


def _terminate(signum: int, frame: Optional[FrameType]) -> None:
    raise SystemExit(128 + signum)


def main(argv: Optional[List[str]] = None, deps: Optional[Deps] = None) -> int:
    args = build_parser().parse_args(argv)
    deps = deps or Deps()
    if args.command == "watch":
        signal.signal(signal.SIGTERM, _terminate)
        return watch(deps)
    if args.command == "run":
        signal.signal(signal.SIGTERM, _terminate)
        return run_one(args.pr_id, args.base, deps)
    if args.command == "seed":
        return seed(args.force, deps)
    if args.command == "sync-dashboard":
        deps.sync_dashboard()
        return 0
    try:
        if args.command == "prepare":
            return with_lock(deps, lambda: cmd_prepare(args.pr_id, deps))
        if args.command == "render":
            return cmd_render(args.context, args.review, deps)
        if args.command == "finish":
            return with_lock(deps, lambda: cmd_finish(args.context, args.artifact_url, deps))
        return cmd_list(deps)
    except CLI_ERRORS as error:
        print("error: %s" % error, file=sys.stderr)
        return 1

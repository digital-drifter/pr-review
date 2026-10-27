import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import IO, Any, Callable, Dict, List, Optional, Set, Tuple

from prreview import bitbucket, notify, workspace
from prreview.bitbucket import BitbucketAuthError, BitbucketTransientError
from prreview.config import (
    CLAUDE_BIN, DATA_DIR, HARDCOPY_INTERVAL, INSTALLED_PLUGINS, MAX_ATTEMPTS, MAX_UNCOUNTED_BLOCKS, READ_DENY,
    REPO_FULL_NAME, RUNS_DIR, STALL_SECONDS, LONG_RUN_SECONDS, DASHBOARD_LOG, DASHBOARD_TRIGGER_ID, USER_CLAUDE_MD, USER_SETTINGS, WORKTREES_DIR,
)
from prreview.decide import clear_failure, next_action, observe, record_failure
from prreview.keychain import SecretMissing
from prreview.logs import log
from prreview.models import PullRequest
from prreview.notify import (
    format_failure, format_fork, format_long_run, format_review_ready, format_update, is_artifact_url,
)
from prreview.render import latest_counts, latest_fixed_count, load_page, open_findings
from prreview.state import PrEntry, State, load_state, save_state
from prreview.workspace import (
    MEMORY_INDEX, RENDER_BIN, Workspace, WorkspaceError, prune_runs, render_command, write_context,
)

SCREEN = "/usr/bin/screen"
SESSION_ERROR = "the review session reported an error; see the log"
UNEXPECTED_URL = "published to an unexpected URL"
REASON_MAX = 300
HARDCOPY_WAIT = 3.0
HARDCOPY_SETTLE = 0.2
SCREEN_TIMEOUT = 15
LAUNCH_WAIT = 15.0
QUIT_WAIT = 15.0
TERMINATE_WAIT = 2.0
STUCK_GRACE = 900
STUCK_AFTER = 2 * STUCK_GRACE
SESSION_PREFIX = "pr-review-"
SESSION_SETTINGS = "session-settings.json"
GUARDED_TOOLS = "Artifact|Agent"
RESTRICT_ENV = "CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG"
STALE_OUTPUTS = ("result.json", "review.json", "page.json", "page.html", "screen.txt", SESSION_SETTINGS)
PLUGIN_SOURCES = ((USER_SETTINGS, "enabledPlugins"), (INSTALLED_PLUGINS, "plugins"))
DENY_TOOLS = ("Read",)
STALL_REASON = "the review session stalled"
LOGIN_REASON = "Claude Code is logged out or its login was rejected; run claude and /login"
USAGE_REASON = "Claude usage limit reached; reviews resume after it resets"
TRUST_REASON = "Claude Code is waiting on a folder trust prompt"
BLOCKERS = (
    (LOGIN_REASON, (
        "please run /login", "not logged in", "select login method", "invalid api key", "api error: 401",
        "sign in again to continue", "login expired", "oauth token revoked",
    )),
    (USAGE_REASON, (
        "usage limit reached", "hit your limit", "you've hit your", "spend limit reached", "out of extra usage",
        "credit balance is too low",
    )),
    (TRUST_REASON, ("trust this folder",)),
)
BLOCKER_REGION_LINES = 8
_reviewing: List[Tuple[PullRequest, State]] = []


@dataclass
class ClaudeResult:
    ok: bool
    artifact_url: Optional[str]
    error: Optional[str]
    blocker: bool = False


def _short(text: str) -> str:
    text = "".join(ch for ch in " ".join(text.split()) if ch.isprintable())
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 3] + "..."


def _describe(error: BaseException) -> str:
    if isinstance(error, (WorkspaceError, ValueError, BitbucketAuthError, BitbucketTransientError)) and str(error):
        return _short(str(error))
    return _short("%s: %s" % (type(error).__name__, error))


def session_name(pr_id: int) -> str:
    return "%s%d" % (SESSION_PREFIX, pr_id)


def _screen(*args: str) -> subprocess.CompletedProcess:
    cmd = [SCREEN, *args]
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(os.environ, TERM="xterm-256color"),
            check=False,
            timeout=SCREEN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log("screen %s timed out after %ds" % (args[0], SCREEN_TIMEOUT))
        return subprocess.CompletedProcess(cmd, 124, "", "timed out after %ds" % SCREEN_TIMEOUT)


def _sessions(name_pattern: str) -> List[str]:
    pattern = re.compile(r"^\s*(\d+\.%s)\s" % name_pattern)
    sessions = []
    for line in _screen("-ls").stdout.splitlines():
        match = pattern.match(line)
        if match and "(Dead" not in line:
            sessions.append(match.group(1))
    return sessions


def find_sessions(name: str) -> List[str]:
    return _sessions(re.escape(name))


def _descendants(root: int) -> List[int]:
    try:
        listing = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=False, timeout=SCREEN_TIMEOUT,
        ).stdout
    except subprocess.TimeoutExpired:
        return []
    children: Dict[int, List[int]] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found: List[int] = []
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def _signal(pid: int, signum: int) -> bool:
    try:
        os.kill(pid, signum)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _terminate(pids: List[int]) -> None:
    alive = [pid for pid in pids if pid > 1 and pid != os.getpid() and _signal(pid, signal.SIGTERM)]
    deadline = time.monotonic() + TERMINATE_WAIT
    while alive and time.monotonic() < deadline:
        time.sleep(0.1)
        alive = [pid for pid in alive if _signal(pid, 0)]
    for pid in alive:
        _signal(pid, signal.SIGKILL)


def _quit(session: str) -> None:
    pid, name = session.split(".", 1)
    window_processes = _descendants(int(pid))
    _screen("-S", session, "-X", "quit")
    deadline = time.monotonic() + QUIT_WAIT
    while session in find_sessions(name) and time.monotonic() < deadline:
        time.sleep(0.1)
    _terminate(window_processes)


def quit_sessions(name: str) -> None:
    for session in find_sessions(name):
        _quit(session)


def live_review_sessions() -> List[str]:
    return _sessions(re.escape(SESSION_PREFIX) + r"\d+")


def sweep_sessions(prefix: str = SESSION_PREFIX) -> List[str]:
    swept = _sessions(re.escape(prefix) + r"\d+")
    for session in swept:
        _quit(session)
        log("quit leftover screen session %s" % session)
    return swept


def save_hardcopy(session: str, path: Path) -> None:
    path.unlink(missing_ok=True)
    _screen("-S", session, "-p", "0", "-X", "hardcopy", str(path))
    deadline = time.monotonic() + HARDCOPY_WAIT
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if path.exists():
        time.sleep(HARDCOPY_SETTLE)


def read_screen(session: str, path: Path) -> Optional[bytes]:
    save_hardcopy(session, path)
    try:
        return path.read_bytes()
    except OSError:
        return None


def find_blocker(screen: str) -> Optional[str]:
    lines = (line.replace("\x00", " ").strip() for line in screen.splitlines())
    region = [line for line in lines if line][-BLOCKER_REGION_LINES:]
    flat = " ".join(" ".join(region).split()).lower()
    joined = "".join(region).lower()
    for reason, phrases in BLOCKERS:
        if any(phrase in flat or phrase in joined for phrase in phrases):
            return reason
    return None


def _path_rule(path: str) -> str:
    return "//" + path.lstrip("/")


def guard_command(run_dir: Path) -> str:
    return "/usr/bin/python3 -I %s guard %s || exit 2" % (RENDER_BIN, shlex.quote(str(run_dir.absolute())))


def disabled_plugins() -> Dict[str, bool]:
    names = set()
    for path, key in PLUGIN_SOURCES:
        if not path.exists():
            continue
        plugins = json.loads(path.read_text()).get(key) or {}
        if not isinstance(plugins, dict):
            raise ValueError("%s in %s is not an object" % (key, path))
        names.update(plugins)
    return {name: False for name in sorted(names)}


def session_settings(run_dir: Path) -> dict:
    guard = {"matcher": GUARDED_TOOLS, "hooks": [{
        "type": "command", "command": guard_command(run_dir), "onFailure": "block", "timeout": 30,
    }]}
    return {
        "enabledPlugins": disabled_plugins(),
        "hooks": {"PreToolUse": [guard]},
        "disableAllHooks": False,
        "statusLine": {"type": "command", "command": "true"},
    }


DASHBOARD_PROMPT = 'Use the RemoteTrigger tool once with action "run" and trigger_id "%s", then stop. Do nothing else.'


def start_dashboard_sync(trigger_id: str = DASHBOARD_TRIGGER_ID, log_path: Path = DASHBOARD_LOG) -> None:
    if not trigger_id:
        return
    try:
        settings = {"enabledPlugins": disabled_plugins(), "statusLine": {"type": "command", "command": "true"}}
        command = [
            CLAUDE_BIN, "-p", DASHBOARD_PROMPT % trigger_id,
            "--permission-mode", "dontAsk", "--strict-mcp-config", "--setting-sources", "user",
            "--settings", json.dumps(settings), "--allowedTools", "RemoteTrigger",
        ]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(log_path), "ab") as output:
            subprocess.Popen(
                command, cwd=str(log_path.parent), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True, env=dict(os.environ, **{RESTRICT_ENV: "1"}),
            )
        log("started the PR review dashboard sync (%s)" % trigger_id)
    except Exception as error:
        log("could not start the dashboard sync: %s" % _describe(error))


def write_session_settings(run_dir: Path) -> Path:
    path = run_dir / SESSION_SETTINGS
    path.write_text(json.dumps(session_settings(run_dir), indent=2))
    return path


def claude_command(context_path: Path, ws: Workspace, claude_bin: str, settings_path: Path) -> str:
    run_dir = str(ws.run_dir.absolute())
    rule = _path_rule(run_dir)
    tree = _path_rule(str(ws.worktree.absolute()))
    allowed = " ".join([
        "Skill(pr-auto-review)", "Agent", "Artifact",
        "Read(%s)" % _path_rule(str(USER_CLAUDE_MD)),
        "Grep(%s/**)" % tree,
        "Glob(%s/**)" % tree,
        "Bash(%s)" % render_command(ws.run_dir),
        "Write(%s/review.json)" % rule,
        "Edit(%s/review.json)" % rule,
        "Write(%s/result.json)" % rule,
        "Edit(%s/result.json)" % rule,
    ])
    denied = " ".join("%s(%s)" % (tool, _path_rule(path)) for path in READ_DENY for tool in DENY_TOOLS)
    args = [
        claude_bin,
        "/pr-auto-review %s" % context_path.absolute(),
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        "--setting-sources", "user",
        "--settings", str(settings_path.absolute()),
        "--add-dir", run_dir,
        "--add-dir", str(Path(MEMORY_INDEX).parent),
        "--allowedTools", allowed,
        "--disallowedTools", denied,
    ]
    return "cd %s && export %s=1 && exec %s" % (
        shlex.quote(str(ws.worktree)), RESTRICT_ENV, " ".join(shlex.quote(arg) for arg in args)
    )


def parse_result(data: Any) -> ClaudeResult:
    if not isinstance(data, dict):
        return ClaudeResult(False, None, "invalid result.json: expected a JSON object")
    status = data.get("status")
    if status == "error":
        reason = data.get("error") if isinstance(data.get("error"), str) else ""
        log("the review session reported: %s" % _short(reason or "no reason given"))
        return ClaudeResult(False, None, SESSION_ERROR)
    if status != "published":
        return ClaudeResult(False, None, "invalid result.json: status must be published or error")
    if not is_artifact_url(data.get("artifact_url")):
        return ClaudeResult(False, None, "invalid result.json: artifact_url is not a claude.ai artifact link")
    return ClaudeResult(True, data["artifact_url"], None)


def _timeout_text(seconds: int) -> str:
    if seconds % 60 == 0:
        return "%d minutes" % (seconds // 60)
    return "%d seconds" % seconds


@dataclass
class _Screen:
    session: str
    path: Path
    interval: float
    stall_seconds: float
    next_check: float
    last: Optional[bytes] = None
    last_change: float = 0.0

    def verdict(self, now: float) -> Optional[ClaudeResult]:
        if now < self.next_check:
            return None
        self.next_check = now + self.interval
        screen = read_screen(self.session, self.path)
        if screen is None:
            return None
        if screen != self.last:
            self.last, self.last_change = screen, now
            return None
        blocker = find_blocker(screen.decode("utf-8", "replace"))
        if blocker is not None:
            return ClaudeResult(False, None, blocker, blocker=True)
        if now - self.last_change >= self.stall_seconds:
            return ClaudeResult(False, None, STALL_REASON)
        return None


def _wait_for_result(
    result_path: Path,
    session: Optional[str],
    name: str,
    timeout: Optional[float],
    poll_interval: float,
    watch: Optional[_Screen] = None,
    on_long_run: Optional[Callable[[], None]] = None,
    long_run_after: float = LONG_RUN_SECONDS,
) -> ClaudeResult:
    begun = time.monotonic()
    deadline = None if timeout is None else begun + timeout
    long_run_noticed = on_long_run is None
    unparsed_once = False
    while True:
        if result_path.exists():
            try:
                return parse_result(json.loads(result_path.read_text()))
            except (OSError, ValueError) as error:
                if unparsed_once:
                    return ClaudeResult(False, None, _short("invalid result.json: %s" % error))
                unparsed_once = True
        elif session is None or session not in find_sessions(name):
            if result_path.exists():
                continue
            return ClaudeResult(False, None, "Claude session exited without a result")
        elif watch is not None:
            verdict = watch.verdict(time.monotonic())
            if verdict is not None and not result_path.exists():
                return verdict
        if not long_run_noticed and time.monotonic() - begun >= long_run_after:
            long_run_noticed = True
            try:
                on_long_run()
            except Exception as error:
                log("long-run notice failed: %s" % _describe(error))
        if deadline is None:
            time.sleep(poll_interval)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ClaudeResult(False, None, "timed out after %s" % _timeout_text(int(timeout)))
        time.sleep(min(poll_interval, remaining))


def _await_session(name: str, result_path: Path, stale: Set[str]) -> Optional[str]:
    deadline = time.monotonic() + LAUNCH_WAIT
    while True:
        sessions = [session for session in find_sessions(name) if session not in stale]
        if sessions:
            return sessions[0]
        if result_path.exists() or time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def run_claude(
    context_path: Path,
    ws: Workspace,
    claude_bin: str = CLAUDE_BIN,
    timeout: Optional[float] = None,
    poll_interval: float = 5.0,
    hardcopy_interval: float = HARDCOPY_INTERVAL,
    stall_seconds: float = STALL_SECONDS,
    on_long_run: Optional[Callable[[], None]] = None,
) -> ClaudeResult:
    name = session_name(ws.pr.id)
    quit_sessions(name)
    stale = set(find_sessions(name))
    for stale in STALE_OUTPUTS:
        (ws.run_dir / stale).unlink(missing_ok=True)
    try:
        settings_path = write_session_settings(ws.run_dir)
    except (OSError, ValueError, AttributeError) as error:
        return ClaudeResult(False, None, _short("could not write the session settings: %s" % error))

    launch = _screen("-ln", "-dmS", name, "/bin/zsh", "-c", claude_command(context_path, ws, claude_bin, settings_path))
    if launch.returncode != 0:
        return ClaudeResult(False, None, _short("could not start screen: %s" % (launch.stderr.strip() or launch.returncode)))

    session = _await_session(name, ws.run_dir / "result.json", stale)
    log("started %s for PR #%d" % (session or name, ws.pr.id))
    screen_path = ws.run_dir / "screen.txt"
    watch = None
    if session is not None:
        watch = _Screen(session, screen_path, hardcopy_interval, stall_seconds, time.monotonic() + hardcopy_interval)
    try:
        return _wait_for_result(ws.run_dir / "result.json", session, name, timeout, poll_interval, watch, on_long_run)
    finally:
        if session is not None and session in find_sessions(name):
            save_hardcopy(session, screen_path)
        quit_sessions(name)
        log("PR #%d run dir %s; screen %s" % (ws.pr.id, ws.run_dir, screen_path))


def read_page(run_dir: Path, pr: PullRequest) -> dict:
    page = json.loads((run_dir / "page.json").read_text())
    doc = page.get("pr") if isinstance(page, dict) else None
    sections = page.get("sections") if isinstance(page, dict) else None
    latest = sections[0] if isinstance(sections, list) and sections else None
    if (
        not isinstance(doc, dict)
        or doc.get("id") != pr.id
        or not isinstance(latest, dict)
        or not str(latest.get("head", "")).startswith(pr.head)
    ):
        raise ValueError("page.json is not a review of PR #%d at %s" % (pr.id, pr.head))
    return page


def finish(
    pr: PullRequest,
    run_dir: Path,
    artifact_url: str,
    state: State,
    now: datetime,
    data_dir: Path = DATA_DIR,
) -> dict:
    if not is_artifact_url(artifact_url):
        raise ValueError("artifact URL is not a claude.ai artifact link")
    known = state.prs.get(pr.id)
    if known is not None and known.artifact_url and artifact_url != known.artifact_url:
        raise ValueError(UNEXPECTED_URL)
    page = read_page(run_dir, pr)
    pages = data_dir / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    shutil.move(str(run_dir / "page.json"), str(pages / ("%d.json" % pr.id)))

    entry = state.prs.get(pr.id)
    if entry is None:
        entry = PrEntry(head=pr.head, head_first_seen=int(now.timestamp()))
        state.prs[pr.id] = entry
    entry.reviewed_commit = pr.head
    entry.artifact_url = artifact_url
    entry.attempts = 0
    entry.blocked = 0
    entry.baseline_head = None
    return page


@dataclass
class Deps:
    list_prs: Callable[[], List[PullRequest]] = bitbucket.list_reviewing_prs
    get_pr: Callable[[int], PullRequest] = bitbucket.get_pr
    prepare: Callable[[PullRequest, str, Optional[str]], Workspace] = workspace.prepare
    cleanup: Callable[[Workspace], None] = workspace.cleanup
    run_claude: Callable[..., ClaudeResult] = run_claude
    send: Callable[[str], bool] = notify.send
    now: Callable[[], datetime] = datetime.now
    data_dir: Path = DATA_DIR
    worktrees_dir: Path = WORKTREES_DIR
    runs_dir: Path = RUNS_DIR
    sweep: Callable[[], List[str]] = sweep_sessions
    watchdog_seconds: int = STUCK_GRACE
    live_sessions: Callable[[], List[str]] = live_review_sessions
    sync_dashboard: Callable[[], None] = start_dashboard_sync


def acquire_lock(data_dir: Path, started: int, wait: float = 0.0) -> Optional[IO[str]]:
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(str(data_dir / "watch.lock"), "a+")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                handle.close()
                return None
            time.sleep(min(0.5, remaining))
    handle.truncate(0)
    handle.write("%d\n" % started)
    handle.flush()
    return handle


def release_lock(handle: IO[str]) -> None:
    handle.truncate(0)
    handle.close()


def lock_started(data_dir: Path) -> Optional[int]:
    try:
        return int((data_dir / "watch.lock").read_text().strip())
    except (OSError, ValueError):
        return None


def _notify_stuck(deps: Deps, started: int) -> None:
    reason = "PR review watcher has been stuck since %s" % datetime.fromtimestamp(started).strftime("%b %-d %-I:%M %p")
    log(reason)
    marker = deps.data_dir / "stuck-notified"
    try:
        notified: Optional[int] = int(marker.read_text().strip())
    except (OSError, ValueError):
        notified = None
    if notified != started:
        deps.send(format_failure(None, reason))
        marker.write_text("%d\n" % started)


def _report_stuck(deps: Deps) -> None:
    started = lock_started(deps.data_dir)
    if started is None or int(deps.now().timestamp()) - started <= STUCK_AFTER or deps.live_sessions():
        return
    _notify_stuck(deps, started)


def _count_hung_attempt(pr: PullRequest, state: State, data_dir: Path) -> None:
    entry = state.prs[pr.id]
    entry.attempts += 1
    log("PR #%d review hung past the watchdog (attempt %d of %d)" % (pr.id, entry.attempts, MAX_ATTEMPTS))
    try:
        save_entry(state, pr.id, data_dir)
    except Exception as error:
        log("PR #%d could not record the hung attempt: %s" % (pr.id, _describe(error)))


def _supervised(deps: Deps, started: int, body: Callable[[], int]) -> int:
    def fire(signum: int, frame: Optional[FrameType]) -> None:
        for pr, state in list(_reviewing):
            _count_hung_attempt(pr, state, deps.data_dir)
        _notify_stuck(deps, started)
        raise SystemExit(1)

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(deps.watchdog_seconds)
    try:
        (deps.data_dir / "stuck-notified").unlink(missing_ok=True)
        deps.sweep()
        return body()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def save_entry(state: State, pr_id: int, data_dir: Path) -> None:
    path = data_dir / "state.json"
    try:
        current = load_state(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        current = State(prs=dict(state.prs), failures={})
    current.prs[pr_id] = state.prs[pr_id]
    current.failures = state.failures
    save_state(current, path)


def load_state_or_notify(deps: Deps) -> Optional[State]:
    marker = deps.data_dir / "state.corrupt-notified"
    try:
        state = load_state(deps.data_dir / "state.json")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        reason = "state.json is unreadable: %s. Fix or re-seed with pr-review seed --force." % _describe(error)
        log(reason)
        if not marker.exists():
            deps.send(format_failure(None, reason))
            marker.write_text(datetime.now().isoformat(timespec="seconds") + "\n")
        return None
    marker.unlink(missing_ok=True)
    return state


def _list_prs(state: State, deps: Deps) -> Optional[List[PullRequest]]:
    try:
        prs = deps.list_prs()
    except SecretMissing:
        reason, transient = "Bitbucket token missing from Keychain", False
    except BitbucketAuthError as error:
        reason, transient = _describe(error), False
    except BitbucketTransientError as error:
        reason, transient = _describe(error), True
    except Exception as error:
        reason, transient = "Bitbucket request failed: %s" % _describe(error), True
    else:
        clear_failure(state, "bitbucket")
        return prs
    log("bitbucket: %s" % reason)
    if record_failure(state, "bitbucket", reason, transient):
        deps.send(format_failure(None, reason))
    return None


def review_message(pr: PullRequest, ws: Workspace, page: dict, url: str) -> str:
    section = page["sections"][0]
    if section["kind"] == "full":
        return format_review_ready(pr, section["one_line"], latest_counts(page), url)
    return format_update(pr, latest_counts(page), latest_fixed_count(page), url, section["kind"] == "full_fallback")


def _attempt_failed(pr: PullRequest, state: State, reason: str, run_dir: Path, deps: Deps) -> bool:
    entry = state.prs[pr.id]
    entry.attempts += 1
    log("PR #%d review failed (attempt %d of %d): %s; run dir %s" % (pr.id, entry.attempts, MAX_ATTEMPTS, reason, run_dir))
    save_entry(state, pr.id, deps.data_dir)
    if entry.attempts == MAX_ATTEMPTS:
        deps.send(format_failure(pr.id, reason))
    return False


def _blocked(pr: PullRequest, state: State, reason: str, run_dir: Path, deps: Deps) -> bool:
    entry = state.prs[pr.id]
    if record_failure(state, "claude", reason, transient=False):
        deps.send(format_failure(None, reason))
    if entry.blocked >= MAX_UNCOUNTED_BLOCKS:
        return _attempt_failed(pr, state, reason, run_dir, deps)
    entry.blocked += 1
    log("PR #%d review blocked, attempt not counted (%d of %d): %s; run dir %s" % (
        pr.id, entry.blocked, MAX_UNCOUNTED_BLOCKS, reason, run_dir))
    save_entry(state, pr.id, deps.data_dir)
    return False


def _cleanup(ws: Workspace, deps: Deps) -> None:
    try:
        deps.cleanup(ws)
    except Exception as error:
        log("PR #%d cleanup failed: %s" % (ws.pr.id, _describe(error)))


def _no_new_commits(pr: PullRequest, ws: Workspace, state: State, deps: Deps) -> bool:
    _cleanup(ws, deps)
    state.prs[pr.id].reviewed_commit = pr.head
    save_entry(state, pr.id, deps.data_dir)
    log("PR #%d: no new commits from the author since %s; marked reviewed at %s" % (pr.id, ws.base[:12], pr.head))
    return True


def review_pr(pr: PullRequest, mode: str, base: Optional[str], state: State, deps: Deps) -> bool:
    _reviewing.append((pr, state))
    try:
        return _review_pr(pr, mode, base, state, deps)
    finally:
        _reviewing.pop()


def _skip_fork(pr: PullRequest, state: State, deps: Deps) -> bool:
    entry = state.prs[pr.id]
    first = not entry.fork
    entry.fork = True
    entry.baseline_head = pr.head
    save_entry(state, pr.id, deps.data_dir)
    log("PR #%d comes from fork %r; marked seen at %s and never auto-reviewed" % (pr.id, pr.source_repo, pr.head))
    if first:
        deps.send(format_fork(pr.id))
    return False


def _review_pr(pr: PullRequest, mode: str, base: Optional[str], state: State, deps: Deps) -> bool:
    entry = state.prs[pr.id]
    if pr.source_repo != REPO_FULL_NAME:
        return _skip_fork(pr, state, deps)
    log("PR #%d: %s review at %s" % (pr.id, mode, pr.head))
    try:
        ws = deps.prepare(pr, mode, base)
    except Exception as error:
        return _attempt_failed(pr, state, _describe(error), deps.runs_dir / ("%d-%s" % (pr.id, pr.head)), deps)
    if ws.mode == "incremental" and ws.commit_count == 0:
        return _no_new_commits(pr, ws, state, deps)

    message = ""
    error_text: Optional[str] = None
    blocker: Optional[str] = None
    try:
        earlier = open_findings(load_page(pr.id, deps.data_dir))
        context_path = write_context(ws, earlier, entry.artifact_url, "watcher")
        minutes = int(LONG_RUN_SECONDS // 60)

        def long_run_notice() -> None:
            log("PR #%d review has been running for %d minutes" % (pr.id, minutes))
            deps.send(format_long_run(pr, minutes, session_name(pr.id)))

        paused = signal.alarm(0)
        try:
            result = deps.run_claude(context_path, ws, on_long_run=long_run_notice)
        finally:
            if paused:
                signal.alarm(deps.watchdog_seconds)
        if result.blocker:
            blocker = result.error or "Claude Code is blocked"
        elif result.ok and result.artifact_url:
            message = review_message(pr, ws, read_page(ws.run_dir, pr), result.artifact_url)
            finish(pr, ws.run_dir, result.artifact_url, state, deps.now(), deps.data_dir)
            clear_failure(state, "claude")
            save_entry(state, pr.id, deps.data_dir)
        else:
            error_text = result.error or "Claude run failed"
    except Exception as error:
        error_text = _describe(error)
    finally:
        _cleanup(ws, deps)

    if blocker is not None:
        return _blocked(pr, state, blocker, ws.run_dir, deps)
    if error_text is not None:
        return _attempt_failed(pr, state, error_text, ws.run_dir, deps)
    deps.send(message)
    log("PR #%d reviewed: %s" % (pr.id, entry.artifact_url))
    deps.sync_dashboard()
    return True


def _watch(deps: Deps) -> int:
    removed = prune_runs(deps.runs_dir, deps.now().timestamp())
    if removed:
        log("removed %d run dirs older than 14 days: %s" % (len(removed), ", ".join(sorted(removed))))
    state_path = deps.data_dir / "state.json"
    state = load_state_or_notify(deps)
    if state is None:
        return 1
    prs = _list_prs(state, deps)
    if prs is None:
        save_state(state, state_path)
        return 0
    stamp = int(deps.now().timestamp())
    observe(state, prs, stamp)
    action = next_action(state, prs, stamp)
    save_state(state, state_path)
    if action is None:
        log("tick: %d PRs, no action" % len(prs))
        return 0
    review_pr(action.pr, action.mode, action.base, state, deps)
    return 0


def watch(deps: Deps = Deps()) -> int:
    started = int(deps.now().timestamp())
    lock = acquire_lock(deps.data_dir, started)
    if lock is None:
        log("previous run still active")
        _report_stuck(deps)
        return 0
    try:
        return _supervised(deps, started, lambda: _watch(deps))
    finally:
        release_lock(lock)


def _seed(force: bool, deps: Deps) -> int:
    state_path = deps.data_dir / "state.json"
    state = State(prs={}, failures={})
    if state_path.exists():
        if not force:
            log("state.json already exists; re-seed with pr-review seed --force")
            return 1
        try:
            state = load_state(state_path)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            log("state.json is unreadable (%s); seeding from scratch" % _describe(error))
    try:
        prs = deps.list_prs()
    except SecretMissing:
        log("seed failed: Bitbucket token missing from Keychain")
        return 1
    except (BitbucketAuthError, BitbucketTransientError) as error:
        log("seed failed: %s" % _describe(error))
        return 1

    stamp = int(deps.now().timestamp())
    for pr in prs:
        baseline = None if pr.draft else pr.head
        entry = state.prs.get(pr.id)
        if entry is None:
            state.prs[pr.id] = PrEntry(head=pr.head, head_first_seen=stamp, baseline_head=baseline)
            continue
        entry.head = pr.head
        entry.head_first_seen = stamp
        entry.baseline_head = baseline
        entry.attempts = 0
        entry.blocked = 0
    save_state(state, state_path)
    (deps.data_dir / "state.corrupt-notified").unlink(missing_ok=True)
    drafts = sum(1 for pr in prs if pr.draft)
    log("seeded %d PRs (%d ready, %d drafts)" % (len(prs), len(prs) - drafts, drafts))
    return 0


def seed(force: bool, deps: Deps = Deps()) -> int:
    lock = acquire_lock(deps.data_dir, int(deps.now().timestamp()))
    if lock is None:
        log("previous run still active")
        return 1
    try:
        return _seed(force, deps)
    finally:
        release_lock(lock)


def _run_one(pr_id: int, base: Optional[str], deps: Deps) -> int:
    state = load_state_or_notify(deps)
    if state is None:
        return 1
    try:
        pr = deps.get_pr(pr_id)
    except SecretMissing:
        log("could not fetch PR #%d: Bitbucket token missing from Keychain" % pr_id)
        return 1
    except (BitbucketAuthError, BitbucketTransientError, KeyError, TypeError, ValueError, AttributeError) as error:
        log("could not fetch PR #%d: %s" % (pr_id, _describe(error)))
        return 1
    observe(state, [pr], int(deps.now().timestamp()))
    save_state(state, deps.data_dir / "state.json")
    return 0 if review_pr(pr, "incremental" if base else "full", base, state, deps) else 1


def run_one(pr_id: int, base: Optional[str], deps: Deps = Deps()) -> int:
    started = int(deps.now().timestamp())
    lock = acquire_lock(deps.data_dir, started)
    if lock is None:
        log("previous run still active")
        return 1
    try:
        return _supervised(deps, started, lambda: _run_one(pr_id, base, deps))
    finally:
        release_lock(lock)

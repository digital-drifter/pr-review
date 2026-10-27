import contextlib
import fcntl
import inspect
import io
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from unittest import mock

from prreview.bitbucket import BitbucketAuthError, BitbucketTransientError
from prreview.cli import main
from prreview.keychain import SecretMissing
from prreview.models import Counts, PullRequest
from prreview.notify import format_failure, format_long_run, format_review_ready, format_update
from prreview.render import apply_review, load_page
from prreview import runner, workspace
from prreview.config import RUNS_DIR
from prreview.runner import (
    ClaudeResult, Deps, acquire_lock, find_sessions, finish, parse_result, release_lock, run_claude, run_one, seed,
    sweep_sessions, watch,
)
from prreview.state import PrEntry, State, load_state, save_state
from prreview.workspace import Workspace, WorkspaceError, write_context

FAKE_CLAUDE = str(Path(__file__).resolve().parent / "fake_claude.py")
BIN = str(Path(__file__).resolve().parent.parent / "bin" / "pr-review")
HOME_RULE = "//" + str(Path.home()).lstrip("/")
TEST_PREFIX = "pr-review-test-"
LIVE_SWEEP = re.compile(r"pr-review-[0-9]+")
URL = "https://claude.ai/artifact/abc123"
NOW = datetime(2026, 10, 27, 12, 0, 0)
LONG_AGO = int((NOW - timedelta(minutes=30)).timestamp())
ONE_LINE = "Moves invoice lookups to the bridge."


def screen_sessions(name: str) -> List[str]:
    out = subprocess.run(["/usr/bin/screen", "-ls"], capture_output=True, text=True).stdout
    return re.findall(r"^\s*(\d+\.%s)\s" % re.escape(name), out, re.M)


def kill_sessions(name: str) -> None:
    for session in screen_sessions(name):
        subprocess.run(["/usr/bin/screen", "-S", session, "-X", "quit"], capture_output=True)


def make_pr(pr_id: int = 101, head: str = "a" * 12, draft: bool = False, title: str = "Move invoice lookups") -> PullRequest:
    return PullRequest(
        id=pr_id, title=title, description="desc", author="Jordan", draft=draft, head=head,
        source_branch="feature/x", destination_branch="master",
        link="https://bitbucket.org/acme/widgets/pull-requests/%d" % pr_id,
    )


def make_ws(
    pr: PullRequest,
    root: Path,
    mode: str = "full",
    base: Optional[str] = None,
    full_fallback: bool = False,
    changed_lines: int = 40,
    commit_count: int = 2,
) -> Workspace:
    run_dir = root / "runs" / ("%d-%s" % (pr.id, pr.head))
    worktree = root / "worktrees" / str(pr.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree.mkdir(parents=True, exist_ok=True)
    return Workspace(
        pr=pr, mode=mode, base=base or "0" * 40, head=pr.head + "f" * 28, full_fallback=full_fallback,
        worktree=worktree, run_dir=run_dir, diff_command="git diff x y", files_changed=3,
        changed_lines=changed_lines, commit_count=commit_count,
    )


def finding(severity: str = "nit", title: str = "Missing return type") -> dict:
    data = {
        "severity": severity, "title": title, "file": "app/Foo.php", "line": 12,
        "breaks_when": "it breaks", "fix": "fix it",
    }
    if severity in ("critical", "should_fix"):
        data["reproduce"] = "Add a test that loads 50 tickets and count the queries"
    return data


def review(findings: Optional[List[dict]] = None, fixed: Optional[List[str]] = None) -> dict:
    return {"summary": "Moves invoice lookups.", "one_line": ONE_LINE, "findings": findings or [], "fixed": fixed or []}


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.prs: List[PullRequest] = []
        self.list_error: Optional[Exception] = None
        self.prepare_error: Optional[Exception] = None
        self.ws_options: dict = {}
        self.outcomes: List[str] = []
        self.review = review()
        self.clock = NOW
        self.sent: List[str] = []
        self.prepared: List[Tuple[int, str, Optional[str]]] = []
        self.cleaned: List[int] = []
        self.cleaned_paths: List[Path] = []
        self.cleanup_error: Optional[Exception] = None
        self.contexts: List[dict] = []
        self.events: List[str] = []
        self.reviewed_when_sent: List[Optional[str]] = []
        self.list_calls = 0
        self.prepare_hang = False
        self.live: List[str] = []
        self.alarm_during_run: List[float] = []
        self.alarm_when_sent: List[float] = []
        self.long_run = False
        self.syncs = 0

    def sweep(self) -> List[str]:
        self.events.append("sweep")
        return []

    def sync_dashboard(self) -> None:
        self.syncs += 1

    def list_prs(self) -> List[PullRequest]:
        self.events.append("list")
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.prs)

    def get_pr(self, pr_id: int) -> PullRequest:
        return next(pr for pr in self.prs if pr.id == pr_id)

    def prepare(self, pr: PullRequest, mode: str, base: Optional[str]) -> Workspace:
        self.prepared.append((pr.id, mode, base))
        if self.prepare_hang:
            time.sleep(10)
        if self.prepare_error is not None:
            raise self.prepare_error
        options = dict(self.ws_options)
        return make_ws(pr, self.root, mode=options.pop("mode", mode), base=base, **options)

    def cleanup(self, ws: Workspace) -> None:
        self.cleaned.append(ws.pr.id)
        self.cleaned_paths.append(ws.worktree)
        if self.cleanup_error is not None:
            raise self.cleanup_error

    def run_claude(
        self, context_path: Path, ws: Workspace, on_long_run: Optional[Callable[[], None]] = None
    ) -> ClaudeResult:
        if self.long_run and on_long_run is not None:
            on_long_run()
        context = json.loads(context_path.read_text())
        self.contexts.append(context)
        self.alarm_during_run.append(signal.getitimer(signal.ITIMER_REAL)[0])
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "hang":
            time.sleep(10)
        if outcome == "blocked":
            return ClaudeResult(False, None, runner.USAGE_REASON, blocker=True)
        if outcome not in ("ok", "tamper", "badpage", "concurrent"):
            return ClaudeResult(False, None, outcome)
        page_context = dict(context, head="9" * 40) if outcome == "badpage" else context
        page = apply_review(load_page(ws.pr.id, self.data_dir), page_context, self.review, self.clock)
        (ws.run_dir / "page.json").write_text(json.dumps(page))
        if outcome == "tamper":
            tampered = dict(context, pr=dict(context["pr"], id=999, head="z" * 12), run_dir=str(self.root / "elsewhere"))
            context_path.write_text(json.dumps(tampered))
        if outcome == "concurrent":
            state = load_state(self.data_dir / "state.json")
            state.prs[202] = PrEntry("m" * 12, 5, reviewed_commit="m" * 12, artifact_url=URL)
            save_state(state, self.data_dir / "state.json")
        return ClaudeResult(True, URL, None)

    def send(self, text: str) -> bool:
        self.sent.append(text)
        self.alarm_when_sent.append(signal.getitimer(signal.ITIMER_REAL)[0])
        try:
            entry = load_state(self.data_dir / "state.json").prs.get(101)
        except (ValueError, TypeError):
            entry = None
        self.reviewed_when_sent.append(entry.reviewed_commit if entry else None)
        return True

    def deps(self) -> Deps:
        return Deps(
            list_prs=self.list_prs, get_pr=self.get_pr, prepare=self.prepare, cleanup=self.cleanup,
            run_claude=self.run_claude, send=self.send, now=lambda: self.clock, data_dir=self.data_dir,
            worktrees_dir=self.root / "worktrees", runs_dir=self.root / "runs", sweep=self.sweep,
            live_sessions=lambda: list(self.live), sync_dashboard=self.sync_dashboard,
        )


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.use_harness(self.root)

    def use_harness(self, root: Path) -> None:
        self.h = Harness(root)
        self.state_path = self.h.data_dir / "state.json"
        self.output = ""

    def save(self, entries: dict, failures: Optional[dict] = None) -> None:
        save_state(State(prs=entries, failures=failures or {}), self.state_path)

    def state(self) -> State:
        return load_state(self.state_path)

    def call(self, fn: Callable[[], int]) -> int:
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                return fn()
        finally:
            self.output += out.getvalue()

    def tick(self) -> int:
        return self.call(lambda: watch(self.h.deps()))

    def earlier_page(self, pr: PullRequest, findings: List[dict]) -> dict:
        context_path = write_context(make_ws(pr, self.h.root), [], None, "watcher")
        page = apply_review(None, json.loads(context_path.read_text()), review(findings), NOW - timedelta(days=1))
        pages = self.h.data_dir / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        (pages / ("%d.json" % pr.id)).write_text(json.dumps(page))
        return page


class WatchTest(RunnerTestCase):
    def test_watch_reviews_action_marks_reviewed_and_sends_review_ready(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.review = review([finding("nit"), finding("critical", "Double charge")])

        self.assertEqual(self.tick(), 0)

        entry = self.state().prs[101]
        self.assertEqual(
            (entry.reviewed_commit, entry.artifact_url, entry.attempts, entry.baseline_head), (pr.head, URL, 0, None)
        )
        self.assertEqual(self.h.prepared, [(101, "full", None)])
        self.assertEqual(self.h.cleaned, [101])
        context = self.h.contexts[0]
        self.assertEqual((context["invoked_by"], context["artifact_url"], context["earlier_findings"]), ("watcher", None, []))
        self.assertTrue((self.h.data_dir / "pages" / "101.json").exists())
        self.assertEqual(list(self.h.root.glob("runs/*/page.json")), [])
        self.assertEqual(self.h.sent, [format_review_ready(pr, ONE_LINE, Counts(1, 0, 1), URL)])
        self.assertEqual(self.h.reviewed_when_sent, [pr.head])

    def test_watch_tick_prunes_run_dirs_older_than_two_weeks(self) -> None:
        old = self.h.root / "runs" / "77-0123456789ab"
        fresh = self.h.root / "runs" / "78-0123456789ab"
        for path, age in ((old, timedelta(days=15)), (fresh, timedelta(days=13))):
            path.mkdir(parents=True)
            stamp = (NOW - age).timestamp()
            os.utime(str(path), (stamp, stamp))

        self.assertEqual(self.tick(), 0)

        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertIn("removed 1 run dirs older than 14 days: 77-0123456789ab", self.output)

    def test_watch_and_run_one_sweep_leftover_sessions_before_anything_else(self) -> None:
        self.h.prs = [make_pr()]

        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.call(lambda: run_one(101, None, self.h.deps())), 0)

        self.assertEqual(self.h.events, ["sweep", "list", "sweep"])
        self.assertEqual(runner.Deps().sweep, sweep_sessions)

    def test_watch_ignores_a_tampered_context(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.outcomes = ["tamper"]

        self.assertEqual(self.tick(), 0)

        entries = self.state().prs
        self.assertEqual(list(entries), [101])
        self.assertEqual((entries[101].reviewed_commit, entries[101].artifact_url), (pr.head, URL))
        self.assertEqual(sorted(p.name for p in (self.h.data_dir / "pages").iterdir()), ["101.json"])
        self.assertEqual(self.h.cleaned_paths, [self.root / "worktrees" / "101"])

    def test_watch_invalid_page_counts_attempt_and_moves_nothing(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.outcomes = ["badpage"]

        self.assertEqual(self.tick(), 0)

        entry = self.state().prs[101]
        self.assertEqual((entry.attempts, entry.reviewed_commit, entry.artifact_url), (1, None, None))
        self.assertFalse((self.h.data_dir / "pages").exists())
        self.assertEqual(len(list(self.h.root.glob("runs/*/page.json"))), 1)
        self.assertEqual((self.h.sent, self.h.cleaned), ([], [101]))

    def test_watch_keeps_entries_written_during_a_review(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.outcomes = ["concurrent"]

        self.assertEqual(self.tick(), 0)

        entries = self.state().prs
        self.assertEqual(entries[202], PrEntry("m" * 12, 5, reviewed_commit="m" * 12, artifact_url=URL))
        self.assertEqual(entries[101].reviewed_commit, pr.head)

    def test_watch_starts_the_dashboard_sync_after_a_published_review(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.h.syncs, 1)
        self.assertEqual(self.state().prs[101].artifact_url, URL)

    def test_watch_does_not_start_the_dashboard_sync_when_nothing_was_published(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.outcomes = ["Claude session exited without a result"]

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.h.syncs, 0)

    def test_watch_sends_a_heads_up_when_a_review_runs_long(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.h.long_run = True
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.h.sent[0], format_long_run(pr, 60, "pr-review-101"))
        self.assertEqual(len(self.h.sent), 2)
        self.assertIn("PR #101 review has been running for 60 minutes", self.output)

    def test_watch_does_not_report_a_long_review_as_stuck(self) -> None:
        self.h.data_dir.mkdir(parents=True)
        self.h.live = ["4242.pr-review-101"]
        started = NOW - timedelta(hours=6)
        with open(str(self.h.data_dir / "watch.lock"), "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.write("%d\n" % int(started.timestamp()))
            handle.flush()

            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.h.sent, [])
        self.assertFalse((self.h.data_dir / "stuck-notified").exists())

    def test_watch_reports_a_stuck_lock_once(self) -> None:
        self.h.data_dir.mkdir(parents=True)
        marker = self.h.data_dir / "stuck-notified"
        started = NOW - timedelta(seconds=runner.STUCK_AFTER - 300)
        with open(str(self.h.data_dir / "watch.lock"), "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.write("%d\n" % int(started.timestamp()))
            handle.flush()

            self.assertEqual(self.tick(), 0)
            self.assertEqual(self.h.sent, [])
            self.h.clock = NOW + timedelta(minutes=10)
            self.assertEqual(self.tick(), 0)
            self.assertEqual(self.tick(), 0)
            self.assertEqual(len(self.h.sent), 1)
            handle.truncate(0)
            handle.write("%d\n" % int((started + timedelta(minutes=4)).timestamp()))
            handle.flush()
            self.assertEqual(self.tick(), 0)
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.h.sent, [
            format_failure(None, "PR review watcher has been stuck since Oct 27 11:35 AM"),
            format_failure(None, "PR review watcher has been stuck since Oct 27 11:39 AM"),
        ])
        self.assertTrue(marker.exists())
        self.assertEqual(self.h.list_calls, 0)
        self.assertEqual(self.tick(), 0)
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.h.sent), 2)

    def test_watchdog_reports_and_unwinds_a_hung_run(self) -> None:
        pr = make_pr()
        previous = signal.getsignal(signal.SIGALRM)
        for name, command in (("watch", watch), ("run_one", lambda deps: run_one(101, None, deps))):
            with self.subTest(name):
                self.use_harness(self.root / name)
                self.h.prs = [pr]
                self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
                self.h.prepare_hang = True
                deps = replace(self.h.deps(), watchdog_seconds=1)
                started = time.monotonic()

                with self.assertRaises(SystemExit):
                    self.call(lambda: command(deps))

                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(self.h.sent, [format_failure(None, "PR review watcher has been stuck since Oct 27 12:00 PM")])
                self.assertEqual(self.h.cleaned, [])
                self.assertEqual(self.state().prs[101].attempts, 1)
                self.assertIn("PR #101 review hung past the watchdog (attempt 1 of 3)", self.output)
                self.assertEqual(runner._reviewing, [])
                self.assertEqual((self.h.data_dir / "stuck-notified").read_text().strip(), str(int(NOW.timestamp())))
                self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
                self.assertEqual(signal.alarm(0), 0)
                lock = acquire_lock(self.h.data_dir, 1)
                self.assertIsNotNone(lock)
                release_lock(lock)

    def test_watchdog_does_not_limit_the_claude_run(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})

        self.assertEqual(self.call(lambda: watch(replace(self.h.deps(), watchdog_seconds=100))), 0)

        self.assertEqual(self.h.alarm_during_run, [0.0])
        self.assertEqual(len(self.h.alarm_when_sent), 1)
        self.assertGreater(self.h.alarm_when_sent[0], 0)
        self.assertEqual(signal.alarm(0), 0)

    def test_a_head_that_keeps_hanging_stops_after_three_attempts(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, attempts=2)})
        self.h.prepare_hang = True

        with self.assertRaises(SystemExit):
            self.call(lambda: watch(replace(self.h.deps(), watchdog_seconds=1)))
        self.assertEqual(self.state().prs[101].attempts, 3)

        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.h.prepared), 1)
        self.assertIn("tick: 1 PRs, no action", self.output)

    def test_watch_incremental_sends_update_copy(self) -> None:
        old = "b" * 12
        pr = make_pr(head="c" * 12)
        self.h.prs = [pr]
        self.earlier_page(make_pr(head=old), [finding("should_fix", "N+1 on tickets")])
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, reviewed_commit=old, artifact_url=URL)})
        self.h.review = review([finding("nit")], fixed=["F1"])

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.h.prepared, [(101, "incremental", old)])
        context = self.h.contexts[0]
        self.assertEqual(context["artifact_url"], URL)
        self.assertEqual([f["id"] for f in context["earlier_findings"]], ["F1"])
        self.assertEqual(self.h.sent, [format_update(pr, Counts(0, 0, 1), 1, URL, False)])
        self.assertEqual(self.state().prs[101].reviewed_commit, pr.head)

    def test_fork_pr_is_skipped_for_good_with_one_dm(self) -> None:
        pr = replace(make_pr(), source_repo="someone/widgets")
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})

        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 0)
        pushed = replace(pr, head="b" * 12)
        self.h.prs = [pushed]
        self.h.clock = NOW + timedelta(minutes=20)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.call(lambda: run_one(101, None, self.h.deps())), 1)

        entry = self.state().prs[101]
        self.assertEqual((entry.fork, entry.baseline_head, entry.reviewed_commit, entry.attempts), (True, "b" * 12, None, 0))
        self.assertEqual(self.h.sent, ["PR #101 comes from a fork; fork PRs aren't auto-reviewed."])
        self.assertEqual((self.h.prepared, self.h.contexts), ([], []))
        self.assertIn("PR #101 comes from fork 'someone/widgets'; marked seen at aaaaaaaaaaaa", self.output)

    def test_stale_fork_flag_is_cleared_once_the_pr_is_seen_from_the_repo(self) -> None:
        pr = make_pr(head="b" * 12)
        self.h.prs = [pr]
        self.save({101: PrEntry(head="a" * 12, head_first_seen=LONG_AGO, baseline_head="a" * 12, fork=True)})

        self.assertEqual(self.tick(), 0)

        self.assertFalse(self.state().prs[101].fork)
        self.assertEqual(self.h.prepared, [(101, "full", None)])
        self.assertEqual(self.h.sent, [format_review_ready(pr, ONE_LINE, Counts(0, 0, 0), URL)])
        self.assertNotIn("fork", self.output)

    def test_watch_merge_only_push_is_marked_reviewed_without_claude_or_dm(self) -> None:
        old = "b" * 12
        pr = make_pr(head="c" * 12)
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, reviewed_commit=old, artifact_url=URL)})
        self.h.ws_options = {"commit_count": 0}

        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 0)

        entry = self.state().prs[101]
        self.assertEqual((entry.reviewed_commit, entry.artifact_url, entry.attempts), (pr.head, URL, 0))
        self.assertEqual(self.h.prepared, [(101, "incremental", old)])
        self.assertEqual((self.h.contexts, self.h.sent, self.h.cleaned), ([], [], [101]))
        self.assertIn("PR #101: no new commits from the author since %s" % old, self.output)

    def test_full_review_with_no_author_commits_still_runs(self) -> None:
        pr = make_pr(head="c" * 12)
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, reviewed_commit="b" * 12, artifact_url=URL)})
        self.h.ws_options = {"commit_count": 0, "mode": "full", "full_fallback": True}

        self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.h.contexts), 1)
        self.assertEqual(len(self.h.sent), 1)

    def test_watch_rejects_a_publish_to_a_different_artifact(self) -> None:
        pr = make_pr(head="c" * 12)
        self.h.prs = [pr]
        other = "https://claude.ai/artifact/other"
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, reviewed_commit="b" * 12, artifact_url=other, attempts=2)})

        self.assertEqual(self.tick(), 0)

        entry = self.state().prs[101]
        self.assertEqual((entry.attempts, entry.artifact_url, entry.reviewed_commit), (3, other, "b" * 12))
        self.assertEqual(self.h.sent, [format_failure(101, "published to an unexpected URL")])

    def test_watch_large_and_rebased_reviews_pick_their_copy(self) -> None:
        pr = make_pr(head="c" * 12)
        cases = [
            ("large_full_review", {"changed_lines": 9000}, review([finding("nit")]),
             format_update(pr, Counts(0, 0, 1), 0, URL, False)),
            ("full_fallback", {"mode": "full", "full_fallback": True}, review([finding("critical")]),
             format_update(pr, Counts(1, 0, 0), 0, URL, True)),
        ]
        for name, ws_options, data, expected in cases:
            with self.subTest(name):
                self.use_harness(self.root / name)
                self.h.prs = [pr]
                self.h.ws_options = ws_options
                self.h.review = data
                self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, reviewed_commit="b" * 12, artifact_url=URL)})

                self.assertEqual(self.tick(), 0)

                self.assertEqual(self.h.sent, [expected])

    def test_watch_failure_counts_attempts_cleans_up_and_notifies_on_third(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        reason = "Claude session exited without a result"
        self.h.outcomes = [reason] * 3

        sent_counts = []
        for _ in range(4):
            self.assertEqual(self.tick(), 0)
            sent_counts.append(len(self.h.sent))

        entry = self.state().prs[101]
        self.assertEqual((entry.attempts, entry.reviewed_commit), (3, None))
        self.assertEqual(sent_counts, [0, 0, 1, 1])
        self.assertEqual(self.h.cleaned, [101, 101, 101])
        self.assertEqual(len(self.h.prepared), 3)
        self.assertEqual(self.h.sent, [format_failure(101, reason)])
        self.assertFalse((self.h.data_dir / "pages" / "101.json").exists())
        self.assertIn("(attempt 3 of 3): %s; run dir %s" % (reason, self.root / "runs" / "101-aaaaaaaaaaaa"), self.output)

    def test_watch_blocked_session_notifies_once_without_charging_attempts(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.outcomes = ["blocked"] * 2

        for _ in range(2):
            self.assertEqual(self.tick(), 0)

        entry = self.state().prs[101]
        self.assertEqual((entry.attempts, entry.blocked, entry.reviewed_commit), (0, 2, None))
        self.assertEqual(self.state().failures["claude"].count, 2)
        self.assertEqual(self.h.sent, [format_failure(None, runner.USAGE_REASON)])
        self.assertEqual(self.h.cleaned, [101] * 2)
        self.assertIn(
            "blocked, attempt not counted (2 of 2): %s; run dir %s" % (runner.USAGE_REASON, self.root / "runs" / "101-aaaaaaaaaaaa"),
            self.output,
        )

        self.assertEqual(self.tick(), 0)

        entry = self.state().prs[101]
        self.assertEqual(self.state().failures, {})
        self.assertEqual((entry.reviewed_commit, entry.attempts, entry.blocked), (pr.head, 0, 0))
        self.assertEqual(self.h.sent[1], format_review_ready(pr, ONE_LINE, Counts(0, 0, 0), URL))

    def test_watch_pr_that_keeps_hitting_a_blocker_is_charged_and_stops(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.outcomes = ["blocked"] * 10

        progress = []
        for _ in range(7):
            self.assertEqual(self.tick(), 0)
            entry = self.state().prs[101]
            progress.append((entry.blocked, entry.attempts))

        self.assertEqual(progress, [(1, 0), (2, 0), (2, 1), (2, 2), (2, 3), (2, 3), (2, 3)])
        self.assertEqual(len(self.h.prepared), 5)
        self.assertEqual(self.h.sent, [format_failure(None, runner.USAGE_REASON), format_failure(101, runner.USAGE_REASON)])
        self.assertEqual(self.state().failures["claude"].count, 5)

        self.h.prs = [make_pr(head="b" * 12)]
        self.h.outcomes = []
        self.h.clock = NOW + timedelta(minutes=20)
        self.tick()
        entry = self.state().prs[101]
        self.assertEqual((entry.head, entry.blocked, entry.attempts), ("b" * 12, 0, 0))

    def test_prepare_failure_logs_the_expected_run_dir(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO)})
        self.h.prepare_error = WorkspaceError("git fetch failed")

        self.assertEqual(self.tick(), 0)

        self.assertIn("git fetch failed; run dir %s" % (self.root / "runs" / "101-aaaaaaaaaaaa"), self.output)

    def test_watch_prepare_error_counts_as_failed_attempt(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]
        self.save({101: PrEntry(head=pr.head, head_first_seen=LONG_AGO, attempts=2)})
        self.h.prepare_error = WorkspaceError("git fetch failed")

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.state().prs[101].attempts, 3)
        self.assertEqual((self.h.cleaned, self.h.contexts), ([], []))
        self.assertEqual(self.h.sent, [format_failure(101, "git fetch failed")])

    def test_watch_auth_error_notifies_once(self) -> None:
        cases = [
            (BitbucketAuthError("Bitbucket rejected credentials (HTTP 401)"), "Bitbucket rejected credentials (HTTP 401)"),
            (SecretMissing("No Keychain item for service bitbucket-api-token"), "Bitbucket token missing from Keychain"),
        ]
        for error, reason in cases:
            with self.subTest(type(error).__name__):
                self.use_harness(self.root / type(error).__name__)
                self.h.list_error = error

                self.assertEqual(self.tick(), 0)
                self.assertEqual(self.tick(), 0)

                self.assertEqual(self.h.sent, [format_failure(None, reason)])
                self.assertEqual(self.state().failures["bitbucket"].count, 2)
                self.h.list_error = None
                self.assertEqual(self.tick(), 0)
                self.assertEqual(self.state().failures, {})

    def test_watch_transient_errors_notify_after_fifteen_ticks_then_go_quiet(self) -> None:
        self.h.list_error = BitbucketTransientError("Bitbucket HTTP 503")

        sent_counts = []
        for _ in range(17):
            self.assertEqual(self.tick(), 0)
            sent_counts.append(len(self.h.sent))

        self.assertEqual(sent_counts, [0] * 14 + [1, 1, 1])
        self.assertEqual(self.h.sent, [format_failure(None, "Bitbucket HTTP 503")])
        self.h.list_error = None
        self.assertEqual(self.tick(), 0)
        self.assertNotIn("bitbucket", self.state().failures)

    def test_watch_reviews_a_new_pr_on_its_first_tick(self) -> None:
        self.h.prs = [make_pr()]

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.state().prs[101].head_first_seen, int(NOW.timestamp()))
        self.assertEqual(self.h.prepared, [(101, "full", None)])

    def test_watch_exits_when_lock_held(self) -> None:
        self.h.data_dir.mkdir(parents=True)
        with open(str(self.h.data_dir / "watch.lock"), "a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.tick(), 0)

        self.assertIn("previous run still active", self.output)
        self.assertEqual(self.h.list_calls, 0)
        self.assertFalse(self.state_path.exists())

    def test_watch_unreadable_state_notifies_once_and_leaves_file(self) -> None:
        for name, content in (("json", "{oops"), ("fields", '{"prs": {"1": {"bogus": 1}}}')):
            with self.subTest(name):
                self.use_harness(self.root / name)
                self.h.prs = [make_pr()]
                self.h.data_dir.mkdir(parents=True)
                self.state_path.write_text(content)
                marker = self.h.data_dir / "state.corrupt-notified"

                self.assertEqual(self.tick(), 1)
                self.assertTrue(marker.exists())
                self.assertEqual(self.tick(), 1)

                self.assertEqual(self.state_path.read_text(), content)
                self.assertEqual(len(self.h.sent), 1)
                self.assertTrue(self.h.sent[0].startswith(format_failure(None, "state.json is unreadable: ")))
                self.assertTrue(self.h.sent[0].endswith("Fix or re-seed with pr-review seed --force."))
                self.assertEqual(self.h.list_calls, 0)
                self.save({})
                self.assertEqual(self.tick(), 0)
                self.assertFalse(marker.exists())

    def test_run_one_reviews_now(self) -> None:
        pr = make_pr()
        self.h.prs = [pr]

        self.assertEqual(self.call(lambda: run_one(101, None, self.h.deps())), 0)
        self.assertEqual(self.h.prepared, [(101, "full", None)])
        self.assertEqual(self.state().prs[101].reviewed_commit, pr.head)
        self.assertEqual(self.h.sent, [format_review_ready(pr, ONE_LINE, Counts(0, 0, 0), URL)])

        self.assertEqual(self.call(lambda: run_one(101, "d" * 40, self.h.deps())), 0)
        self.assertEqual(self.h.prepared[-1], (101, "incremental", "d" * 40))
        self.assertEqual(self.h.contexts[-1]["artifact_url"], URL)
        self.assertEqual(self.h.sent[-1], format_update(pr, Counts(0, 0, 0), 0, URL, False))

        self.h.outcomes = ["timed out after 30 minutes"]
        self.assertEqual(self.call(lambda: run_one(101, None, self.h.deps())), 1)
        self.assertEqual(self.state().prs[101].attempts, 1)


class SeedTest(RunnerTestCase):
    def test_seed_records_ready_at_head_and_drafts_without_baseline(self) -> None:
        self.h.prs = [make_pr(101, "a" * 12), make_pr(102, "b" * 12, draft=True)]

        self.assertEqual(self.call(lambda: seed(False, self.h.deps())), 0)

        stamp = int(NOW.timestamp())
        self.assertEqual(
            self.state().prs,
            {101: PrEntry("a" * 12, stamp, None, "a" * 12, 0, None), 102: PrEntry("b" * 12, stamp, None, None, 0, None)},
        )
        self.assertEqual(self.h.sent, [])
        self.assertIn("seeded 2 PRs", self.output)
        self.h.clock = NOW + timedelta(minutes=20)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.h.prepared, [])

    def test_seed_refuses_existing_state_without_force(self) -> None:
        self.save({101: PrEntry("a" * 12, 5)})
        before = self.state_path.read_text()
        self.h.prs = [make_pr()]

        self.assertEqual(self.call(lambda: seed(False, self.h.deps())), 1)

        self.assertEqual(self.state_path.read_text(), before)
        self.assertEqual(self.h.list_calls, 0)

    def test_seed_force_keeps_reviews_and_rebaselines(self) -> None:
        self.save({
            101: PrEntry("a" * 12, 5, reviewed_commit="r" * 12, attempts=2, artifact_url=URL),
            555: PrEntry("e" * 12, 5),
        })
        self.h.prs = [make_pr(101, "n" * 12), make_pr(102, "d" * 12, draft=True)]

        self.assertEqual(self.call(lambda: seed(True, self.h.deps())), 0)

        stamp = int(NOW.timestamp())
        entries = self.state().prs
        self.assertEqual(entries[101], PrEntry("n" * 12, stamp, "r" * 12, "n" * 12, 0, URL))
        self.assertEqual(entries[102], PrEntry("d" * 12, stamp, None, None, 0, None))
        self.assertEqual(entries[555], PrEntry("e" * 12, 5))
        self.assertEqual(self.h.sent, [])

    def test_seed_force_replaces_unreadable_state(self) -> None:
        self.h.data_dir.mkdir(parents=True)
        self.state_path.write_text("{oops")
        marker = self.h.data_dir / "state.corrupt-notified"
        marker.write_text("")
        self.h.prs = [make_pr()]

        self.assertEqual(self.call(lambda: seed(True, self.h.deps())), 0)

        self.assertEqual(list(self.state().prs), [101])
        self.assertFalse(marker.exists())


class FinishTest(RunnerTestCase):
    def test_finish_moves_page_and_updates_entry(self) -> None:
        pr = make_pr(head="c" * 12)
        ws = make_ws(pr, self.root)
        context_path = write_context(ws, [], None, "manual")
        page = apply_review(None, json.loads(context_path.read_text()), review([finding()]), NOW)
        (ws.run_dir / "page.json").write_text(json.dumps(page))
        state = State(prs={101: PrEntry("c" * 12, 5, reviewed_commit="b" * 12, baseline_head="c" * 12, attempts=2)}, failures={})

        result = finish(pr, ws.run_dir, URL, state, NOW, self.h.data_dir)

        self.assertEqual(result, page)
        self.assertFalse((ws.run_dir / "page.json").exists())
        self.assertEqual(json.loads((self.h.data_dir / "pages" / "101.json").read_text()), page)
        self.assertEqual(state.prs[101], PrEntry("c" * 12, 5, "c" * 12, None, 0, URL))

    def test_finish_adds_missing_entry_and_rejects_foreign_urls(self) -> None:
        pr = make_pr()
        ws = make_ws(pr, self.root)
        context_path = write_context(ws, [], None, "manual")
        page = apply_review(None, json.loads(context_path.read_text()), review(), NOW)
        (ws.run_dir / "page.json").write_text(json.dumps(page))
        state = State(prs={}, failures={})

        for bad in ("https://example.test/x", "https://claude.ai/artifact/abc\n"):
            with self.assertRaises(ValueError):
                finish(pr, ws.run_dir, bad, state, NOW, self.h.data_dir)
        self.assertEqual(state.prs, {})
        finish(pr, ws.run_dir, URL, state, NOW, self.h.data_dir)

        self.assertEqual(state.prs[101], PrEntry("a" * 12, int(NOW.timestamp()), "a" * 12, None, 0, URL))

    def test_finish_requires_the_url_already_on_record(self) -> None:
        pr = make_pr()
        ws = make_ws(pr, self.root)
        context_path = write_context(ws, [], URL, "manual")
        (ws.run_dir / "page.json").write_text(json.dumps(apply_review(None, json.loads(context_path.read_text()), review(), NOW)))
        state = State(prs={101: PrEntry("a" * 12, 5, reviewed_commit="b" * 12, artifact_url=URL)}, failures={})

        with self.assertRaisesRegex(ValueError, "^published to an unexpected URL$"):
            finish(pr, ws.run_dir, "https://claude.ai/artifact/other", state, NOW, self.h.data_dir)

        self.assertEqual((state.prs[101].artifact_url, state.prs[101].reviewed_commit), (URL, "b" * 12))
        self.assertTrue((ws.run_dir / "page.json").exists())
        finish(pr, ws.run_dir, URL, state, NOW, self.h.data_dir)
        self.assertEqual(state.prs[101].reviewed_commit, pr.head)

    def test_finish_rejects_a_page_for_another_head_or_pr(self) -> None:
        pr = make_pr()
        ws = make_ws(pr, self.root)
        context = json.loads(write_context(ws, [], None, "manual").read_text())
        state = State(prs={}, failures={})
        for name, page in (
            ("head", apply_review(None, dict(context, head="9" * 40), review(), NOW)),
            ("pr", apply_review(None, dict(context, pr=dict(context["pr"], id=999)), review(), NOW)),
            ("empty", {"pr": context["pr"], "sections": [], "fixed": {}}),
        ):
            with self.subTest(name):
                (ws.run_dir / "page.json").write_text(json.dumps(page))
                with self.assertRaises(ValueError):
                    finish(pr, ws.run_dir, URL, state, NOW, self.h.data_dir)
                self.assertTrue((ws.run_dir / "page.json").exists())
                self.assertFalse((self.h.data_dir / "pages").exists())
                self.assertEqual(state.prs, {})


class ParseResultTest(unittest.TestCase):
    def test_result_requires_published_status_and_claude_url(self) -> None:
        for url in (URL, "https://claude.ai/code/artifact/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"):
            with self.subTest(url=url):
                self.assertEqual(
                    parse_result({"status": "published", "artifact_url": url, "error": ""}), ClaudeResult(True, url, None)
                )
        for data in ({"status": "published", "artifact_url": "https://evil.test/x"}, {"status": "done"}, ["published"]):
            with self.subTest(data=data):
                result = parse_result(data)
                self.assertFalse(result.ok)
                self.assertTrue(result.error.startswith("invalid result.json: "), result.error)

    def test_artifact_url_must_be_a_plain_claude_artifact_link(self) -> None:
        for url in (
            "https://claude.ai/artifact/abc\n", "https://claude.ai/artifact/abc?x=1", "https://claude.ai/artifact/abc#x",
            "https://claude.ai.evil.test/artifact/abc", "http://claude.ai/artifact/abc", "https://claude.ai/chat/abc",
            "https://claude.ai/artifact/../x", "https://claude.ai/artifact/", "https://claude.ai/artifact/a|b>",
            "https://claude.ai/artifact/abc/x", None, 5,
        ):
            with self.subTest(url=url):
                result = parse_result({"status": "published", "artifact_url": url, "error": ""})
                self.assertEqual((result.ok, result.artifact_url), (False, None))

    def test_session_errors_reach_the_dm_as_a_fixed_reason_and_the_log_verbatim(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = parse_result({"status": "error", "artifact_url": "", "error": "see <https://evil.test|here>\x1b[2J"})

        self.assertEqual(result, ClaudeResult(False, None, "the review session reported an error; see the log"))
        self.assertIn("the review session reported: see <https://evil.test|here>[2J", out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())


class SessionNameTest(unittest.TestCase):
    def test_live_sessions_use_the_swept_prefix_and_test_sessions_do_not(self) -> None:
        self.assertEqual(runner.session_name(2844), "pr-review-2844")
        self.assertIsNotNone(LIVE_SWEEP.fullmatch(runner.session_name(2844)))
        self.assertIsNone(LIVE_SWEEP.fullmatch(TEST_PREFIX + "990001"))
        self.assertIsNone(re.match(r"^\s*(\d+\.%s)\s" % (re.escape(runner.SESSION_PREFIX) + r"\d+"), "\t1.%s990001\t" % TEST_PREFIX))


class DashboardSyncTest(unittest.TestCase):
    def test_starts_a_detached_fixed_prompt_claude_run_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner.subprocess, "Popen") as popen, \
                mock.patch.object(runner, "disabled_plugins", return_value={"x@m": False}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            runner.start_dashboard_sync("trig_abc", Path(tmp) / "sync.log")
        popen.assert_called_once()
        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(command[:3], [runner.CLAUDE_BIN, "-p", runner.DASHBOARD_PROMPT % "trig_abc"])
        self.assertIn('"run"', command[2])
        self.assertIn("trig_abc", command[2])
        self.assertEqual(command[command.index("--allowedTools") + 1], "RemoteTrigger")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(json.loads(command[command.index("--settings") + 1])["enabledPlugins"], {"x@m": False})
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"][runner.RESTRICT_ENV], "1")
        popen.return_value.wait.assert_not_called()
        self.assertIn("started the PR review dashboard sync", out.getvalue())

    def test_no_trigger_configured_starts_nothing(self) -> None:
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            runner.start_dashboard_sync("", Path("/nonexistent/sync.log"))
        popen.assert_not_called()

    def test_a_failed_start_is_logged_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner.subprocess, "Popen", side_effect=OSError("no claude")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            runner.start_dashboard_sync("trig_abc", Path(tmp) / "sync.log")
        self.assertIn("could not start the dashboard sync", out.getvalue())


class NoRunLimitTest(unittest.TestCase):
    def test_a_long_running_session_is_never_timed_out(self) -> None:
        clock = iter(range(0, 10 ** 9, 3600))
        listings = [["7.pr-review-1"]] * 50 + [[]]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(runner.time, "sleep"), \
                mock.patch.object(runner, "find_sessions", side_effect=listings):
            result = runner._wait_for_result(Path(tmp) / "result.json", "7.pr-review-1", "pr-review-1", None, 5.0)
        self.assertEqual(result, ClaudeResult(False, None, "Claude session exited without a result"))

    def run_long(self, alive_polls: int, calls: List[int]) -> ClaudeResult:
        clock = iter(range(0, 10 ** 9, 600))
        listings = [["7.pr-review-1"]] * alive_polls + [[]]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(runner.time, "sleep"), \
                mock.patch.object(runner, "find_sessions", side_effect=listings):
            return runner._wait_for_result(
                Path(tmp) / "result.json", "7.pr-review-1", "pr-review-1", None, 5.0,
                on_long_run=lambda: calls.append(1), long_run_after=3600,
            )

    def test_long_run_notice_fires_once_after_an_hour(self) -> None:
        calls: List[int] = []
        self.run_long(20, calls)
        self.assertEqual(calls, [1])

    def test_long_run_notice_does_not_fire_before_an_hour(self) -> None:
        calls: List[int] = []
        self.run_long(5, calls)
        self.assertEqual(calls, [])

    def test_run_claude_has_no_default_time_limit(self) -> None:
        self.assertIsNone(inspect.signature(run_claude).parameters["timeout"].default)
        self.assertFalse(hasattr(runner, "RUN_TIMEOUT"))


class SessionQuitTest(unittest.TestCase):
    def test_quit_waits_until_the_session_has_gone_then_ends_its_window_processes(self) -> None:
        listings = [["7.pr-review-1"], ["7.pr-review-1"], ["7.pr-review-1"], []]
        with mock.patch.object(runner, "_screen") as screen, \
                mock.patch.object(runner, "find_sessions", side_effect=listings) as find, \
                mock.patch.object(runner, "_descendants", return_value=[8, 9]) as descendants, \
                mock.patch.object(runner, "_terminate") as terminate:
            runner.quit_sessions("pr-review-1")
        descendants.assert_called_once_with(7)
        screen.assert_called_once_with("-S", "7.pr-review-1", "-X", "quit")
        self.assertEqual(find.call_count, 4)
        terminate.assert_called_once_with([8, 9])

    def test_sweep_waits_until_each_session_has_gone(self) -> None:
        with mock.patch.object(runner, "_screen"), \
                mock.patch.object(runner, "_sessions", return_value=["7.pr-review-1"]), \
                mock.patch.object(runner, "find_sessions", side_effect=[["7.pr-review-1"], []]) as find, \
                mock.patch.object(runner, "_descendants", return_value=[]), \
                mock.patch.object(runner, "_terminate") as terminate, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.sweep_sessions(), ["7.pr-review-1"])
        self.assertEqual(find.call_count, 2)
        terminate.assert_called_once_with([])

    def test_descendants_walk_the_whole_process_tree(self) -> None:
        listing = "  1     0\n  7     1\n  8     7\n  9     8\n 10     1\n 11     9\nbad line\n"
        with mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, listing, "")):
            self.assertEqual(sorted(runner._descendants(7)), [8, 9, 11])

    def test_terminate_escalates_to_sigkill_and_skips_what_it_cannot_signal(self) -> None:
        sent: List[Tuple[int, int]] = []
        stubborn = {20}

        def kill(pid: int, signum: int) -> None:
            if pid == 30:
                raise PermissionError
            if pid == 40 or (signum == 0 and pid not in stubborn):
                raise ProcessLookupError
            sent.append((pid, signum))

        with mock.patch.object(runner.os, "kill", side_effect=kill), mock.patch.object(runner, "TERMINATE_WAIT", 0.3):
            runner._terminate([1, 20, 30, 40, 50])

        self.assertNotIn(1, [pid for pid, _ in sent])
        self.assertIn((20, signal.SIGTERM), sent)
        self.assertIn((20, signal.SIGKILL), sent)
        self.assertIn((50, signal.SIGTERM), sent)
        self.assertNotIn((50, signal.SIGKILL), sent)

    def test_launch_wait_ignores_a_session_left_from_before_the_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "find_sessions", side_effect=[["7.pr-review-1"], ["7.pr-review-1", "9.pr-review-1"]]):
            session = runner._await_session("pr-review-1", Path(tmp) / "result.json", {"7.pr-review-1"})
        self.assertEqual(session, "9.pr-review-1")


class FindBlockerTest(unittest.TestCase):
    def test_known_blockers_map_to_fixed_reasons(self) -> None:
        cases = [
            ("  Not logged in · Please run /login", runner.LOGIN_REASON),
            ("API Error: 401 {\"type\":\"authentication_error\"}", runner.LOGIN_REASON),
            ("Select login method:\n 1. Claude account with subscription", runner.LOGIN_REASON),
            ("Invalid API key · Fix external API key", runner.LOGIN_REASON),
            ("You've hit your limit · resets 5pm", runner.USAGE_REASON),
            ("You've hit your session limit · resets 5pm", runner.USAGE_REASON),
            ("You've hit your weekly limit · resets Oct 30, 9am", runner.USAGE_REASON),
            ("You've hit your Opus limit · resets Oct 30 · progress saved", runner.USAGE_REASON),
            ("You've hit your monthly spend limit.", runner.USAGE_REASON),
            ("You've hit your team's shared budget. /model to switch models.", runner.USAGE_REASON),
            ("  You've hit your session\n  limit · resets 5pm", runner.USAGE_REASON),
            ("You've hit y\nour weekly limit", runner.USAGE_REASON),
            ("Usage limit reached · continuing automatically at 5pm", runner.USAGE_REASON),
            ("Quick safety check\n Yes, I trust this folder\n No, exit", runner.TRUST_REASON),
            ("Login expired · Please run\n/login", runner.LOGIN_REASON),
            ("Login expired · Please run /lo\ngin", runner.LOGIN_REASON),
        ]
        for screen, reason in cases:
            with self.subTest(screen=screen):
                self.assertEqual(runner.find_blocker(screen), reason)

    def test_only_the_bottom_ui_region_is_searched(self) -> None:
        bottom = ["  status line %d" % i for i in range(runner.BLOCKER_REGION_LINES - 1)]
        inside = "\n\n".join(["Read(app/Auth.php)", "Please run /login"] + bottom)
        above = "\n\n".join(["Write(review.json) usage limit reached", "Please run /login", "filler"] + bottom)
        hit_inside = "\n".join(["Read(app/Auth.php)", "You've hit your session limit · resets 5pm"] + bottom)
        hit_above = "\n".join(["You've hit your session limit · resets 5pm", "filler"] + bottom)

        self.assertEqual(runner.find_blocker(inside), runner.LOGIN_REASON)
        self.assertIsNone(runner.find_blocker(above))
        self.assertEqual(runner.find_blocker(hit_inside), runner.USAGE_REASON)
        self.assertIsNone(runner.find_blocker(hit_above))
        padded = inside.replace("status line 0", "status line 0\n" + "\x00" * 80 + "\n  \x00\x00  ")
        self.assertEqual(runner.find_blocker(padded), runner.LOGIN_REASON)

    def test_harmless_text_is_not_a_blocker(self) -> None:
        for screen in (
            "Your login expires in 1 day · run /login to renew",
            "Read 401 lines",
            "Concurrent subagent limit reached",
            "Context limit reached",
            "Server is temporarily limiting requests (not your usage limit)",
            "PR #2844 returns HTTP 401 when the token expires",
            "A retry loop here could hit your API rate limiter",
        ):
            with self.subTest(screen=screen):
                self.assertIsNone(runner.find_blocker(screen))


class ClaudeSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.record = self.root / "record.json"
        self.names: List[str] = []
        patcher = mock.patch.dict(os.environ, {"FAKE_CLAUDE_RECORD": str(self.record), "FAKE_CLAUDE_URL": URL})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(runner.RESTRICT_ENV, None)
        self.user_settings = self.root / "settings.json"
        self.user_settings.write_text(json.dumps({"enabledPlugins": {"pr-review-toolkit@m": True, "figma@m": False}}))
        self.installed = self.root / "installed_plugins.json"
        self.installed.write_text(json.dumps({"plugins": {"superpowers@m": [{}], "figma@m": [{}]}}))
        sources = mock.patch(
            "prreview.runner.PLUGIN_SOURCES", ((self.user_settings, "enabledPlugins"), (self.installed, "plugins"))
        )
        sources.start()
        self.addCleanup(sources.stop)
        prefix = mock.patch("prreview.runner.SESSION_PREFIX", TEST_PREFIX)
        prefix.start()
        self.addCleanup(prefix.stop)

    def tearDown(self) -> None:
        for name in self.names:
            self.assertIsNone(LIVE_SWEEP.fullmatch(name), name)
            kill_sessions(name)
        subprocess.run(["/usr/bin/pkill", "-f", str(self.root)], capture_output=True)

    def test_run_claude_leaves_no_window_process_behind(self) -> None:
        context_path, ws = self.workspace(990030)

        self.assertEqual(self.run_fake(context_path, ws, "ok"), ClaudeResult(True, URL, None))

        leftover = subprocess.run(["/usr/bin/pgrep", "-f", str(ws.run_dir)], capture_output=True, text=True).stdout
        self.assertEqual(leftover.split(), [])

    def workspace(self, pr_id: int) -> Tuple[Path, Workspace]:
        self.names.append(TEST_PREFIX + str(pr_id))
        ws = make_ws(make_pr(pr_id), self.root)
        self.assertEqual(runner.session_name(pr_id), TEST_PREFIX + str(pr_id))
        return write_context(ws, [], None, "watcher"), ws

    def run_fake(
        self, context_path: Path, ws: Workspace, mode: str, timeout: int = 10, interval: float = 60.0, stall: float = 300.0
    ) -> ClaudeResult:
        os.environ["FAKE_CLAUDE_MODE"] = mode
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = run_claude(
                context_path, ws, claude_bin=FAKE_CLAUDE, timeout=timeout, poll_interval=0.2,
                hardcopy_interval=interval, stall_seconds=stall,
            )
        self.output = out.getvalue()
        return result

    def start_session(self, name: str) -> None:
        self.names.append(name)
        subprocess.run(
            ["/usr/bin/screen", "-ln", "-dmS", name, "/bin/sleep", "60"],
            env=dict(os.environ, TERM="xterm-256color"), stdin=subprocess.DEVNULL, check=True,
        )

    def test_run_claude_launches_hidden_session_with_expected_args(self) -> None:
        context_path, ws = self.workspace(990001)

        with mock.patch("prreview.runner.subprocess.run", wraps=subprocess.run) as run:
            result = self.run_fake(context_path, ws, "ok")

        self.assertEqual(result, ClaudeResult(True, URL, None))
        self.assertEqual({call.kwargs.get("timeout") for call in run.call_args_list}, {15})
        recorded = json.loads(self.record.read_text())
        run_dir = str(ws.run_dir)
        rule = "//" + run_dir.lstrip("/")
        tree = "//" + str(ws.worktree).lstrip("/")
        render = "/usr/bin/python3 -I %s render %s/context.json %s/review.json" % (BIN, run_dir, run_dir)
        allowed = (
            "Skill(pr-auto-review) Agent Artifact Read(%s/.claude/CLAUDE.md) "
            "Grep(%s/**) Glob(%s/**) Bash(%s) "
            "Write(%s/review.json) Edit(%s/review.json) Write(%s/result.json) Edit(%s/result.json)"
            % (HOME_RULE, tree, tree, render, rule, rule, rule, rule)
        )
        self.assertEqual(json.loads(context_path.read_text())["render_command"], render)
        self.assertNotIn("*", render)
        denied = (
            "Read(%s/.ssh/**) Read(%s/.aws/**) "
            "Read(%s/.claude/.credentials.json) Read(//srv/acme/widgets/**)" % (HOME_RULE, HOME_RULE, HOME_RULE)
        )
        self.assertNotIn("Bash(git", allowed)
        self.assertEqual(recorded["argv"], [
            "/pr-auto-review %s" % context_path,
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--setting-sources", "user",
            "--settings", "%s/session-settings.json" % run_dir,
            "--add-dir", run_dir,
            "--add-dir", str(Path.home() / ".claude/projects/-srv-acme-widgets/memory"),
            "--allowedTools", allowed,
            "--disallowedTools", denied,
        ])
        self.assertEqual(Path(recorded["cwd"]).resolve(), ws.worktree.resolve())
        self.assertEqual(recorded["restrict"], "1")
        self.assertNotIn(runner.RESTRICT_ENV, os.environ)
        self.assertGreater((ws.run_dir / "screen.txt").stat().st_size, 0)
        self.assertEqual(screen_sessions(TEST_PREFIX + "990001"), [])

    def test_session_settings_disable_every_known_plugin_and_hook_the_guard(self) -> None:
        context_path, ws = self.workspace(990007)

        self.assertEqual(self.run_fake(context_path, ws, "ok"), ClaudeResult(True, URL, None))

        settings = json.loads((ws.run_dir / "session-settings.json").read_text())
        guard = "/usr/bin/python3 -I %s guard %s || exit 2" % (BIN, ws.run_dir)
        self.assertEqual(settings, {
            "enabledPlugins": {"figma@m": False, "pr-review-toolkit@m": False, "superpowers@m": False},
            "hooks": {"PreToolUse": [{"matcher": "Artifact|Agent", "hooks": [
                {"type": "command", "command": guard, "onFailure": "block", "timeout": 30},
            ]}]},
            "disableAllHooks": False,
            "statusLine": {"type": "command", "command": "true"},
        })
        self.assertEqual(runner.PLUGIN_SOURCES[0][0], self.user_settings)
        spaced = replace(ws, run_dir=self.root / "run dir; touch x")
        self.assertEqual(shlex.split(runner.guard_command(spaced.run_dir))[4], str(spaced.run_dir))

    def test_unreadable_plugin_list_fails_closed_without_launching(self) -> None:
        context_path, ws = self.workspace(990008)
        self.user_settings.write_text(json.dumps({"enabledPlugins": ["pr-review-toolkit@m"]}))

        result = self.run_fake(context_path, ws, "ok")

        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("could not write the session settings: "), result.error)
        self.assertFalse(self.record.exists())
        self.assertEqual(screen_sessions(TEST_PREFIX + "990008"), [])

    def test_session_allows_only_the_review_skill_and_scoped_reads(self) -> None:
        ws = make_ws(make_pr(), self.root)
        args = shlex.split(runner.claude_command(Path("context.json"), ws, "claude", ws.run_dir / "s.json"))
        allowed = args[args.index("--allowedTools") + 1].split(" ")
        denied = args[args.index("--disallowedTools") + 1].split(" ")

        for bare in ("Read", "Grep", "Glob", "Skill", "Bash", "Write", "Edit"):
            self.assertNotIn(bare, allowed)
        self.assertIn("Skill(pr-auto-review)", allowed)
        self.assertEqual([rule for rule in allowed if rule.startswith("Read(")], ["Read(%s/.claude/CLAUDE.md)" % HOME_RULE])
        tree = "//" + str(ws.worktree).lstrip("/")
        self.assertEqual(
            [rule for rule in allowed if rule.startswith(("Grep(", "Glob("))], ["Grep(%s/**)" % tree, "Glob(%s/**)" % tree]
        )
        for path in (".ssh/**", ".aws/**", ".claude/.credentials.json"):
            self.assertIn("Read(%s/%s)" % (HOME_RULE, path), denied)
        self.assertIn("Read(//srv/acme/widgets/**)", denied)
        self.assertEqual(args[args.index("--settings") + 1], str(ws.run_dir / "s.json"))

    def test_render_rule_is_exactly_the_context_render_command(self) -> None:
        linked = self.root / "linked-runs"
        linked.symlink_to(self.root / "runs", target_is_directory=True)
        ws = make_ws(make_pr(), self.root)
        for name, run_dir in (("plain", ws.run_dir), ("through a symlink", linked / ws.run_dir.name)):
            with self.subTest(name):
                ws = replace(ws, run_dir=run_dir)
                context = json.loads(write_context(ws, [], None, "watcher").read_text())
                allowed = shlex.split(runner.claude_command(Path("context.json"), ws, "claude", Path("s.json")))
                rules = allowed[allowed.index("--allowedTools") + 1]

                self.assertEqual(re.findall(r"Bash\(([^)]*)\)", rules), [context["render_command"]])
                self.assertNotIn("*", context["render_command"])

    def test_session_write_rules_stay_outside_claude_config_dirs(self) -> None:
        ws = replace(make_ws(make_pr(), self.root), run_dir=RUNS_DIR / "101-aaaaaaaaaaaa")

        rules = re.findall(r"(?:Write|Edit)\(([^)]*)\)", runner.claude_command(Path("context.json"), ws, "claude", Path("s.json")))

        self.assertEqual(len(rules), 4)
        for rule in rules:
            self.assertNotIn("/.claude/", rule)
        self.assertEqual(Deps().runs_dir, RUNS_DIR)
        self.assertEqual(inspect.signature(workspace.prepare).parameters["runs_dir"].default, RUNS_DIR)

    def test_run_claude_quits_leftover_session_and_ignores_stale_result(self) -> None:
        context_path, ws = self.workspace(990002)
        self.start_session(TEST_PREFIX + "990002")
        self.start_session(TEST_PREFIX + "9900020")
        stale = {"status": "published", "artifact_url": "https://claude.ai/artifact/stale", "error": ""}
        (ws.run_dir / "result.json").write_text(json.dumps(stale))
        (ws.run_dir / "review.json").write_text("{}")
        (ws.run_dir / "page.html").write_text("old")

        result = self.run_fake(context_path, ws, "ok")

        self.assertEqual(result, ClaudeResult(True, URL, None))
        recorded = json.loads(self.record.read_text())
        self.assertEqual(len(re.findall(r"\.%s990002\s" % re.escape(TEST_PREFIX), recorded["screen_ls"])), 1)
        self.assertEqual(screen_sessions(TEST_PREFIX + "990002"), [])
        self.assertEqual(len(screen_sessions(TEST_PREFIX + "9900020")), 1)
        self.assertFalse((ws.run_dir / "review.json").exists())
        self.assertFalse((ws.run_dir / "page.html").exists())

    def test_screen_timeout_reads_as_no_sessions(self) -> None:
        expired = subprocess.TimeoutExpired(["/usr/bin/screen"], 15)
        with mock.patch("prreview.runner.subprocess.run", side_effect=expired):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(find_sessions(TEST_PREFIX + "990099"), [])

    def test_run_claude_tolerates_a_session_that_lists_late(self) -> None:
        context_path, ws = self.workspace(990006)
        real = runner.find_sessions
        misses = {"left": 3}

        def late(name: str) -> List[str]:
            if misses["left"] > 0:
                misses["left"] -= 1
                return []
            return real(name)

        with mock.patch("prreview.runner.find_sessions", side_effect=late):
            result = self.run_fake(context_path, ws, "ok")

        self.assertEqual(result, ClaudeResult(True, URL, None))
        self.assertEqual(misses["left"], 0)
        self.assertEqual(screen_sessions(TEST_PREFIX + "990006"), [])

    def test_sweep_quits_every_matching_session(self) -> None:
        self.start_session(TEST_PREFIX + "990010")
        self.start_session(TEST_PREFIX + "990011")
        self.start_session(TEST_PREFIX + "x990012")
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            swept = sweep_sessions(TEST_PREFIX)

        self.assertEqual(sorted(s.split(".", 1)[1] for s in swept), [TEST_PREFIX + "990010", TEST_PREFIX + "990011"])
        self.assertEqual(screen_sessions(TEST_PREFIX + "990010") + screen_sessions(TEST_PREFIX + "990011"), [])
        self.assertEqual(len(screen_sessions(TEST_PREFIX + "x990012")), 1)
        self.assertEqual(out.getvalue().count("quit leftover screen session"), 2)

    def test_run_claude_session_exit_without_result_or_bad_json_is_failure(self) -> None:
        cases = ((990003, "exit", "Claude session exited without a result"), (990004, "bad", "invalid result.json: "))
        for pr_id, mode, prefix in cases:
            with self.subTest(mode):
                context_path, ws = self.workspace(pr_id)

                result = self.run_fake(context_path, ws, mode)

                self.assertEqual((result.ok, result.artifact_url), (False, None))
                self.assertTrue(result.error.startswith(prefix), result.error)
                self.assertEqual(screen_sessions(TEST_PREFIX + str(pr_id)), [])

    def test_blocked_session_quits_early_as_a_blocker(self) -> None:
        context_path, ws = self.workspace(990020)
        started = time.monotonic()

        result = self.run_fake(context_path, ws, "blocked", timeout=30, interval=0.5, stall=60)

        self.assertEqual(result, ClaudeResult(False, None, runner.LOGIN_REASON, blocker=True))
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("Please run /login", (ws.run_dir / "screen.txt").read_text(errors="replace"))
        self.assertEqual(screen_sessions(TEST_PREFIX + "990020"), [])

    def test_idle_session_with_harmless_notices_is_reported_stalled(self) -> None:
        context_path, ws = self.workspace(990021)

        result = self.run_fake(context_path, ws, "stall", timeout=30, interval=0.5, stall=1.5)

        self.assertEqual(result, ClaudeResult(False, None, "the review session stalled"))
        self.assertIn("run /login to renew", (ws.run_dir / "screen.txt").read_text(errors="replace"))
        self.assertEqual(screen_sessions(TEST_PREFIX + "990021"), [])

    def test_blocker_text_in_the_transcript_body_is_a_stall_not_a_blocker(self) -> None:
        context_path, ws = self.workspace(990024)

        result = self.run_fake(context_path, ws, "body_blocker", timeout=30, interval=0.5, stall=1.5)

        self.assertEqual(result, ClaudeResult(False, None, "the review session stalled"))
        self.assertIn("usage limit reached", (ws.run_dir / "screen.txt").read_text(errors="replace"))

    def test_busy_session_that_mentions_a_blocker_keeps_running(self) -> None:
        context_path, ws = self.workspace(990022)

        result = self.run_fake(context_path, ws, "busy", timeout=30, interval=0.5, stall=60)

        self.assertEqual(result, ClaudeResult(True, URL, None))

    def test_run_logs_its_run_dir_and_screen_path(self) -> None:
        context_path, ws = self.workspace(990023)

        self.run_fake(context_path, ws, "exit")

        self.assertIn("PR #990023 run dir %s; screen %s" % (ws.run_dir, ws.run_dir / "screen.txt"), self.output)

    def test_run_claude_timeout_quits_session_and_reports(self) -> None:
        context_path, ws = self.workspace(990005)
        started = time.monotonic()

        result = self.run_fake(context_path, ws, "hang", timeout=3)

        self.assertEqual(result, ClaudeResult(False, None, "timed out after 3 seconds"))
        self.assertLess(time.monotonic() - started, 8)
        self.assertGreater((ws.run_dir / "screen.txt").stat().st_size, 0)
        self.assertEqual(screen_sessions(TEST_PREFIX + "990005"), [])


class CliTest(RunnerTestCase):
    def cli(self, *argv: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), self.h.deps())
        return rc, out.getvalue(), err.getvalue()

    def test_cli_sync_dashboard_starts_the_sync(self) -> None:
        rc, _, _ = self.cli("sync-dashboard")
        self.assertEqual((rc, self.h.syncs), (0, 1))

    def test_cli_list_prints_sorted_tab_separated_rows(self) -> None:
        self.h.prs = [make_pr(205, title="Second"), make_pr(17, draft=True, title="First")]

        self.assertEqual(self.cli("list"), (0, "17\tdraft\tFirst\n205\tready\tSecond\n", ""))

        self.h.list_error = BitbucketAuthError("Bitbucket rejected credentials (HTTP 401)")
        rc, out, err = self.cli("list")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("Bitbucket rejected credentials (HTTP 401)", err)

    def test_cli_render_writes_page_or_rejects_invalid_review(self) -> None:
        ws = make_ws(make_pr(), self.root)
        context_path = write_context(ws, [], None, "watcher")
        review_path = ws.run_dir / "review.json"

        for content, message in (
            (json.dumps(dict(review(), summary="")), "summary must not be empty"),
            ("{not json", "not valid JSON"),
        ):
            with self.subTest(message):
                review_path.write_text(content)
                rc, out, err = self.cli("render", str(context_path), str(review_path))
                self.assertEqual((rc, out), (2, ""))
                self.assertIn(message, err)
                self.assertFalse((ws.run_dir / "page.json").exists())

        review_path.write_text(json.dumps(review([finding()])))
        rc, out, _ = self.cli("render", str(context_path), str(review_path))

        self.assertEqual((rc, out), (0, "%s\n" % (ws.run_dir / "page.html")))
        page = json.loads((ws.run_dir / "page.json").read_text())
        self.assertEqual(page["sections"][0]["at"], NOW.isoformat(timespec="seconds"))
        self.assertIn("<!doctype html>", (ws.run_dir / "page.html").read_text())

    def test_cli_render_only_writes_inside_a_run_dir_with_its_review(self) -> None:
        ws = make_ws(make_pr(), self.root)
        context_path = write_context(ws, [], None, "watcher")
        (ws.run_dir / "review.json").write_text(json.dumps(review()))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "context.json").write_text(context_path.read_text())
        (outside / "review.json").write_text(json.dumps(review()))
        nested = ws.run_dir / "nested"
        nested.mkdir()
        (nested / "context.json").write_text(context_path.read_text())
        (nested / "review.json").write_text(json.dumps(review()))
        other = make_ws(make_pr(102), self.root).run_dir / "review.json"
        other.write_text(json.dumps(review()))

        for name, context_arg, review_arg, message in (
            ("outside", outside / "context.json", outside / "review.json", "not a run directory"),
            ("nested", nested / "context.json", nested / "review.json", "not a run directory"),
            ("other review", context_path, other, "review must be in"),
            ("context name", ws.run_dir / "review.json", ws.run_dir / "review.json", "context.json and review.json"),
            ("review name", context_path, ws.run_dir / "context.json", "context.json and review.json"),
        ):
            with self.subTest(name):
                rc, out, err = self.cli("render", str(context_arg), str(review_arg))
                self.assertEqual((rc, out), (2, ""))
                self.assertIn(message, err)
        self.assertEqual(list(self.root.rglob("page.json")), [])

    def test_cli_prepare_picks_mode_from_state(self) -> None:
        pr = make_pr(head="c" * 12)
        self.h.prs = [pr]
        cases = [
            (None, ("full", None)),
            (PrEntry("c" * 12, 5, reviewed_commit="b" * 12, artifact_url=URL), ("incremental", "b" * 12)),
            (PrEntry("c" * 12, 5, reviewed_commit="c" * 12, artifact_url=URL), ("full", None)),
        ]
        for entry, expected in cases:
            with self.subTest(expected=expected):
                self.save({101: entry} if entry else {})

                rc, out, _ = self.cli("prepare", "101")

                self.assertEqual(rc, 0)
                self.assertEqual(self.h.prepared[-1], (101,) + expected)
                context = json.loads(Path(out.strip()).read_text())
                self.assertEqual(context["invoked_by"], "manual")
                self.assertEqual(context["artifact_url"], entry.artifact_url if entry else None)

    def test_cli_finish_records_review_and_cleans_up(self) -> None:
        pr = make_pr()
        ws = make_ws(pr, self.root)
        context_path = write_context(ws, [], None, "manual")
        page = apply_review(None, json.loads(context_path.read_text()), review([finding("critical"), finding()]), NOW)
        (ws.run_dir / "page.json").write_text(json.dumps(page))
        self.h.prs = [pr]

        rc, out, _ = self.cli("finish", str(context_path), "--artifact-url", URL)

        self.assertEqual(rc, 0)
        self.assertEqual(out, "%s\nCritical 1 · Should fix 0 · Nit 1\n" % ONE_LINE)
        entry = self.state().prs[101]
        self.assertEqual((entry.reviewed_commit, entry.artifact_url), (pr.head, URL))
        self.assertEqual(self.h.cleaned, [101])
        self.assertEqual(self.h.syncs, 1)

    def finished_run(self, head: str = "a" * 12) -> Tuple[Path, Workspace]:
        ws = make_ws(make_pr(head=head), self.root)
        context_path = write_context(ws, [], None, "manual")
        page = apply_review(None, json.loads(context_path.read_text()), review([finding()]), NOW)
        (ws.run_dir / "page.json").write_text(json.dumps(page))
        return context_path, ws

    def test_cli_finish_rejects_a_mismatched_run_dir_or_moved_head(self) -> None:
        context_path, ws = self.finished_run()
        context = json.loads(context_path.read_text())
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "context.json").write_text(context_path.read_text())
        cases = [
            ("outside runs", outside / "context.json", context, [make_pr()], "not a run directory"),
            ("head edited", context_path, dict(context, pr=dict(context["pr"], head="b" * 12)), [make_pr()], "does not match"),
            ("id edited", context_path, dict(context, pr=dict(context["pr"], id=102)), [make_pr(), make_pr(102)], "does not match"),
            ("head moved", context_path, context, [make_pr(head="c" * 12)], "head is now"),
        ]
        for name, path, data, prs, message in cases:
            with self.subTest(name):
                path.write_text(json.dumps(data))
                self.h.prs = prs

                rc, out, err = self.cli("finish", str(path), "--artifact-url", URL)

                self.assertEqual((rc, out), (1, ""))
                self.assertIn(message, err)
                self.assertFalse(self.state_path.exists())
                self.assertTrue((ws.run_dir / "page.json").exists())
                self.assertEqual(self.h.cleaned, [])

    def test_cli_finish_cleans_up_the_trusted_worktree_only(self) -> None:
        context_path, ws = self.finished_run()
        context = json.loads(context_path.read_text())
        context_path.write_text(json.dumps(dict(context, worktree="/srv/acme/widgets", run_dir="/tmp/elsewhere")))
        self.h.prs = [make_pr()]
        self.h.cleanup_error = WorkspaceError("worktree remove failed")

        rc, out, err = self.cli("finish", str(context_path), "--artifact-url", URL)

        self.assertEqual(rc, 0)
        self.assertEqual(self.h.cleaned_paths, [self.root / "worktrees" / "101"])
        self.assertIn("worktree remove failed", err)
        self.assertEqual(self.state().prs[101].reviewed_commit, "a" * 12)
        self.assertTrue((self.h.data_dir / "pages" / "101.json").exists())

    def test_cli_manual_commands_give_up_when_a_review_holds_the_lock(self) -> None:
        context_path, _ = self.finished_run()
        self.h.prs = [make_pr()]
        self.h.data_dir.mkdir(parents=True)
        with open(str(self.h.data_dir / "watch.lock"), "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch("prreview.cli.MANUAL_LOCK_WAIT", 0.3):
                results = [
                    self.cli("finish", str(context_path), "--artifact-url", URL),
                    self.cli("prepare", "101"),
                ]

        for result in results:
            self.assertEqual(result, (1, "", "A watcher review is running; try again in a few minutes.\n"))
        self.assertEqual((self.h.prepared, self.h.cleaned), ([], []))
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()

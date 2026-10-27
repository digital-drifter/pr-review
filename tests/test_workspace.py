import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path
from typing import List, Optional

from prreview.models import PullRequest
from prreview.workspace import (
    EXCLUDE_PATHSPECS, WorkspaceError, _git, _is_session_config_name, cleanup, prepare, prune_runs, write_context,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), result.stderr))
    return result.stdout.strip()


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.origin = root / "origin.git"
        self.dev = root / "dev"
        self.repo = root / "repo"
        self.data_dir = root / "data"
        self.runs_dir = root / "runs"
        self.worktrees_dir = root / "worktrees"
        subprocess.run(["git", "init", "--bare", "-b", "master", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.dev)], check=True, capture_output=True)
        git(self.dev, "checkout", "-b", "master")
        self.write(self.dev, "README.md", "hello\n")
        self.commit(self.dev, "init")
        git(self.dev, "push", "origin", "master")
        subprocess.run(["git", "clone", str(self.origin), str(self.repo)], check=True, capture_output=True)

    def write(self, base: Path, name: str, content: str) -> None:
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, base: Path, message: str) -> str:
        git(base, "add", "-A")
        git(base, "commit", "-m", message)
        return git(base, "rev-parse", "HEAD")

    def lines(self, count: int) -> str:
        return "".join("line %d\n" % i for i in range(count))

    def make_pr(self, head_sha: str, pr_id: int = 7) -> PullRequest:
        return PullRequest(
            id=pr_id, title="T", description="D", author="A", draft=False, head=head_sha[:12],
            source_branch="feature", destination_branch="master", link="https://example.test/pr/%d" % pr_id,
        )

    def prep(self, pr: PullRequest, mode: str = "full", base: Optional[str] = None):
        return prepare(pr, mode, base, repo=self.repo, runs_dir=self.runs_dir, worktrees_dir=self.worktrees_dir)

    def feature_branch(self) -> None:
        git(self.dev, "checkout", "-b", "feature")

    def push_feature(self) -> None:
        git(self.dev, "push", "-f", "origin", "feature")


class PrepareTest(WorkspaceTestCase):
    def test_full_mode_diffs_from_merge_base_with_destination(self) -> None:
        base_sha = git(self.dev, "rev-parse", "HEAD")
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(7))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        git(self.dev, "checkout", "master")
        self.write(self.dev, "other.txt", "x\n")
        self.commit(self.dev, "master moves")
        git(self.dev, "push", "origin", "master")

        ws = self.prep(self.make_pr(head))

        self.assertEqual(ws.mode, "full")
        self.assertFalse(ws.full_fallback)
        self.assertEqual(ws.base, base_sha)
        self.assertEqual(ws.head, head)
        self.assertEqual(ws.diff_command, "git diff %s %s -- %s" % (base_sha, head, " ".join(EXCLUDE_PATHSPECS)))
        self.assertEqual(ws.files_changed, 1)
        self.assertEqual(ws.changed_lines, 7)
        self.assertEqual(ws.commit_count, 1)
        self.assertEqual(ws.worktree, self.worktrees_dir / "7")
        self.assertEqual(ws.run_dir, self.runs_dir / ("7-" + head[:12]))
        self.assertTrue(ws.run_dir.is_dir())
        self.assertEqual(git(ws.worktree, "rev-parse", "HEAD"), head)

    def test_prepare_refreshes_a_reused_run_dir_so_prune_keeps_it(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(3))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        pr = self.make_pr(head)
        run_dir = self.runs_dir / ("7-" + head[:12])
        run_dir.mkdir(parents=True)
        (run_dir / "context.json").write_text("{}")
        (run_dir / "diff.patch").write_text("old")
        old = time.time() - 30 * 24 * 3600
        os.utime(str(run_dir), (old, old))

        ws = self.prep(pr)

        self.assertEqual(ws.run_dir, run_dir)
        self.assertGreater(run_dir.stat().st_mtime, time.time() - 60)
        self.assertEqual(prune_runs(self.runs_dir, time.time()), [])
        self.assertTrue(run_dir.is_dir())

    def test_incremental_excludes_commits_merged_from_destination(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        reviewed = self.commit(self.dev, "A")
        git(self.dev, "checkout", "master")
        self.write(self.dev, "big.txt", self.lines(3000))
        self.commit(self.dev, "big master change")
        git(self.dev, "push", "origin", "master")
        git(self.dev, "checkout", "feature")
        git(self.dev, "merge", "--no-edit", "master")
        self.write(self.dev, "app/b.php", self.lines(10))
        head = self.commit(self.dev, "B")
        self.push_feature()

        ws = self.prep(self.make_pr(head), "incremental", reviewed[:12])

        self.assertEqual(ws.mode, "incremental")
        self.assertFalse(ws.full_fallback)
        self.assertEqual(ws.base, reviewed)
        self.assertEqual(ws.changed_lines, 10)
        self.assertEqual(ws.commit_count, 1)
        self.assertIn("--no-merges", ws.diff_command)
        self.assertIn("^" + git(self.repo, "rev-parse", "origin/master"), ws.diff_command)
        self.assertIn("%s..%s" % (reviewed, head), ws.diff_command)

    def test_force_push_falls_back_to_full(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        old = self.commit(self.dev, "old")
        git(self.dev, "reset", "--hard", "master")
        self.write(self.dev, "app/c.php", self.lines(4))
        head = self.commit(self.dev, "rewritten")
        self.push_feature()

        ws = self.prep(self.make_pr(head), "incremental", old[:12])

        self.assertEqual(ws.mode, "full")
        self.assertTrue(ws.full_fallback)
        self.assertEqual(ws.changed_lines, 4)

    def test_unresolvable_base_falls_back_to_full(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(3))
        head = self.commit(self.dev, "feat")
        self.push_feature()

        ws = self.prep(self.make_pr(head), "incremental", "deadbeef0000")

        self.assertEqual(ws.mode, "full")
        self.assertTrue(ws.full_fallback)

    def test_size_count_skips_lockfiles_and_baselines(self) -> None:
        self.feature_branch()
        self.write(self.dev, "composer.lock", self.lines(4000))
        self.write(self.dev, "public/build/app.js", self.lines(100))
        self.write(self.dev, "resources/x.min.js", self.lines(100))
        self.write(self.dev, "phpstan-baseline.neon", self.lines(100))
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()

        ws = self.prep(self.make_pr(head))

        self.assertEqual(ws.changed_lines, 5)
        self.assertEqual(ws.files_changed, 5)

    def test_diff_patch_leaves_out_size_excluded_files(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(2))
        reviewed = self.commit(self.dev, "A")
        excluded = (
            "composer.lock", "resources/admin/package-lock.json", "yarn.lock", "phpstan-baseline.neon",
            "public/build/app.js", "resources/js/vendor.min.js", "public/css/site.min.css", "resources/x.js.map",
        )
        for name in excluded:
            self.write(self.dev, name, "LOCKFILE HUNK\n")
        self.write(self.dev, "app/b.php", self.lines(3))
        self.write(self.dev, "resources/build/keep.js", "keep\n")
        head = self.commit(self.dev, "B")
        self.push_feature()

        for mode, base in (("full", None), ("incremental", reviewed[:12])):
            with self.subTest(mode):
                diff = (self.prep(self.make_pr(head), mode, base).run_dir / "diff.patch").read_text()
                self.assertNotIn("LOCKFILE HUNK", diff)
                for name in excluded:
                    self.assertNotIn(name, diff)
                self.assertIn("app/b.php", diff)
                self.assertIn("resources/build/keep.js", diff)

    def test_large_diffs_are_prepared_in_full(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(6000))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        ws = self.prep(self.make_pr(head))
        self.assertEqual(ws.changed_lines, 6000)
        diff = (ws.run_dir / "diff.patch").read_text().splitlines()
        self.assertEqual(sum(1 for line in diff if line.startswith("+") and not line.startswith("+++")), 6000)

    def test_binary_files_count_zero_lines(self) -> None:
        self.feature_branch()
        (self.dev / "img.bin").write_bytes(b"\x00\x01\x02" * 100)
        head = self.commit(self.dev, "bin")
        self.push_feature()
        ws = self.prep(self.make_pr(head))
        self.assertEqual(ws.files_changed, 1)
        self.assertEqual(ws.changed_lines, 0)

    def test_main_checkout_is_untouched(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        (self.repo / "wip.txt").write_text("work in progress\n")
        (self.repo / "README.md").write_text("edited\n")
        git(self.repo, "stash", "push", "-m", "mine")
        (self.repo / "README.md").write_text("edited again\n")

        def snapshot() -> tuple:
            return (
                git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"),
                git(self.repo, "rev-parse", "HEAD"),
                git(self.repo, "status", "--porcelain"),
                git(self.repo, "stash", "list"),
                git(self.repo, "diff"),
            )

        before = snapshot()
        ws = self.prep(self.make_pr(head))
        cleanup(ws, repo=self.repo, worktrees_dir=self.worktrees_dir)
        self.assertEqual(snapshot(), before)

    def test_stale_worktree_from_crashed_run_is_replaced(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        pr = self.make_pr(head)

        first = self.prep(pr)
        (first.worktree / "leftover.txt").write_text("junk")
        second = self.prep(pr)

        self.assertEqual(second.worktree, first.worktree)
        self.assertFalse((second.worktree / "leftover.txt").exists())
        self.assertEqual(git(second.worktree, "rev-parse", "HEAD"), head)

    def test_stale_directory_without_registration_is_replaced(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        stale = self.worktrees_dir / "7"
        stale.mkdir(parents=True)
        (stale / "junk.txt").write_text("junk")

        ws = self.prep(self.make_pr(head))

        self.assertFalse((ws.worktree / "junk.txt").exists())
        self.assertEqual(git(ws.worktree, "rev-parse", "HEAD"), head)

    def test_rejects_branch_names_with_shell_or_option_characters(self) -> None:
        self.feature_branch()
        self.write(self.dev, "a.php", "x\n")
        head = self.commit(self.dev, "feat")
        self.push_feature()
        for bad in ["master; rm -rf ~", "--upload-pack=x", "a..b", "$(id)"]:
            for field in ("source_branch", "destination_branch"):
                pr = self.make_pr(head)
                pr = PullRequest(**{**pr.__dict__, field: bad})
                with self.assertRaises(WorkspaceError, msg=bad):
                    self.prep(pr)
        self.assertFalse(self.worktrees_dir.exists())

    def test_rejects_unknown_mode(self) -> None:
        self.feature_branch()
        self.write(self.dev, "a.php", "x\n")
        head = self.commit(self.dev, "feat")
        self.push_feature()
        with self.assertRaises(ValueError):
            self.prep(self.make_pr(head), "bogus")

    def test_diff_command_contains_only_shas(self) -> None:
        self.feature_branch()
        self.write(self.dev, "a.php", self.lines(3))
        reviewed = self.commit(self.dev, "A")
        self.write(self.dev, "b.php", self.lines(3))
        head = self.commit(self.dev, "B")
        self.push_feature()
        sha = "[0-9a-f]{40}"
        suffix = " -- " + " ".join(EXCLUDE_PATHSPECS)
        full = self.prep(self.make_pr(head))
        self.assertTrue(full.diff_command.endswith(suffix))
        self.assertRegex(full.diff_command[: -len(suffix)], "^git diff %s %s$" % (sha, sha))
        inc = self.prep(self.make_pr(head), "incremental", reviewed[:12])
        self.assertTrue(inc.diff_command.endswith(suffix))
        self.assertRegex(inc.diff_command[: -len(suffix)], "^git log --no-merges -p --reverse %s\\.\\.%s \\^%s$" % (sha, sha, sha))
        for ws in (full, inc):
            self.assertNotIn("master", ws.diff_command)
            self.assertNotIn("feature", ws.diff_command)

    def test_failed_prepare_removes_its_worktree(self) -> None:
        self.feature_branch()
        self.write(self.dev, "a.php", "x\n")
        head = self.commit(self.dev, "feat")
        self.push_feature()
        with mock.patch("prreview.workspace._numstat", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.prep(self.make_pr(head))
        self.assertFalse((self.worktrees_dir / "7").exists())
        self.assertNotIn(str(self.worktrees_dir), git(self.repo, "worktree", "list"))

    def test_cleanup_removes_worktree_and_keeps_run_dir(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        ws = self.prep(self.make_pr(head))

        cleanup(ws, repo=self.repo, worktrees_dir=self.worktrees_dir)

        self.assertFalse(ws.worktree.exists())
        self.assertTrue(ws.run_dir.is_dir())
        self.assertNotIn(str(ws.worktree), git(self.repo, "worktree", "list"))

    def test_prepare_strips_pr_controlled_claude_config(self) -> None:
        own = self.repo / ".claude" / "settings.local.json"
        own.parent.mkdir()
        own.write_text("{}")
        self.feature_branch()
        self.write(self.dev, ".claude/settings.json", '{"hooks": {}}\n')
        self.write(self.dev, ".claude/skills/evil/SKILL.md", "---\nallowed-tools: Bash\n---\n")
        self.write(self.dev, ".mcp.json", "{}\n")
        self.write(self.dev, "CLAUDE.md", "rules\n")
        head = self.commit(self.dev, "config")
        self.push_feature()

        ws = self.prep(self.make_pr(head))

        self.assertFalse(os.path.lexists(str(ws.worktree / ".claude")))
        self.assertFalse(os.path.lexists(str(ws.worktree / ".mcp.json")))
        self.assertEqual((ws.worktree / "CLAUDE.md").read_text(), "rules\n")
        self.assertEqual(own.read_text(), "{}")
        self.assertIn(".claude/settings.json", (ws.run_dir / "diff.patch").read_text())

    def test_prepare_unlinks_config_symlinks_without_following_them(self) -> None:
        victim_dir = self.data_dir.parent / "victim"
        victim_dir.mkdir()
        (victim_dir / "keep.txt").write_text("keep")
        victim_file = self.data_dir.parent / "victim.json"
        victim_file.write_text("keep")
        self.feature_branch()
        (self.dev / ".claude").symlink_to(victim_dir)
        (self.dev / ".mcp.json").symlink_to(victim_file)
        self.write(self.dev, "a.php", "x\n")
        head = self.commit(self.dev, "links")
        self.push_feature()

        ws = self.prep(self.make_pr(head))

        self.assertFalse(os.path.lexists(str(ws.worktree / ".claude")))
        self.assertFalse(os.path.lexists(str(ws.worktree / ".mcp.json")))
        self.assertEqual((victim_dir / "keep.txt").read_text(), "keep")
        self.assertEqual(victim_file.read_text(), "keep")

    def test_fetch_leaves_fetch_head_alone_and_checkout_runs_no_hooks(self) -> None:
        marker = self.data_dir.parent / "hook-ran"
        hook = "#!/bin/sh\necho ran >> %s\n" % marker
        own = self.repo / ".git" / "hooks" / "post-checkout"
        own.write_text(hook)
        own.chmod(0o755)
        self.feature_branch()
        self.write(self.dev, ".husky/post-checkout", hook)
        (self.dev / ".husky" / "post-checkout").chmod(0o755)
        head = self.commit(self.dev, "hooks")
        self.push_feature()
        (self.repo / ".git" / "FETCH_HEAD").unlink(missing_ok=True)

        for hooks_path in (None, ".husky"):
            with self.subTest(hooks_path=hooks_path):
                if hooks_path:
                    git(self.repo, "config", "core.hooksPath", hooks_path)
                ws = self.prep(self.make_pr(head))

                self.assertTrue((ws.worktree / "README.md").exists())
                self.assertFalse(marker.exists())
                self.assertFalse((self.repo / ".git" / "FETCH_HEAD").exists())

    def test_pr_symlinks_are_checked_out_as_plain_files(self) -> None:
        secret = self.data_dir.parent / "secret.txt"
        secret.write_text("secret")
        self.feature_branch()
        (self.dev / "app").mkdir()
        (self.dev / "app" / "link.php").symlink_to(secret)
        (self.dev / "up").symlink_to("..")
        head = self.commit(self.dev, "links")
        self.push_feature()

        ws = self.prep(self.make_pr(head))

        for rel, target in (("app/link.php", str(secret)), ("up", "..")):
            path = ws.worktree / rel
            self.assertFalse(path.is_symlink(), rel)
            self.assertEqual(path.read_text(), target)
        self.assertNotIn("symlinks = false", (self.repo / ".git" / "config").read_text())

    def test_strips_nested_claude_config_at_any_depth(self) -> None:
        outside = self.data_dir.parent / "outside-claude"
        (outside / "skills" / "x").mkdir(parents=True)
        (outside / "skills" / "x" / "SKILL.md").write_text("keep")
        self.feature_branch()
        self.write(self.dev, ".claude/settings.json", "{}\n")
        self.write(self.dev, "a/b/.claude/skills/x/SKILL.md", "---\nallowed-tools: Bash\n---\n")
        self.write(self.dev, "a/.mcp.json", "{}\n")
        self.write(self.dev, "a/CLAUDE.md", "nested rules\n")
        (self.dev / "c").mkdir()
        (self.dev / "c" / ".claude").symlink_to(outside)
        self.write(self.dev, "c/keep.php", "x\n")
        head = self.commit(self.dev, "nested config")
        self.push_feature()

        ws = self.prep(self.make_pr(head))

        for rel in (".claude", "a/b/.claude", "a/.mcp.json", "c/.claude"):
            self.assertFalse(os.path.lexists(str(ws.worktree / rel)), rel)
        self.assertTrue((ws.worktree / "a" / "b").is_dir())
        self.assertEqual((ws.worktree / "a" / "CLAUDE.md").read_text(), "nested rules\n")
        self.assertTrue((ws.worktree / "c" / "keep.php").exists())
        self.assertEqual((outside / "skills" / "x" / "SKILL.md").read_text(), "keep")
        diff = (ws.run_dir / "diff.patch").read_text()
        self.assertIn("a/b/.claude/skills/x/SKILL.md", diff)
        self.assertIn("a/.mcp.json", diff)

    def test_strips_case_variant_claude_config(self) -> None:
        self.feature_branch()
        self.write(self.dev, "CLAUDE.md", "rules\n")
        self.write(self.dev, "x/.Claude/skills/s/SKILL.md", "---\nallowed-tools: Bash\n---\n")
        self.write(self.dev, "x/CLAUDE.md", "x rules\n")
        self.write(self.dev, "y/.MCP.JSON", "{}\n")
        self.write(self.dev, "y/CLAUDE.md", "y rules\n")
        self.write(self.dev, "z/.mcp.jſon", "{}\n")
        self.write(self.dev, "z/keep.php", "x\n")
        head = self.commit(self.dev, "case variants")
        self.push_feature()

        ws = self.prep(self.make_pr(head))

        self.assertEqual(sorted(os.listdir(str(ws.worktree / "x"))), ["CLAUDE.md"])
        self.assertEqual(sorted(os.listdir(str(ws.worktree / "y"))), ["CLAUDE.md"])
        self.assertEqual(sorted(os.listdir(str(ws.worktree / "z"))), ["keep.php"])
        self.assertEqual((ws.worktree / "CLAUDE.md").read_text(), "rules\n")
        self.assertEqual((ws.worktree / "x" / "CLAUDE.md").read_text(), "x rules\n")
        self.assertEqual((ws.worktree / "y" / "CLAUDE.md").read_text(), "y rules\n")

    def test_failed_config_strip_removes_the_worktree(self) -> None:
        self.feature_branch()
        self.write(self.dev, "a.php", "x\n")
        head = self.commit(self.dev, "feat")
        self.push_feature()
        with mock.patch("prreview.workspace._strip_session_config", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.prep(self.make_pr(head))
        self.assertFalse((self.worktrees_dir / "7").exists())

    def test_cleanup_refuses_paths_outside_its_worktrees_dir(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        ws = self.prep(self.make_pr(head))
        other = self.worktrees_dir / "8"
        other.mkdir()

        for target in (self.repo, other, self.worktrees_dir / "7" / "app"):
            with self.subTest(target=target):
                with self.assertRaises(WorkspaceError):
                    cleanup(replace(ws, worktree=target), repo=self.repo, worktrees_dir=self.worktrees_dir)

        self.assertTrue((self.repo / "README.md").exists())
        self.assertTrue(other.is_dir())
        self.assertTrue((ws.worktree / "app" / "a.php").exists())

    def test_prepare_writes_the_diff_to_diff_patch(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(3))
        reviewed = self.commit(self.dev, "A")
        self.write(self.dev, "app/b.php", self.lines(4))
        head = self.commit(self.dev, "B")
        self.push_feature()
        dest = git(self.repo, "rev-parse", "origin/master")

        full = self.prep(self.make_pr(head))
        expected_full = git(self.repo, "diff", full.base, head)
        self.assertEqual((full.run_dir / "diff.patch").read_text().strip(), expected_full)
        self.assertIn("+line 3", expected_full)

        inc = self.prep(self.make_pr(head), "incremental", reviewed[:12])
        expected_inc = git(self.repo, "log", "--no-merges", "-p", "--reverse", "%s..%s" % (reviewed, head), "^" + dest)
        self.assertEqual((inc.run_dir / "diff.patch").read_text().strip(), expected_inc)
        self.assertIn("app/b.php", expected_inc)
        self.assertNotIn("app/a.php", expected_inc)

    def test_every_git_call_has_a_timeout_and_a_timeout_raises(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(3))
        head = self.commit(self.dev, "feat")
        self.push_feature()

        with mock.patch("prreview.workspace.subprocess.run", wraps=subprocess.run) as run:
            self.prep(self.make_pr(head))

        timeouts = [(call.args[0][3], call.kwargs.get("timeout")) for call in run.call_args_list]
        self.assertIn(("fetch", 300), timeouts)
        self.assertEqual({t for name, t in timeouts if name != "fetch"}, {120})
        expired = subprocess.TimeoutExpired(["git"], 120)
        with mock.patch("prreview.workspace.subprocess.run", side_effect=expired):
            with self.assertRaisesRegex(WorkspaceError, "timed out after 120s"):
                _git(self.repo, "status")


class PruneRunsTest(unittest.TestCase):
    def test_removes_only_old_direct_run_dirs_without_following_symlinks(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        runs = root / "runs"
        outside = root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        now = 2_000_000_000.0
        old = now - 15 * 24 * 3600

        def make_dir(rel: str, mtime: float) -> Path:
            path = runs / rel
            path.mkdir(parents=True)
            (path / "screen.txt").write_text("x")
            os.utime(str(path), (mtime, mtime))
            return path

        make_dir("101-aaaaaaaaaaaa", old)
        (runs / "101-aaaaaaaaaaaa" / "link").symlink_to(outside)
        make_dir("102-bbbbbbbbbbbb", now - 13 * 24 * 3600)
        for rel in ("notes", "103-ABCDEF123456", "106-eeeeeeeeeeeee", "sub/107-ffffffffffff"):
            make_dir(rel, old)
        (runs / "104-cccccccccccc").symlink_to(outside)
        os.utime(str(runs / "104-cccccccccccc"), (old, old), follow_symlinks=False)
        (runs / "105-dddddddddddd").write_text("file")
        os.utime(str(runs / "105-dddddddddddd"), (old, old))
        os.utime(str(runs / "sub"), (old, old))

        self.assertEqual(prune_runs(runs, now), ["101-aaaaaaaaaaaa"])

        self.assertEqual(
            sorted(os.listdir(str(runs))),
            ["102-bbbbbbbbbbbb", "103-ABCDEF123456", "104-cccccccccccc", "105-dddddddddddd", "106-eeeeeeeeeeeee", "notes", "sub"],
        )
        self.assertTrue((runs / "sub" / "107-ffffffffffff").is_dir())
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertEqual(prune_runs(root / "missing", now), [])


class SessionConfigNameTest(unittest.TestCase):
    def test_matching_folds_case_and_unicode(self) -> None:
        for name in (".claude", ".Claude", ".CLAUDE", ".mcp.json", ".MCP.JSON", ".mcp.jſon"):
            self.assertTrue(_is_session_config_name(name), name)
        for name in ("CLAUDE.md", ".claude.bak", "claude", ".git", ".claudé", ".claudé"):
            self.assertFalse(_is_session_config_name(name), name)


class WriteContextTest(WorkspaceTestCase):
    def test_context_json_contains_paths_and_no_secrets(self) -> None:
        self.feature_branch()
        self.write(self.dev, "app/a.php", self.lines(5))
        head = self.commit(self.dev, "feat")
        self.push_feature()
        ws = self.prep(self.make_pr(head))
        findings: List[dict] = [{"id": "f1", "severity": "nit", "title": "T", "file": "a.php", "line": 3}]

        path = write_context(ws, findings, "https://claude.ai/artifact/x", "watcher")

        self.assertEqual(path, ws.run_dir / "context.json")
        text = path.read_text()
        self.assertNotIn("xoxb-", text)
        self.assertNotIn("ATATT", text)
        ctx = json.loads(text)
        self.assertEqual(ctx["pr"]["id"], 7)
        self.assertEqual(ctx["pr"]["head"], head[:12])
        self.assertEqual(ctx["pr"]["source_branch"], "feature")
        self.assertEqual(ctx["mode"], "full")
        self.assertFalse(ctx["full_fallback"])
        self.assertEqual(ctx["head"], head)
        self.assertEqual(ctx["worktree"], str(ws.worktree))
        self.assertEqual(ctx["run_dir"], str(ws.run_dir))
        self.assertEqual(ctx["diff_command"], ws.diff_command)
        self.assertEqual(ctx["diff_path"], str(ws.run_dir / "diff.patch"))
        self.assertTrue((ws.run_dir / "diff.patch").exists())
        self.assertEqual(ctx["changed_lines"], 5)
        self.assertEqual(ctx["files_changed"], 1)
        self.assertEqual(ctx["commit_count"], 1)
        self.assertNotIn("too_large", ctx)
        self.assertEqual(ctx["earlier_findings"], findings)
        self.assertEqual(ctx["artifact_url"], "https://claude.ai/artifact/x")
        self.assertEqual(ctx["invoked_by"], "watcher")
        self.assertEqual(ctx["review_path"], str(ws.run_dir / "review.json"))
        self.assertEqual(ctx["memory_index"], str(Path.home() / ".claude/projects/-srv-acme-widgets/memory/MEMORY.md"))
        self.assertEqual(
            ctx["render_command"],
            "/usr/bin/python3 -I %s/bin/pr-review render %s/context.json %s/review.json"
            % (Path(__file__).resolve().parent.parent, ws.run_dir, ws.run_dir),
        )

    def test_invoked_by_is_validated(self) -> None:
        self.feature_branch()
        self.write(self.dev, "a.php", "x\n")
        head = self.commit(self.dev, "feat")
        self.push_feature()
        ws = self.prep(self.make_pr(head))
        with self.assertRaises(ValueError):
            write_context(ws, [], None, "cron")


if __name__ == "__main__":
    unittest.main()

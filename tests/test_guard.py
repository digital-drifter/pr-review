import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional, Tuple
from unittest import mock

from prreview import guard, runner

BIN = Path(__file__).resolve().parent.parent / "bin" / "pr-review"
STORED = "https://claude.ai/artifact/Stored123"
OTHER = "https://claude.ai/artifact/Other456"
FORBIDDEN_PUBLISH_KEYS = (
    "asset", "file_paths", "files", "capabilities", "type_url", "from_url", "asset_ids", "edits", "root", "force",
    "contract", "pin", "auto_open", "overwrite_unread",
)


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run_dir = self.root / "runs" / "101-aaaaaaaaaaaa"
        self.run_dir.mkdir(parents=True)
        self.page = self.run_dir / "page.html"
        self.page.write_text("<html></html>")
        self.worktree = self.root / "worktrees" / "101"
        self.worktree.mkdir(parents=True)
        (self.worktree / "evil.html").write_text("<html>evil</html>")
        self.use_url(None)

    def use_url(self, url: Optional[str]) -> None:
        (self.run_dir / "context.json").write_text(json.dumps({"artifact_url": url, "invoked_by": "watcher"}))

    def hook(self, tool: str, tool_input: Any) -> str:
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})

    def run_guard(self, stdin: str, argv: Optional[list] = None) -> Tuple[int, str]:
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = guard.main([str(self.run_dir)] if argv is None else argv, io.StringIO(stdin))
        return code, err.getvalue()

    def assert_allowed(self, tool: str, tool_input: Any) -> None:
        self.assertEqual(self.run_guard(self.hook(tool, tool_input)), (0, ""))

    def assert_denied(self, tool: str, tool_input: Any, reason: str = "") -> None:
        code, err = self.run_guard(self.hook(tool, tool_input))
        self.assertEqual(code, 2, tool_input)
        self.assertTrue(err.startswith("pr-review guard: "), err)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertIn(reason, err)

    def publish(self, **extra: Any) -> dict:
        return dict({"file_path": str(self.page), "icon": "code", "description": "Claude's review of PR #101"}, **extra)


class ArtifactGuardTest(GuardTestCase):
    def test_first_publish_of_the_run_page_without_url_is_allowed(self) -> None:
        self.assert_allowed("Artifact", self.publish())
        self.assert_allowed("Artifact", self.publish(action="publish", title="PR 101", label="first"))
        self.assert_allowed("Artifact", {"file_path": str(self.run_dir / ".." / self.run_dir.name / "page.html")})

    def test_first_publish_with_any_url_is_denied(self) -> None:
        for url in (OTHER, STORED, ""):
            with self.subTest(url=url):
                self.assert_denied("Artifact", self.publish(url=url), "no artifact yet")

    def test_update_must_target_the_stored_url(self) -> None:
        self.use_url(STORED)

        self.assert_allowed("Artifact", self.publish(url=STORED))
        self.assert_denied("Artifact", self.publish(url=OTHER), "must use url %s" % STORED)
        self.assert_denied("Artifact", self.publish(), "must use url %s" % STORED)
        self.assert_denied("Artifact", self.publish(url=STORED + "/"), "must use url")

    def test_publish_of_any_other_file_is_denied(self) -> None:
        for file_path in (
            str(self.worktree / "evil.html"),
            "page.html",
            "runs/101-aaaaaaaaaaaa/page.html",
            str(self.run_dir / "page.json"),
            None,
            7,
        ):
            with self.subTest(file_path=file_path):
                self.assert_denied("Artifact", self.publish(file_path=file_path), "Artifact publish")
        self.assert_denied("Artifact", {"icon": "code"}, "needs file_path")

    def test_a_symlinked_or_missing_page_is_denied(self) -> None:
        self.page.unlink()
        self.assert_denied("Artifact", self.publish(), "limited to")
        self.page.symlink_to(self.worktree / "evil.html")
        self.assert_denied("Artifact", self.publish(), "limited to")
        self.assert_denied("Artifact", self.publish(file_path=str(self.worktree / "evil.html")), "limited to")

    def test_every_extra_publish_parameter_is_denied(self) -> None:
        self.use_url(STORED)
        for key in FORBIDDEN_PUBLISH_KEYS + ("favicon", "prompt", "path"):
            with self.subTest(key=key):
                self.assert_denied("Artifact", self.publish(url=STORED, **{key: True}), "does not allow %s" % key)

    def test_non_string_publish_values_are_denied(self) -> None:
        self.assert_denied("Artifact", self.publish(icon=["code"]), "must be strings")
        self.assert_denied("Artifact", self.publish(action=None), "action None is not allowed")

    def test_read_is_limited_to_the_stored_url(self) -> None:
        self.use_url(STORED)

        self.assert_allowed("Artifact", {"action": "read", "url": STORED})
        self.assert_allowed("Artifact", {"action": "read", "url": STORED, "prompt": "the review page"})
        self.assert_denied("Artifact", {"action": "read", "url": OTHER}, "limited to this PR's review artifact")
        self.assert_denied("Artifact", {"action": "read"}, "limited to")
        self.assert_denied("Artifact", {"action": "read", "url": STORED, "prompt": 3}, "must be a string")
        for key in ("path", "paths", "out_dir", "type_url", "page"):
            with self.subTest(key=key):
                self.assert_denied("Artifact", {"action": "read", "url": STORED, key: "x"}, "does not allow %s" % key)

    def test_read_without_a_stored_url_is_denied(self) -> None:
        self.assert_denied("Artifact", {"action": "read", "url": OTHER}, "limited to")

    def test_every_other_action_is_denied(self) -> None:
        self.use_url(STORED)
        for action in ("list", "delete", "open", "pin", "unpin", "quickstart", "PUBLISH", ""):
            with self.subTest(action=action):
                self.assert_denied("Artifact", {"action": action, "url": STORED}, "is not allowed")


class AgentGuardTest(GuardTestCase):
    def test_default_general_purpose_and_explore_agents_are_allowed(self) -> None:
        base = {"description": "Verify a claim", "prompt": "Read app/Foo.php"}
        for tool in ("Agent", "Task"):
            for extra in (
                {},
                {"subagent_type": "general-purpose"},
                {"subagent_type": "Explore"},
                {"model": "sonnet", "run_in_background": True, "effort": "low"},
            ):
                with self.subTest(tool=tool, extra=extra):
                    self.assert_allowed(tool, dict(base, **extra))

    def test_any_isolation_is_denied(self) -> None:
        for value in ("remote", "worktree", None, ""):
            with self.subTest(isolation=value):
                self.assert_denied("Agent", {"description": "d", "prompt": "p", "isolation": value}, "isolation")

    def test_other_agent_types_are_denied(self) -> None:
        for kind in ("fork", "Plan", "claude-code-guide", "security-bug-reviewer", "", None, ["Explore"]):
            with self.subTest(kind=kind):
                self.assert_denied("Agent", {"description": "d", "prompt": "p", "subagent_type": kind}, "subagent_type")

    def test_unknown_agent_parameters_are_denied(self) -> None:
        for key in ("cwd", "name", "team_name", "mode"):
            with self.subTest(key=key):
                self.assert_denied("Agent", {"description": "d", "prompt": "p", key: "x"}, "does not allow %s" % key)


class FailClosedTest(GuardTestCase):
    def test_unparseable_or_unexpected_input_is_denied(self) -> None:
        for stdin in ("", "{not json", "[]", json.dumps({"tool_name": "Artifact"}),
                      json.dumps({"tool_name": "Artifact", "tool_input": "x"})):
            with self.subTest(stdin=stdin):
                code, err = self.run_guard(stdin)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("pr-review guard: "), err)

    def test_other_tools_are_denied(self) -> None:
        for tool in ("ArtifactData", "Bash", "", None):
            with self.subTest(tool=tool):
                self.assert_denied(tool, {}, "not a guarded tool")

    def test_missing_or_bad_context_is_denied(self) -> None:
        cases = (None, "{bad", json.dumps([]), json.dumps({}), json.dumps({"artifact_url": "https://evil.test/x"}))
        for context in cases:
            with self.subTest(context=context):
                path = self.run_dir / "context.json"
                path.unlink(missing_ok=True)
                if context is not None:
                    path.write_text(context)
                self.assert_denied("Artifact", self.publish(), "failed closed")

    def test_wrong_arguments_are_denied(self) -> None:
        for argv in ([], [str(self.run_dir), "extra"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_guard(self.hook("Artifact", self.publish()), argv)[0], 2)


class GuardCommandTest(GuardTestCase):
    def shell(self, command: str, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", "-c", command], input=stdin, capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "PR_REVIEW_ENV_FILE": os.environ["PR_REVIEW_ENV_FILE"]},
        )

    def test_session_hook_command_allows_and_denies_with_exit_codes(self) -> None:
        command = runner.session_settings(self.run_dir)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        allowed = self.shell(command, self.hook("Artifact", self.publish()))
        denied = self.shell(command, self.hook("Agent", {"description": "d", "prompt": "p", "isolation": "remote"}))
        garbage = self.shell(command, "not json")

        self.assertEqual((allowed.returncode, allowed.stderr), (0, ""))
        self.assertEqual((denied.returncode, denied.stderr), (2, "pr-review guard: Agent isolation is not allowed\n"))
        self.assertEqual((garbage.returncode, garbage.stderr), (2, "pr-review guard: failed closed: JSONDecodeError\n"))

    def test_any_other_failure_of_the_command_becomes_exit_2(self) -> None:
        for name, body, expected in (("crash", "import sys\nsys.exit(1)\n", 2), ("ok", "pass\n", 0)):
            with self.subTest(name):
                script = self.root / name
                script.write_text(body)
                with mock.patch("prreview.runner.RENDER_BIN", str(script)):
                    command = runner.guard_command(self.run_dir)
                self.assertEqual(self.shell(command, "{}").returncode, expected)

    def test_bin_script_fails_closed_when_the_guard_cannot_load(self) -> None:
        copy = self.root / "copy"
        (copy / "bin").mkdir(parents=True)
        (copy / "prreview").mkdir()
        shutil.copy(str(BIN), str(copy / "bin" / "pr-review"))
        (copy / "prreview" / "__init__.py").write_text("")
        (copy / "prreview" / "guard.py").write_text("raise RuntimeError('broken')\n")

        result = subprocess.run(
            ["/usr/bin/python3", "-I", str(copy / "bin" / "pr-review"), "guard", str(self.run_dir)],
            input="{}", capture_output=True, text=True, timeout=30,
        )

        self.assertEqual((result.returncode, result.stderr), (2, "pr-review guard: failed closed: RuntimeError\n"))
        self.assertTrue(os.access(str(BIN), os.X_OK))

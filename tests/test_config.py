import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from prreview import config
from prreview.config import ConfigMissing, load_settings, read_env_file, required

PROJECT = Path(__file__).resolve().parent.parent


class EnvFileTest(unittest.TestCase):
    def test_reads_keys_and_skips_comments_blanks_and_junk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n\nA=1\n  B = two words  \nC=\"{quoted}\"\nD='single'\nE=x=y\nnot a setting\nF=\n"
            )
            self.assertEqual(
                read_env_file(path), {"A": "1", "B": "two words", "C": "{quoted}", "D": "single", "E": "x=y", "F": ""}
            )

    def test_a_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(read_env_file(Path("/nonexistent/.env")), {})

    def test_environment_overrides_the_file_named_by_pr_review_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.env"
            path.write_text("A=file\nB=file\n")
            settings = load_settings({"PR_REVIEW_ENV_FILE": str(path), "B": "env", "C": ""})
        self.assertEqual((settings["A"], settings["B"], settings["C"]), ("file", "env", ""))

    def test_required_rejects_missing_and_empty_values(self) -> None:
        self.assertEqual(required({"X": "1"}, "X"), "1")
        for settings in ({}, {"X": ""}):
            with self.assertRaisesRegex(ConfigMissing, "X is not set; copy .env.example to .env"):
                required(settings, "X")


class LoadedConfigTest(unittest.TestCase):
    def test_settings_come_from_the_test_env_file_with_derived_defaults(self) -> None:
        self.assertEqual(config.REPO, Path("/srv/acme/widgets"))
        self.assertEqual(config.REPO_FULL_NAME, "acme/widgets")
        self.assertEqual(config.REVIEWER_UUID, "{00000000-0000-4000-8000-000000000001}")
        self.assertEqual(config.BB_EMAIL, "reviewer@example.com")
        self.assertEqual(config.SLACK_CHANNEL, "U0000000001")
        self.assertEqual(config.DASHBOARD_TRIGGER_ID, "")
        self.assertEqual(config.WORKTREES_DIR, Path("/srv/acme/.pr-review-worktrees"))
        self.assertEqual(config.RUNS_DIR, Path("/srv/acme/.pr-review-runs"))
        self.assertEqual(config.CLAUDE_BIN, str(Path.home() / ".local/bin/claude"))
        self.assertEqual(config.READ_DENY[-1], "/srv/acme/widgets/**")

    def test_missing_settings_fail_loudly_and_the_guard_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PR_REVIEW_ENV_FILE=str(Path(tmp) / "missing.env"))
            imported = subprocess.run(
                ["/usr/bin/python3", "-I", "-c", "import sys; sys.path.insert(0, %r); import prreview.config" % str(PROJECT)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            guard = subprocess.run(
                ["/usr/bin/python3", "-I", str(PROJECT / "bin" / "pr-review"), "guard", tmp],
                env=env, input="{}", capture_output=True, text=True, timeout=30,
            )
        self.assertNotEqual(imported.returncode, 0)
        self.assertIn("PR_REVIEW_REPO is not set", imported.stderr)
        self.assertEqual(guard.returncode, 2)
        self.assertIn("failed closed", guard.stderr)


if __name__ == "__main__":
    unittest.main()

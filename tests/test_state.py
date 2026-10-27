import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prreview.state import FailureEntry, PrEntry, State, load_state, save_state


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "state.json"

    def test_missing_file_gives_empty_state(self) -> None:
        state = load_state(self.path)
        self.assertEqual((state.prs, state.failures), ({}, {}))

    def test_round_trip_preserves_entries_and_failures(self) -> None:
        state = State(
            prs={12: PrEntry("aaa", 5, "bbb", "ccc", 2, "https://a.test")},
            failures={"net": FailureEntry("boom", 2, False)},
        )
        save_state(state, self.path)
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
        loaded = load_state(self.path)
        self.assertEqual(loaded, state)
        self.assertIn(12, loaded.prs)

    def test_failed_save_leaves_previous_file_intact(self) -> None:
        save_state(State(prs={1: PrEntry("aaa", 1)}, failures={}), self.path)
        before = self.path.read_text()
        with mock.patch("prreview.state.os.replace", side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                save_state(State(prs={2: PrEntry("bbb", 2)}, failures={}), self.path)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(os.listdir(self.path.parent), ["state.json"])

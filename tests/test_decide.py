import unittest
from dataclasses import replace
from typing import Optional

from prreview.decide import clear_failure, next_action, observe, record_failure
from prreview.models import Action, PullRequest
from prreview.state import PrEntry, State


def pr(pr_id: int, head: str = "aaa", draft: bool = False) -> PullRequest:
    return PullRequest(pr_id, "Title", "Body", "Author", draft, head, "feature", "master", "https://example.test/" + str(pr_id))


def empty_state() -> State:
    return State(prs={}, failures={})


class DecideTest(unittest.TestCase):
    def test_new_ready_pr_is_reviewed_right_away(self) -> None:
        state = empty_state()
        observe(state, [pr(1, head="aaa")], now=1000)
        self.assertEqual(next_action(state, [pr(1, head="aaa")], now=1000), Action(pr(1, head="aaa"), "full", None))

    def test_a_fresh_push_is_reviewed_right_away(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, reviewed_commit="aaa")}, failures={})
        observe(state, [pr(1, head="bbb")], now=5000)
        self.assertEqual(next_action(state, [pr(1, head="bbb")], now=5000), Action(pr(1, head="bbb"), "incremental", "aaa"))

    def test_drafts_are_skipped(self) -> None:
        state = empty_state()
        observe(state, [pr(1, draft=True)], now=0)
        self.assertIsNone(next_action(state, [pr(1, draft=True)], now=10000))

    def test_fork_entries_are_never_acted_on(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, fork=True)}, failures={})
        fork = replace(pr(1, head="bbb"), source_repo="someone/widgets")
        observe(state, [fork], now=0)
        self.assertIsNone(next_action(state, [fork], now=10000))
        self.assertTrue(state.prs[1].fork)

    def test_stale_fork_flag_is_cleared_when_seen_from_the_repo(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, fork=True)}, failures={})
        observe(state, [pr(1, head="aaa")], now=50)
        entry = state.prs[1]
        self.assertEqual((entry.fork, entry.head_first_seen), (False, 0))
        self.assertEqual(next_action(state, [pr(1, head="aaa")], now=10000), Action(pr(1, head="aaa"), "full", None))

    def test_head_change_resets_first_seen_and_attempts(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, attempts=2, reviewed_commit="rrr", baseline_head="bbb", blocked=2)}, failures={})
        observe(state, [pr(1, head="ccc")], now=500)
        entry = state.prs[1]
        self.assertEqual((entry.head, entry.head_first_seen, entry.attempts, entry.blocked), ("ccc", 500, 0, 0))
        self.assertEqual((entry.reviewed_commit, entry.baseline_head), ("rrr", "bbb"))

    def test_observe_keeps_entry_when_head_unchanged(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, attempts=2)}, failures={})
        observe(state, [pr(1, head="aaa")], now=500)
        self.assertEqual((state.prs[1].head_first_seen, state.prs[1].attempts), (0, 2))

    def test_reviewed_pr_with_new_head_is_incremental_from_reviewed_commit(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, reviewed_commit="aaa")}, failures={})
        observe(state, [pr(1, head="bbb")], now=0)
        action: Optional[Action] = next_action(state, [pr(1, head="bbb")], now=600)
        assert action is not None
        self.assertEqual(action.mode, "incremental")
        self.assertEqual(action.base, "aaa")

    def test_seeded_ready_pr_ignored_until_push_then_full(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, baseline_head="aaa")}, failures={})
        observe(state, [pr(1, head="aaa")], now=5000)
        self.assertIsNone(next_action(state, [pr(1, head="aaa")], now=5000))
        observe(state, [pr(1, head="bbb")], now=6000)
        action = next_action(state, [pr(1, head="bbb")], now=6600)
        assert action is not None
        self.assertEqual((action.mode, action.base), ("full", None))

    def test_seeded_draft_gets_full_review_when_marked_ready(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, baseline_head=None)}, failures={})
        observe(state, [pr(1, head="aaa", draft=False)], now=5000)
        action = next_action(state, [pr(1, head="aaa", draft=False)], now=5000)
        assert action is not None
        self.assertEqual((action.mode, action.base), ("full", None))

    def test_gives_up_after_three_attempts_on_same_head(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, attempts=3)}, failures={})
        self.assertIsNone(next_action(state, [pr(1)], now=10000))
        state.prs[1].attempts = 2
        self.assertIsNotNone(next_action(state, [pr(1)], now=10000))

    def test_reviewed_commit_equal_head_is_no_action(self) -> None:
        state = State(prs={1: PrEntry(head="aaa", head_first_seen=0, reviewed_commit="aaa")}, failures={})
        self.assertIsNone(next_action(state, [pr(1)], now=10000))

    def test_oldest_head_first_seen_wins(self) -> None:
        state = State(prs={
            1: PrEntry(head="aaa", head_first_seen=300),
            2: PrEntry(head="bbb", head_first_seen=100),
            3: PrEntry(head="ccc", head_first_seen=200),
        }, failures={})
        action = next_action(state, [pr(1, "aaa"), pr(2, "bbb"), pr(3, "ccc")], now=10000)
        assert action is not None
        self.assertEqual(action.pr.id, 2)

    def test_auth_failure_notifies_once_until_cleared(self) -> None:
        state = empty_state()
        self.assertTrue(record_failure(state, "bitbucket", "401", transient=False))
        self.assertFalse(record_failure(state, "bitbucket", "401", transient=False))
        clear_failure(state, "bitbucket")
        self.assertTrue(record_failure(state, "bitbucket", "401", transient=False))

    def test_transient_failure_notifies_on_fifteenth_consecutive(self) -> None:
        state = empty_state()
        results = [record_failure(state, "net", "x", transient=True) for _ in range(16)]
        self.assertEqual(results, [False] * 14 + [True, False])

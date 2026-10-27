from typing import List, Optional

from prreview.config import MAX_ATTEMPTS, REPO_FULL_NAME, TRANSIENT_NOTIFY_AFTER
from prreview.models import Action, PullRequest
from prreview.state import FailureEntry, PrEntry, State


def observe(state: State, prs: List[PullRequest], now: int) -> None:
    for pr in prs:
        entry = state.prs.get(pr.id)
        if entry is None:
            state.prs[pr.id] = PrEntry(head=pr.head, head_first_seen=now)
            continue
        if entry.fork and pr.source_repo == REPO_FULL_NAME:
            entry.fork = False
        if entry.head != pr.head:
            entry.head = pr.head
            entry.head_first_seen = now
            entry.attempts = 0
            entry.blocked = 0


def next_action(state: State, prs: List[PullRequest], now: int) -> Optional[Action]:
    best: Optional[Action] = None
    best_seen = 0
    for pr in prs:
        entry = state.prs.get(pr.id)
        if entry is None or pr.draft or entry.fork:
            continue
        if entry.attempts >= MAX_ATTEMPTS:
            continue
        if entry.reviewed_commit == pr.head or entry.baseline_head == pr.head:
            continue
        if best is not None and entry.head_first_seen >= best_seen:
            continue
        mode = "incremental" if entry.reviewed_commit else "full"
        best = Action(pr, mode, entry.reviewed_commit)
        best_seen = entry.head_first_seen
    return best


def record_failure(state: State, key: str, message: str, transient: bool) -> bool:
    entry = state.failures.get(key)
    if entry is None:
        entry = FailureEntry(message=message, count=0, notified=False)
        state.failures[key] = entry
    entry.message = message
    entry.count += 1
    if entry.notified:
        return False
    threshold = TRANSIENT_NOTIFY_AFTER if transient else 1
    if entry.count >= threshold:
        entry.notified = True
        return True
    return False


def clear_failure(state: State, key: str) -> None:
    state.failures.pop(key, None)

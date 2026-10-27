from dataclasses import dataclass
from typing import Optional

from prreview.config import REPO_FULL_NAME


@dataclass(frozen=True)
class PullRequest:
    id: int
    title: str
    description: str
    author: str
    draft: bool
    head: str
    source_branch: str
    destination_branch: str
    link: str
    source_repo: str = REPO_FULL_NAME


@dataclass(frozen=True)
class Counts:
    critical: int
    should_fix: int
    nit: int


@dataclass(frozen=True)
class Action:
    pr: PullRequest
    mode: str
    base: Optional[str]

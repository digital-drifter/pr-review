import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

VERSION = 1


@dataclass
class PrEntry:
    head: str
    head_first_seen: int
    reviewed_commit: Optional[str] = None
    baseline_head: Optional[str] = None
    attempts: int = 0
    artifact_url: Optional[str] = None
    blocked: int = 0
    fork: bool = False


@dataclass
class FailureEntry:
    message: str
    count: int
    notified: bool


@dataclass
class State:
    prs: Dict[int, PrEntry]
    failures: Dict[str, FailureEntry]


def load_state(path: Path) -> State:
    if not path.exists():
        return State(prs={}, failures={})
    raw = json.loads(path.read_text())
    return State(
        prs={int(key): PrEntry(**value) for key, value in raw.get("prs", {}).items()},
        failures={key: FailureEntry(**value) for key, value in raw.get("failures", {}).items()},
    )


def save_state(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": VERSION,
        "prs": {str(key): asdict(value) for key, value in state.prs.items()},
        "failures": {key: asdict(value) for key, value in state.failures.items()},
    }
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

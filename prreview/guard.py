import json
import os
import stat
import sys
from pathlib import Path
from typing import IO, Any, Dict, List, Optional

from prreview.notify import is_artifact_url

PREFIX = "pr-review guard: "
PUBLISH_KEYS = frozenset({"action", "file_path", "url", "icon", "description", "title", "label"})
READ_KEYS = frozenset({"action", "url", "prompt"})
AGENT_KEYS = frozenset({"description", "prompt", "subagent_type", "model", "effort", "run_in_background"})
AGENT_TYPES = frozenset({"general-purpose", "Explore"})
AGENT_TOOLS = frozenset({"Agent", "Task"})


def stored_url(run_dir: Path) -> Optional[str]:
    url = json.loads((run_dir / "context.json").read_text())["artifact_url"]
    if url is not None and not is_artifact_url(url):
        raise ValueError("context.json artifact_url is not a claude.ai artifact link")
    return url


def _extra_keys(tool_input: Dict[str, Any], allowed: frozenset) -> Optional[str]:
    extra = sorted(set(tool_input) - allowed)
    return ", ".join(extra) if extra else None


def _page_error(file_path: Any, run_dir: Path) -> Optional[str]:
    page = run_dir / "page.html"
    if not isinstance(file_path, str) or not os.path.isabs(file_path):
        return "Artifact publish needs file_path %s" % page
    try:
        regular = stat.S_ISREG(os.lstat(str(page)).st_mode)
    except OSError:
        regular = False
    if not regular or os.path.realpath(file_path) != os.path.realpath(str(page)):
        return "Artifact publish is limited to %s" % page
    return None


def check_artifact(tool_input: Dict[str, Any], run_dir: Path) -> Optional[str]:
    action = tool_input.get("action", "publish")
    if action == "publish":
        extra = _extra_keys(tool_input, PUBLISH_KEYS)
        if extra:
            return "Artifact publish does not allow %s" % extra
        if not all(isinstance(value, str) for value in tool_input.values()):
            return "Artifact publish arguments must be strings"
        page_error = _page_error(tool_input.get("file_path"), run_dir)
        if page_error:
            return page_error
        url = stored_url(run_dir)
        if url is None and "url" in tool_input:
            return "this review has no artifact yet; publish without url"
        if url is not None and tool_input.get("url") != url:
            return "Artifact publish must use url %s" % url
        return None
    if action == "read":
        extra = _extra_keys(tool_input, READ_KEYS)
        if extra:
            return "Artifact read does not allow %s" % extra
        if not isinstance(tool_input.get("prompt", ""), str):
            return "Artifact read prompt must be a string"
        url = stored_url(run_dir)
        if url is None or tool_input.get("url") != url:
            return "Artifact read is limited to this PR's review artifact"
        return None
    return "Artifact action %r is not allowed" % (action,)


def check_agent(tool_input: Dict[str, Any]) -> Optional[str]:
    if "isolation" in tool_input:
        return "Agent isolation is not allowed"
    extra = _extra_keys(tool_input, AGENT_KEYS)
    if extra:
        return "Agent does not allow %s" % extra
    if "subagent_type" in tool_input:
        kind = tool_input["subagent_type"]
        if not isinstance(kind, str) or kind not in AGENT_TYPES:
            return "Agent subagent_type must be general-purpose or Explore"
    return None


def decide(hook: Any, run_dir: Path) -> Optional[str]:
    if not isinstance(hook, dict) or not isinstance(hook.get("tool_input"), dict):
        return "unreadable hook input"
    tool = hook.get("tool_name")
    if tool == "Artifact":
        return check_artifact(hook["tool_input"], run_dir)
    if tool in AGENT_TOOLS:
        return check_agent(hook["tool_input"])
    return "%r is not a guarded tool" % (tool,)


def main(argv: List[str], stdin: IO[str]) -> int:
    try:
        if len(argv) != 1:
            raise ValueError("usage: pr-review guard <run_dir>")
        reason = decide(json.loads(stdin.read()), Path(argv[0]))
    except BaseException as error:
        reason = "failed closed: %s" % type(error).__name__
    if reason is None:
        return 0
    print(PREFIX + " ".join(reason.split()), file=sys.stderr)
    return 2

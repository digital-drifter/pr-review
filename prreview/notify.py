import json
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from prreview.config import REPO_SLUG, SLACK_CHANNEL, SLACK_KEYCHAIN_SERVICE, WORKSPACE
from prreview.keychain import read_secret
from prreview.logs import log
from prreview.models import Counts, PullRequest

SLACK_URL = "https://slack.com/api/chat.postMessage"
SLACK_TOKEN = re.compile(r"xox[a-z]-[A-Za-z0-9-]+")
TIMEOUT_SECONDS = 15
ARTIFACT_URL = re.compile(r"https://claude\.ai/(code/)?artifact/[A-Za-z0-9_-]+")
PR_URL = re.compile(r"https://bitbucket\.org/%s/%s/pull-requests/[0-9]+" % (re.escape(WORKSPACE), re.escape(REPO_SLUG)))


def is_artifact_url(url: Any) -> bool:
    return isinstance(url, str) and ARTIFACT_URL.fullmatch(url) is not None


class SlackError(Exception):
    pass


def escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def default_opener(request: urllib.request.Request) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise SlackError("Slack HTTP %d" % error.code)


def post_slack(
    text: str,
    opener: Callable[[urllib.request.Request], Dict[str, Any]] = default_opener,
) -> None:
    payload = {"channel": SLACK_CHANNEL, "text": text, "unfurl_links": False, "unfurl_media": False}
    request = urllib.request.Request(
        SLACK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + read_secret(SLACK_KEYCHAIN_SERVICE),
        },
        method="POST",
    )
    body = opener(request)
    if not body.get("ok"):
        raise SlackError("Slack rejected the message: %s" % body.get("error", "unknown"))


def notify_macos(text: str) -> None:
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e", "on run argv",
            "-e", 'display notification (item 1 of argv) with title "PR review"',
            "-e", "end run",
            text,
        ],
        capture_output=True,
        timeout=15,
    )


def send(
    text: str,
    post: Callable[[str], None] = post_slack,
    fallback: Callable[[str], None] = notify_macos,
) -> bool:
    try:
        post(text)
        return True
    except Exception as error:
        log("Slack send failed: %s" % SLACK_TOKEN.sub("[redacted]", "%s: %s" % (type(error).__name__, error)))
        try:
            fallback(text)
        except Exception:
            pass
        return False


def _plural(count: int, word: str) -> str:
    return "%d %s" % (count, word if count == 1 else word + "s")


def _links(pr: PullRequest, review_url: str) -> str:
    links = []
    if PR_URL.fullmatch(pr.link):
        links.append("<%s|View PR>" % pr.link)
    if is_artifact_url(review_url):
        links.append("<%s|Read review>" % review_url)
    return " · ".join(links)


def _lines(*lines: str) -> str:
    return "\n".join(line for line in lines if line)


def format_review_ready(pr: PullRequest, one_line: str, counts: Counts, review_url: str) -> str:
    parts: List[str] = []
    if counts.critical:
        parts.append("%d critical" % counts.critical)
    if counts.should_fix:
        parts.append("%d should fix" % counts.should_fix)
    if counts.nit:
        parts.append(_plural(counts.nit, "nit"))
    return _lines(
        "🔍 *Review ready: PR #%d %s*" % (pr.id, escape_mrkdwn(pr.title)),
        escape_mrkdwn(one_line),
        " · ".join(parts) if parts else "No issues found",
        _links(pr, review_url),
    )


def format_update(pr: PullRequest, new: Counts, fixed_count: int, review_url: str, full_fallback: bool) -> str:
    parts: List[str] = []
    if new.critical:
        parts.append("%d new critical" % new.critical)
    if new.should_fix:
        parts.append("%d new should fix" % new.should_fix)
    if new.nit:
        parts.append(_plural(new.nit, "new nit"))
    if not parts:
        parts.append("no new issues")
    if fixed_count:
        parts.append("%s fixed" % _plural(fixed_count, "earlier finding"))
    label = "updated (rebased, full re-review)" if full_fallback else "updated"
    return _lines("🔁 *PR #%d %s:* %s" % (pr.id, label, ", ".join(parts)), _links(pr, review_url))


def format_long_run(pr: PullRequest, minutes: int, session: str) -> str:
    return _lines(
        "⏳ *PR #%d review has been running for %d minutes.* "
        "Watch it in iTerm with `screen -r %s` (detach with Ctrl-A, then D)." % (pr.id, minutes, session),
        _links(pr, ""),
    )


def format_fork(pr_id: int) -> str:
    return "PR #%d comes from a fork; fork PRs aren't auto-reviewed." % pr_id


def format_failure(pr_id: Optional[int], reason: str) -> str:
    if pr_id is None:
        return "⚠️ *PR review watcher:* %s" % escape_mrkdwn(reason)
    return "⚠️ *Review of PR #%d failed:* %s" % (pr_id, escape_mrkdwn(reason))

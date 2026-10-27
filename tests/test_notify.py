import contextlib
import io
import json
import unittest
import urllib.request
from typing import Any, Dict, List
from unittest import mock

from prreview.models import Counts, PullRequest
from prreview.notify import (
    SlackError,
    escape_mrkdwn,
    format_failure,
    format_long_run,
    format_review_ready,
    format_update,
    notify_macos,
    post_slack,
    send,
)

PR_URL = "https://bitbucket.org/acme/widgets/pull-requests/2834"
REVIEW = "https://claude.ai/artifact/x"


def make_pr(title: str = "Move attendee lookups") -> PullRequest:
    return PullRequest(2834, title, "", "Ann Author", False, "abc123def456", "feat", "master", PR_URL)


class FormatTest(unittest.TestCase):
    def test_review_ready_copy(self) -> None:
        self.assertEqual(
            format_review_ready(make_pr(), "Moves the attendee report onto invoice_items.", Counts(1, 2, 1), REVIEW),
            "🔍 *Review ready: PR #2834 Move attendee lookups*\n"
            "Moves the attendee report onto invoice_items.\n"
            "1 critical · 2 should fix · 1 nit\n"
            "<https://bitbucket.org/acme/widgets/pull-requests/2834|View PR> · <https://claude.ai/artifact/x|Read review>",
        )

    def test_review_ready_pluralizes_nits(self) -> None:
        text = format_review_ready(make_pr(), "S.", Counts(0, 0, 3), REVIEW)
        self.assertEqual(text.split("\n")[2], "3 nits")

    def test_review_ready_with_no_findings_says_no_issues_found(self) -> None:
        text = format_review_ready(make_pr(), "S.", Counts(0, 0, 0), REVIEW)
        self.assertEqual(text.split("\n")[2], "No issues found")

    def test_update_copy(self) -> None:
        links = "\n<%s|View PR> · <%s|Read review>" % (PR_URL, REVIEW)
        self.assertEqual(
            format_update(make_pr(), Counts(0, 1, 0), 2, REVIEW, False),
            "🔁 *PR #2834 updated:* 1 new should fix, 2 earlier findings fixed" + links,
        )
        self.assertEqual(
            format_update(make_pr(), Counts(0, 0, 0), 0, REVIEW, False),
            "🔁 *PR #2834 updated:* no new issues" + links,
        )
        self.assertEqual(
            format_update(make_pr(), Counts(1, 0, 2), 1, REVIEW, False).split("\n")[0],
            "🔁 *PR #2834 updated:* 1 new critical, 2 new nits, 1 earlier finding fixed",
        )
        self.assertEqual(
            format_update(make_pr(), Counts(0, 0, 0), 3, REVIEW, False).split("\n")[0],
            "🔁 *PR #2834 updated:* no new issues, 3 earlier findings fixed",
        )

    def test_update_full_fallback_copy(self) -> None:
        first = format_update(make_pr(), Counts(0, 1, 0), 0, REVIEW, True).split("\n")[0]
        self.assertEqual(first, "🔁 *PR #2834 updated (rebased, full re-review):* 1 new should fix")

    def test_links_are_built_only_from_validated_urls(self) -> None:
        bad_pr = PullRequest(2834, "T", "", "Ann Author", False, "abc123def456", "feat", "master", "https://evil.test/pull-requests/1")
        for url in ("https://evil.test/x", "https://claude.ai/artifact/x\n", "https://claude.ai/artifact/x|y"):
            with self.subTest(url=url):
                for text in (
                    format_review_ready(make_pr(), "S.", Counts(0, 0, 0), url),
                    format_update(make_pr(), Counts(0, 0, 0), 0, url, False),
                ):
                    self.assertNotIn("Read review", text)
                    self.assertNotIn(url, text)
                    self.assertTrue(text.endswith("<%s|View PR>" % PR_URL), text)
        text = format_review_ready(bad_pr, "S.", Counts(0, 0, 0), REVIEW)
        self.assertNotIn("View PR", text)
        self.assertTrue(text.endswith("<%s|Read review>" % REVIEW))
        self.assertEqual(format_update(bad_pr, Counts(0, 0, 0), 0, "x", False), "🔁 *PR #2834 updated:* no new issues")

    def test_long_run_copy(self) -> None:
        self.assertEqual(
            format_long_run(make_pr(), 60, "pr-review-2834"),
            "⏳ *PR #2834 review has been running for 60 minutes.* "
            "Watch it in iTerm with `screen -r pr-review-2834` (detach with Ctrl-A, then D).\n"
            "<%s|View PR>" % PR_URL,
        )

    def test_failure_copy(self) -> None:
        self.assertEqual(format_failure(2834, "timed out"), "⚠️ *Review of PR #2834 failed:* timed out")
        self.assertEqual(format_failure(None, "boom"), "⚠️ *PR review watcher:* boom")

    def test_pr_text_is_mrkdwn_escaped(self) -> None:
        self.assertEqual(escape_mrkdwn("<!channel> & <b>"), "&lt;!channel&gt; &amp; &lt;b&gt;")
        pr = make_pr("<!channel> & <b>")
        expected = "&lt;!channel&gt; &amp; &lt;b&gt;"
        self.assertIn(expected, format_review_ready(pr, "<@U1>", Counts(0, 0, 0), REVIEW))
        self.assertIn("&lt;@U1&gt;", format_review_ready(pr, "<@U1>", Counts(0, 0, 0), REVIEW))
        self.assertIn("&lt;!here&gt;", format_failure(1, "<!here>"))
        self.assertIn("<%s|View PR>" % PR_URL, format_review_ready(pr, "s", Counts(0, 0, 0), REVIEW))


class PostSlackTest(unittest.TestCase):
    def test_post_slack_payload_and_auth(self) -> None:
        seen: List[urllib.request.Request] = []

        def opener(request: urllib.request.Request) -> Dict[str, Any]:
            seen.append(request)
            return {"ok": True}

        with mock.patch("prreview.notify.read_secret", return_value="xoxb-secret"):
            post_slack("hello", opener)
        request = seen[0]
        self.assertEqual(request.full_url, "https://slack.com/api/chat.postMessage")
        self.assertEqual(request.get_header("Authorization"), "Bearer xoxb-secret")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"channel": "U0000000001", "text": "hello", "unfurl_links": False, "unfurl_media": False},
        )

    def test_post_slack_raises_on_ok_false(self) -> None:
        with mock.patch("prreview.notify.read_secret", return_value="xoxb-secret"):
            with self.assertRaises(SlackError) as ctx:
                post_slack("hi", lambda request: {"ok": False, "error": "channel_not_found"})
        self.assertIn("channel_not_found", str(ctx.exception))
        self.assertNotIn("xoxb-secret", str(ctx.exception))


class NotifyMacosTest(unittest.TestCase):
    def test_notify_macos_passes_text_as_argv(self) -> None:
        with mock.patch("prreview.notify.subprocess.run") as run:
            notify_macos('say "hi" & done')
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[-1], 'say "hi" & done')
        self.assertNotIn('say "hi"', " ".join(argv[:-1]))


class SendTest(unittest.TestCase):
    def test_send_returns_true_without_fallback(self) -> None:
        calls: List[str] = []
        self.assertTrue(send("x", post=lambda text: None, fallback=calls.append))
        self.assertEqual(calls, [])

    def test_send_falls_back_to_macos_when_slack_fails(self) -> None:
        for error in (SlackError("nope"), OSError("down"), TimeoutError("slow")):
            calls: List[str] = []

            def failing(text: str, error: Exception = error) -> None:
                raise error

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(send("msg", post=failing, fallback=calls.append))
            self.assertEqual(calls, ["msg"])

    def test_send_falls_back_when_token_missing(self) -> None:
        from prreview.keychain import SecretMissing

        calls: List[str] = []

        def failing(text: str) -> None:
            raise SecretMissing("none")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(send("msg", post=failing, fallback=calls.append))
        self.assertEqual(calls, ["msg"])

    def test_send_logs_the_slack_failure_without_the_token(self) -> None:
        def failing(text: str) -> None:
            raise SlackError("Slack rejected the message: invalid_auth for xoxb-123-456-abcDEF")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            send("msg", post=failing, fallback=lambda text: None)

        self.assertIn("Slack send failed: SlackError: Slack rejected the message: invalid_auth for [redacted]", out.getvalue())
        self.assertNotIn("xoxb-", out.getvalue())
        self.assertNotIn("msg", out.getvalue().replace("message", ""))


if __name__ == "__main__":
    unittest.main()

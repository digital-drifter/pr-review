import base64
import contextlib
import io
import subprocess
import unittest
import urllib.error
from typing import Dict, List
from unittest import mock
from urllib.parse import parse_qs, urlparse

from prreview import bitbucket
from prreview.bitbucket import (
    BitbucketAuthError,
    BitbucketTransientError,
    default_fetch,
    get_pr,
    list_reviewing_prs,
)
from prreview.keychain import SecretMissing, read_secret
from prreview.models import PullRequest


def api_pr(pr_id: int, description: object = "Body") -> Dict[str, object]:
    pr = {
        "id": pr_id,
        "title": "Title %d" % pr_id,
        "draft": False,
        "author": {"display_name": "Ann Author"},
        "source": {
            "commit": {"hash": "abcdef123456"}, "branch": {"name": "feature/x"},
            "repository": {"full_name": "acme/widgets"},
        },
        "destination": {"branch": {"name": "master"}},
        "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/%d" % pr_id}},
    }
    if description is not None:
        pr["description"] = description
    return pr


class FakeFetch:
    def __init__(self, pages: List[Dict[str, object]]) -> None:
        self.pages = pages
        self.urls: List[str] = []

    def __call__(self, url: str) -> Dict[str, object]:
        self.urls.append(url)
        return self.pages[len(self.urls) - 1]


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class BitbucketTest(unittest.TestCase):
    def test_list_query_filters_on_reviewer_uuid_and_open_state(self) -> None:
        fetch = FakeFetch([{"values": []}])
        list_reviewing_prs(fetch)
        parsed = urlparse(fetch.urls[0])
        self.assertEqual(parsed.path, "/2.0/repositories/acme/widgets/pullrequests")
        query = parse_qs(parsed.query)
        self.assertEqual(
            query["q"], ['reviewers.uuid="{00000000-0000-4000-8000-000000000001}" AND state="OPEN"']
        )
        self.assertEqual(query["pagelen"], ["50"])
        self.assertIn("values.source.commit.hash", query["fields"][0])

    def test_list_follows_next_pages(self) -> None:
        fetch = FakeFetch(
            [
                {"values": [api_pr(1), api_pr(2)], "next": "https://api.bitbucket.org/2.0/next?page=2"},
                {"values": [api_pr(3)]},
            ]
        )
        prs = list_reviewing_prs(fetch)
        self.assertEqual([p.id for p in prs], [1, 2, 3])
        self.assertEqual(fetch.urls[1], "https://api.bitbucket.org/2.0/next?page=2")

    def test_maps_api_fields_to_pull_request(self) -> None:
        fetch = FakeFetch([{"values": [api_pr(7)]}])
        self.assertEqual(
            list_reviewing_prs(fetch),
            [
                PullRequest(
                    id=7,
                    title="Title 7",
                    description="Body",
                    author="Ann Author",
                    draft=False,
                    head="abcdef123456",
                    source_branch="feature/x",
                    destination_branch="master",
                    link="https://bitbucket.org/acme/widgets/pull-requests/7",
                )
            ],
        )

    def test_list_skips_and_logs_malformed_items(self) -> None:
        no_source = {k: v for k, v in api_pr(2).items() if k != "source"}
        bad_head = dict(api_pr(3), source={"commit": {"hash": "../../etc"}, "branch": {"name": "x"}})
        string_id = dict(api_pr(4), id="4")
        null_title = dict(api_pr(5), title=None)
        fetch = FakeFetch([{"values": [api_pr(1), no_source, bad_head, "junk", string_id, null_title, api_pr(6)]}])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            prs = list_reviewing_prs(fetch)

        self.assertEqual([p.id for p in prs], [1, 6])
        self.assertEqual(out.getvalue().count("skipping malformed pull request"), 5)
        self.assertIn("skipping malformed pull request 2 from Bitbucket: KeyError", out.getvalue())

    def test_source_repository_is_requested_and_mapped(self) -> None:
        fork = dict(api_pr(2), source=dict(api_pr(2)["source"], repository={"full_name": "someone/widgets"}))
        fetch = FakeFetch([{"values": [api_pr(1), fork]}])

        prs = list_reviewing_prs(fetch)

        self.assertIn("values.source.repository.full_name", parse_qs(urlparse(fetch.urls[0]).query)["fields"][0])
        self.assertEqual([p.source_repo for p in prs], ["acme/widgets", "someone/widgets"])
        single = FakeFetch([fork])
        self.assertEqual(get_pr(2, single).source_repo, "someone/widgets")
        self.assertIn("source.repository.full_name", parse_qs(urlparse(single.urls[0]).query)["fields"][0])

    def test_missing_source_repository_is_malformed_not_a_fork(self) -> None:
        source = api_pr(1)["source"]
        cases = [
            dict(api_pr(3), source=dict(source, repository=None)),
            dict(api_pr(4), source=dict(source, repository={"full_name": None})),
            dict(api_pr(5), source=dict(source, repository={"full_name": ""})),
            dict(api_pr(6), source={k: v for k, v in source.items() if k != "repository"}),
        ]
        fetch = FakeFetch([{"values": [api_pr(1)] + cases + [api_pr(7)]}])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            prs = list_reviewing_prs(fetch)

        self.assertEqual([p.id for p in prs], [1, 7])
        for pr_id in (3, 4, 5, 6):
            self.assertIn("skipping malformed pull request %d from Bitbucket: ValueError" % pr_id, out.getvalue())
        for case in cases:
            with self.assertRaisesRegex(ValueError, "malformed pull request"):
                get_pr(case["id"], FakeFetch([case]))

    def test_get_pr_rejects_a_malformed_pull_request(self) -> None:
        with self.assertRaises(ValueError):
            get_pr(9, FakeFetch([dict(api_pr(9), source={"commit": {"hash": "zz"}, "branch": {"name": "x"}})]))

    def test_missing_or_null_description_maps_to_empty_string(self) -> None:
        self.assertEqual(get_pr(1, FakeFetch([api_pr(1, None)])).description, "")
        self.assertEqual(get_pr(1, FakeFetch([dict(api_pr(1), description=None)])).description, "")

    def test_get_pr_requests_single_pull_request(self) -> None:
        fetch = FakeFetch([api_pr(9)])
        self.assertEqual(get_pr(9, fetch).id, 9)
        self.assertEqual(
            urlparse(fetch.urls[0]).path, "/2.0/repositories/acme/widgets/pullrequests/9"
        )

    def test_auth_header_uses_email_and_keychain_token(self) -> None:
        captured = []

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured.append((request, timeout))
            return FakeResponse(b'{"ok": true}')

        with mock.patch.object(bitbucket, "read_secret", return_value="tok") as secret, mock.patch.object(
            bitbucket.urllib.request, "urlopen", fake_urlopen
        ):
            self.assertEqual(default_fetch("https://api.bitbucket.org/2.0/x"), {"ok": True})
        secret.assert_called_once_with("bitbucket-api-token")
        request, timeout = captured[0]
        expected = "Basic " + base64.b64encode(b"reviewer@example.com:tok").decode()
        self.assertEqual(request.get_header("Authorization"), expected)
        self.assertEqual(timeout, 30)

    def raise_from_urlopen(self, error: BaseException) -> None:
        def fake_urlopen(request: object, timeout: float) -> None:
            raise error

        with mock.patch.object(bitbucket, "read_secret", return_value="tok"), mock.patch.object(
            bitbucket.urllib.request, "urlopen", fake_urlopen
        ):
            default_fetch("https://api.bitbucket.org/2.0/x")

    def http_error(self, code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://x", code, "msg", {}, None)

    def test_401_raises_auth_error(self) -> None:
        for code in (401, 403):
            with self.assertRaises(BitbucketAuthError):
                self.raise_from_urlopen(self.http_error(code))

    def test_500_and_urlerror_raise_transient_error(self) -> None:
        for error in (
            self.http_error(500),
            self.http_error(503),
            urllib.error.URLError("down"),
            TimeoutError("slow"),
        ):
            with self.assertRaises(BitbucketTransientError):
                self.raise_from_urlopen(error)

    def test_other_http_errors_are_not_auth_errors(self) -> None:
        with self.assertRaises(BitbucketTransientError) as ctx:
            self.raise_from_urlopen(self.http_error(404))
        self.assertNotIsInstance(ctx.exception, BitbucketAuthError)

    def test_missing_secret_propagates(self) -> None:
        with mock.patch.object(bitbucket, "read_secret", side_effect=SecretMissing("x")):
            with self.assertRaises(SecretMissing):
                default_fetch("https://api.bitbucket.org/2.0/x")


class KeychainTest(unittest.TestCase):
    def test_read_secret_strips_newline_and_passes_service(self) -> None:
        done = mock.Mock(returncode=0, stdout="sekret\n")
        with mock.patch("prreview.keychain.subprocess.run", return_value=done) as run:
            self.assertEqual(read_secret("svc"), "sekret")
        self.assertEqual(
            run.call_args[0][0], ["/usr/bin/security", "find-generic-password", "-s", "svc", "-w"]
        )

    def test_read_secret_raises_without_output_in_message(self) -> None:
        done = mock.Mock(returncode=44, stdout="leak", stderr="leak")
        with mock.patch("prreview.keychain.subprocess.run", return_value=done):
            with self.assertRaises(SecretMissing) as ctx:
                read_secret("svc")
        self.assertNotIn("leak", str(ctx.exception))

    def test_read_secret_has_a_timeout_and_reports_it_as_missing(self) -> None:
        done = mock.Mock(returncode=0, stdout="sekret\n")
        with mock.patch("prreview.keychain.subprocess.run", return_value=done) as run:
            read_secret("svc")
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        expired = subprocess.TimeoutExpired(["/usr/bin/security"], 30)
        with mock.patch("prreview.keychain.subprocess.run", side_effect=expired):
            with self.assertRaisesRegex(SecretMissing, "svc timed out"):
                read_secret("svc")


if __name__ == "__main__":
    unittest.main()

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List
from urllib.parse import urlencode

from prreview.config import BB_EMAIL, BB_KEYCHAIN_SERVICE, REPO_SLUG, REVIEWER_UUID, WORKSPACE
from prreview.keychain import read_secret
from prreview.logs import log
from prreview.models import PullRequest

HEAD_PATTERN = re.compile(r"[0-9a-f]{12,40}")

API = "https://api.bitbucket.org/2.0/repositories/%s/%s/pullrequests" % (WORKSPACE, REPO_SLUG)
PR_FIELDS = (
    "id,title,description,draft,author.display_name,source.commit.hash,"
    "source.branch.name,source.repository.full_name,destination.branch.name,links.html.href"
)
TIMEOUT_SECONDS = 30


class BitbucketAuthError(Exception):
    pass


class BitbucketTransientError(Exception):
    pass


def default_fetch(url: str) -> Dict[str, Any]:
    credentials = "%s:%s" % (BB_EMAIL, read_secret(BB_KEYCHAIN_SERVICE))
    token = base64.b64encode(credentials.encode()).decode()
    request = urllib.request.Request(
        url, headers={"Authorization": "Basic " + token, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise BitbucketAuthError("Bitbucket rejected credentials (HTTP %d)" % error.code)
        raise BitbucketTransientError("Bitbucket HTTP %d" % error.code)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise BitbucketTransientError("Bitbucket request failed: %s" % type(error).__name__)


def to_pull_request(data: Dict[str, Any]) -> PullRequest:
    pr = PullRequest(
        id=data["id"],
        title=data["title"],
        description=data.get("description") or "",
        author=data["author"]["display_name"],
        draft=bool(data.get("draft", False)),
        head=data["source"]["commit"]["hash"],
        source_branch=data["source"]["branch"]["name"],
        destination_branch=data["destination"]["branch"]["name"],
        link=data["links"]["html"]["href"],
        source_repo=(data["source"].get("repository") or {}).get("full_name") or "",
    )
    texts = (pr.title, pr.description, pr.author, pr.head, pr.source_branch, pr.destination_branch, pr.link, pr.source_repo)
    if (
        type(pr.id) is not int
        or not all(isinstance(text, str) for text in texts)
        or not HEAD_PATTERN.fullmatch(pr.head)
        or not pr.source_repo
    ):
        raise ValueError("malformed pull request")
    return pr


def list_reviewing_prs(fetch: Callable[[str], Dict[str, Any]] = default_fetch) -> List[PullRequest]:
    query = urlencode(
        {
            "q": 'reviewers.uuid="%s" AND state="OPEN"' % REVIEWER_UUID,
            "pagelen": 50,
            "fields": "next,"
            + ",".join("values." + field for field in PR_FIELDS.split(",")),
        }
    )
    url = "%s?%s" % (API, query)
    prs: List[PullRequest] = []
    while url:
        page = fetch(url)
        for item in page.get("values", []):
            try:
                prs.append(to_pull_request(item))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                item_id = item.get("id") if isinstance(item, dict) else None
                log("skipping malformed pull request %r from Bitbucket: %s" % (item_id, type(error).__name__))
        url = page.get("next", "")
    return prs


def get_pr(pr_id: int, fetch: Callable[[str], Dict[str, Any]] = default_fetch) -> PullRequest:
    return to_pull_request(fetch("%s/%d?%s" % (API, pr_id, urlencode({"fields": PR_FIELDS}))))

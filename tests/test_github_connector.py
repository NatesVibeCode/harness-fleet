"""GitHub artifacts as records: repos, READMEs, releases, issue titles."""
import base64

from harness_fleet import discover
from harness_fleet.discover import fetch_github_org

REPOS = [
    {"full_name": "acme/kafka-tools", "html_url": "https://github.com/acme/kafka-tools",
     "description": "Kafka migration helpers", "language": "Python", "stargazers_count": 42,
     "open_issues_count": 3, "pushed_at": "2026-09-01T10:00:00Z", "archived": False, "fork": False},
    {"full_name": "acme/old-thing", "html_url": "https://github.com/acme/old-thing",
     "description": "Retired", "language": "Go", "stargazers_count": 1,
     "open_issues_count": 0, "pushed_at": "2023-01-01T00:00:00Z", "archived": True, "fork": False},
    {"full_name": "someone/fork", "html_url": "https://github.com/someone/fork",
     "description": "not ours", "fork": True},
]
README = {"content": base64.b64encode(b"Kafka migration helpers for banks.").decode() + "\n"}
RELEASES = [{"name": "v1.2.0", "tag_name": "v1.2.0", "published_at": "2026-08-30T09:00:00Z",
             "body": "Adds a resumable partition rebalance.", "html_url": "https://github.com/acme/kafka-tools/releases/tag/v1.2.0"}]
ISSUES = [
    {"title": "Consumer lag on rebalance", "body": "Seen in production.", "state": "open",
     "updated_at": "2026-09-02T08:00:00Z", "html_url": "https://github.com/acme/kafka-tools/issues/7"},
    {"title": "A pull request, not an issue", "pull_request": {"url": "x"},
     "html_url": "https://github.com/acme/kafka-tools/pull/8"},
]


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, routes=None, status=200, headers=None):
        self.routes = routes or {}
        self.status = status
        self.headers = headers or {}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if url in self.routes:
            return _Resp(self.routes[url], self.status, self.headers)
        if url.endswith("/repos"):
            return _Resp(REPOS, self.status, self.headers)
        if url.endswith("/readme"):
            return _Resp(README, self.status, self.headers)
        if url.endswith("/releases"):
            return _Resp(RELEASES, self.status, self.headers)
        if url.endswith("/issues"):
            return _Resp(ISSUES, self.status, self.headers)
        return _Resp([], self.status, self.headers)

    def close(self):
        pass


def test_repos_readmes_releases_and_issues_become_records():
    client = _Client()
    records, skipped = fetch_github_org("acme", max_repos=5, client=client)

    kinds = [r.metadata["kind"] for r in records]
    # The fake serves the same README/releases/issues for every repo, so the
    # counts show the fan-out: two repos, each with its own artifacts.
    assert kinds.count("repository") == 2, "forks are not the firm's work"
    assert kinds.count("readme") == 2
    assert kinds.count("release") == 2
    assert kinds.count("issue") == 2, "pull requests are not issues"

    readme = next(r for r in records if r.metadata["kind"] == "readme")
    assert "Kafka migration helpers for banks." in readme.text
    assert readme.metadata["captured_at"] == "2026-09-01T10:00:00Z"

    release = next(r for r in records if r.metadata["kind"] == "release")
    assert release.metadata["captured_at"] == "2026-08-30T09:00:00Z"
    assert "resumable partition rebalance" in release.text

    archived = next(r for r in records if r.metadata.get("archived") is True)
    assert "Archived" in archived.text


def test_rate_limit_is_reported_not_silently_empty():
    client = _Client(status=403, headers={"x-ratelimit-remaining": "0"})
    records, skipped = fetch_github_org("acme", client=client)
    assert records == []
    assert skipped and "rate limit" in skipped[0]["reason"].lower()


def test_missing_org_is_reported():
    class NotFound(_Client):
        def get(self, url, params=None, timeout=None, headers=None):
            return _Resp({"message": "Not Found"}, status=404)

    records, skipped = fetch_github_org("nope", client=NotFound())
    assert records == []
    assert skipped and "404" in skipped[0]["reason"]


def test_token_header_is_optional(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    headers = discover._github_headers()
    assert "Authorization" not in headers
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-used")
    assert discover._github_headers()["Authorization"] == "Bearer test-token-not-used"


def test_json_errors_are_reported_as_skips():
    """A malformed API response is a skip with a reason, never a silent zero."""
    class Bad(_Client):
        def get(self, url, params=None, timeout=None, headers=None):
            return _Resp(ValueError("not json"))

    records, skipped = fetch_github_org("acme", client=Bad())
    assert records == []
    assert skipped and "invalid JSON" in skipped[0]["reason"]


def test_unauthorized_is_reported():
    client = _Client(status=401)
    records, skipped = fetch_github_org("acme", client=client)
    assert records == []
    assert skipped and "401" in skipped[0]["reason"]

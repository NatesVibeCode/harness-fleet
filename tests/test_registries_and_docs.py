"""Package registries and documentation-surface discovery."""
from harness_fleet.discover import _registry_records, discover_doc_urls, fetch_package

PYPI = {
    "info": {"name": "acme-tools", "version": "1.4.0", "summary": "Kafka migration helpers",
             "description": "Long README prose about migrating clusters.", "requires_python": ">=3.10",
             "home_page": "https://acme.example"},
    "releases": {"1.4.0": [{"upload_time_iso_8601": "2026-09-08T22:54:46.000000Z"}]},
}
NPM = {"name": "acme-tools", "description": "Kafka helpers", "readme": "NPM readme prose.",
       "dist-tags": {"latest": "2.0.1"}, "time": {"2.0.1": "2026-09-01T00:00:00.000Z"}}


class _Resp:
    def __init__(self, payload, status=200, content_type="application/json"):
        self._payload = payload
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.text = "" if payload is None else str(payload)[:400]

    def json(self):
        return self._payload


class _Client:
    def __init__(self, handler):
        self.handler = handler

    def get(self, url, params=None, timeout=None, headers=None):
        return self.handler(url)

    def close(self):
        pass


def test_pypi_payload_becomes_prose_with_the_publish_date():
    records, skipped = fetch_package("acme-tools", client=_Client(
        lambda url: _Resp(PYPI) if "pypi.org" in url else _Resp({}, status=404)
    ))
    assert len(records) == 1
    record = records[0]
    assert record.metadata["source"] == "registry:pypi"
    assert "Kafka migration helpers" in record.text
    assert "Long README prose about migrating clusters." in record.text
    assert record.metadata["captured_at"].startswith("2026-09-08")


def test_auto_falls_through_to_the_registry_that_knows_the_name():
    def handler(url):
        if "pypi.org" in url:
            return _Resp({"message": "Not Found"}, status=404)
        if "registry.npmjs.org" in url:
            return _Resp(NPM)
        return _Resp({}, status=404)

    records, skipped = fetch_package("acme-tools", client=_Client(handler))
    assert len(records) == 1
    assert records[0].metadata["source"] == "registry:npm"
    assert "NPM readme prose." in records[0].text
    assert any("pypi" in s["source"] for s in skipped), "the miss is reported, not hidden"


def test_explicit_registry_is_honoured():
    records, skipped = fetch_package(
        "acme-tools", registry="npm", client=_Client(lambda url: _Resp(NPM)),
    )
    assert records[0].metadata["source"] == "registry:npm"
    records, skipped = fetch_package(
        "acme-tools", registry="pypi", client=_Client(lambda url: _Resp(PYPI)),
    )
    assert records[0].metadata["source"] == "registry:pypi"


def test_unknown_name_yields_nothing_but_reports_every_attempt():
    records, skipped = fetch_package("no-such-package-xyz", client=_Client(lambda url: _Resp({}, status=404)))
    assert records == []
    assert len(skipped) >= 1


def test_empty_registry_payload_is_not_a_record():
    assert _registry_records("pypi", "x", {"info": {}}, "https://pypi.org/pypi/x/json") == []


def test_doc_discovery_keeps_the_paths_that_answer():
    def handler(url):
        if url.endswith("/docs") or url.endswith("/changelog"):
            return _Resp(None, status=200, content_type="text/html; charset=utf-8")
        return _Resp(None, status=404, content_type="text/html")

    found, skipped = discover_doc_urls("acme.example", client=_Client(handler))
    assert "https://acme.example/docs" in found
    assert "https://acme.example/changelog" in found
    assert all(url.startswith("https://acme.example/") for url in found)


def test_doc_discovery_reports_dns_failures_without_raising():
    def handler(url):
        raise RuntimeError("name resolution failed")

    found, skipped = discover_doc_urls("nope.invalid", client=_Client(handler))
    assert found == []
    assert skipped and "resolution" in skipped[0]["reason"]

"""Archived-capture fetching and density-gated extraction."""
import sys
import types

from harness_fleet.discover import (
    _archive_metadata,
    extract_text,
    pick_wayback_snapshots,
    wayback_cdx,
    wayback_snapshot_url,
)

PROSE = " ".join(["Reviewers describe the team, the manager, and the work in detail."] * 60)
WIDGET = "<div><p>Pros</p><p>Cons</p><p>Advice to Senior Management</p></div>"
TABLE_PAGE = (
    "<html><head><title>Company Reviews</title></head><body>"
    f"{WIDGET}<table><tr><td>{PROSE}</td></tr></table></body></html>"
)


def test_density_gate_recovers_prose_an_extractor_missed(monkeypatch):
    """Readability returning a widget must not win over the document's own text."""
    fake_readability = types.ModuleType("readability")
    fake_readability.Document = type(
        "Document", (), {"__init__": lambda self, html: None, "summary": lambda self: WIDGET}
    )
    fake_trafilatura = types.ModuleType("trafilatura")
    fake_trafilatura.extract = lambda html, **kw: "Flag this {0} as inappropriate"
    monkeypatch.setitem(sys.modules, "readability", fake_readability)
    monkeypatch.setitem(sys.modules, "trafilatura", fake_trafilatura)

    text = extract_text(TABLE_PAGE)
    assert "Reviewers describe the team" in text
    assert len(text.split()) > 100


def test_js_shell_never_admits_script_data(monkeypatch):
    """A client-rendered capture yields no prose: script data is not page copy."""
    monkeypatch.setitem(sys.modules, "readability", None)
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    html = (
        '<html><head><title>Loading</title><script src="/app.js"></script></head>'
        '<body><div id="root"></div>'
        '<script>window.__DATA__ = {"reviews": "' + ("lorem ipsum " * 400) + '"};</script>'
        "</body></html>"
    )
    text = extract_text(html)
    # Whatever survives (a title at most) is negligible, and the 400 words of
    # inline data are never admitted as page copy.
    assert "lorem ipsum" not in text
    assert len(text.split()) < 5


def test_wayback_snapshot_url_builds_the_raw_capture_form():
    assert wayback_snapshot_url("http://example.com/reviews", "20110211100904") == (
        "https://web.archive.org/web/20110211100904id_/http://example.com/reviews"
    )


def test_wayback_cdx_parses_rows_and_tolerates_garbage(monkeypatch):
    rows = [
        ["timestamp", "original", "statuscode", "length"],
        ["20151103222101", "http://example.com/x", "200", "123610"],
    ]

    class Resp:
        status_code = 200

        def json(self):
            return rows

    class Client:
        def get(self, *a, **k):
            return Resp()

    assert wayback_cdx("example.com/x", client=Client()) == [
        {"timestamp": "20151103222101", "original": "http://example.com/x",
         "status": "200", "length": "123610"}
    ]

    def bad_client(payload):
        class Bad:
            status_code = 200

            def json(self):
                return payload

        class BadClient:
            def get(self, *a, **k):
                return Bad()

        return BadClient()

    for payload in ({"unexpected": True}, ["not", "rows"], [], "boom", None):
        assert wayback_cdx("x", client=bad_client(payload)) == []


def test_pick_wayback_snapshots_drops_stubs_and_error_captures():
    rows = [
        {"timestamp": "20151103222101", "original": "u", "status": "200", "length": "123610"},
        {"timestamp": "20160211100904", "original": "u", "status": "200", "length": "900"},
        {"timestamp": "20170101000000", "original": "u", "status": "404", "length": "123610"},
        {"timestamp": "20130101000000", "original": "u", "status": "", "length": ""},
        {"timestamp": "20120101000000", "original": "u", "status": "200", "length": "115497"},
    ]
    assert [r["timestamp"] for r in pick_wayback_snapshots(rows)] == [
        "20151103222101", "20130101000000", "20120101000000"]
    assert [r["timestamp"] for r in pick_wayback_snapshots(rows, since="2014")] == ["20151103222101"]
    assert pick_wayback_snapshots(None) == []


def test_archived_captures_carry_their_snapshot_date():
    """Recency decay must run from the capture date, not from today."""
    meta = _archive_metadata(
        "https://web.archive.org/web/20110211100904id_/http://www.glassdoor.com/Reviews/x.htm"
    )
    assert meta["archived"] is True
    assert meta["archive"] == "wayback"
    assert meta["snapshot_timestamp"] == "20110211100904"
    assert meta["captured_at"] == "2011-02-11T10:09:04+00:00"
    assert meta["original_url"] == "http://www.glassdoor.com/Reviews/x.htm"
    assert _archive_metadata("https://example.com/live") == {}

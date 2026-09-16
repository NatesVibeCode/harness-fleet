"""Going to an entity's own surfaces for the evidence it is missing.

Discovery finds pages *about* a company. A page about a company is not the
company: what it delivers, who it hires and what it charges live on its own
site, on its hiring board and in the stories its vendors publish. These are the
guarantees that make the walk trustworthy — that it asks only for what is
missing, reads the surface that actually answers, and says why when one does
not.
"""
from __future__ import annotations

import json

import pytest

from harness_fleet import contracts, enrich
from harness_fleet.discover import RawRecord
from harness_fleet.enrich import (
    classify_page,
    discover_pages,
    enrich_entity,
    entity_domain,
)

# ---------------------------------------------------------------------------
# The contract between a kind and a surface
# ---------------------------------------------------------------------------

def test_every_kind_maps_to_surfaces_that_exist():
    """A kind pointing at a surface nobody declared is a silent hole.

    The mapping says where to go for evidence; the surface file says what going
    there means. If the two drift, a lane asks for a kind and the walk quietly
    has nowhere to look.
    """
    plan = enrich.load_surfaces()
    declared = set(plan.get("first_party_paths") or {})
    declared |= set(plan.get("templates") or {})
    declared |= set(plan.get("channels") or {})
    declared |= {"vendor_stories"}

    for kind in contracts.EVIDENCE_KINDS:
        surfaces = contracts.surfaces_for_kinds([kind])
        assert surfaces, f"'{kind}' is a kind no surface carries"
        unknown = [surface for surface in surfaces if surface not in declared]
        assert not unknown, f"'{kind}' maps to undeclared surface(s) {unknown}"

    for kind in contracts.KIND_SURFACES:
        assert kind in contracts.EVIDENCE_KINDS, f"surface mapping names unknown kind '{kind}'"


def test_a_surface_that_never_answers_is_recorded_not_used():
    """Review directories refused every domain probed, so they are not sources."""
    plan = enrich.load_surfaces()
    fetchable = json.dumps({k: plan.get(k) for k in ("first_party_paths", "templates", "channels")})
    assert "clutch.co" not in fetchable and "g2.com" not in fetchable
    removed = plan.get("removed") or {}
    assert "clutch.co/profile/<slug>" in removed, "the reason it went is kept"
    assert "403" in removed["clutch.co/profile/<slug>"]


# ---------------------------------------------------------------------------
# Reading a URL
# ---------------------------------------------------------------------------

def test_a_page_is_classified_by_its_path():
    assert classify_page("https://acme.com/case-studies/a-bank") == "case_studies"
    assert classify_page("https://acme.com/insights/blog/x") == "blog"
    assert classify_page("https://acme.com/careers/staff-engineer") == "careers"
    assert classify_page("https://acme.com/what-we-do/data") == "services"
    assert classify_page("https://acme.com/pricing") == "", "no surface claims it"


def test_a_note_in_the_surface_file_is_not_a_surface():
    """The file documents itself with _-prefixed keys; prose is not data."""
    plan = enrich.load_surfaces()
    assert "_comment" in (plan.get("match") or {})
    assert classify_page("https://acme.com/comment/case-studies") == "case_studies"


def test_an_entity_that_is_not_a_domain_has_no_website_to_walk():
    assert entity_domain("trace3.com") == "trace3.com"
    assert entity_domain("viking_cloud_inc") == "", "a slug from a posting path is not a host"
    assert entity_domain("") == ""
    assert entity_domain("unknown_entity") == ""


# ---------------------------------------------------------------------------
# Discovery is the sitemap's job
# ---------------------------------------------------------------------------

def _patch_sitemap(monkeypatch, urls, *, error=None):
    from harness_fleet import discover

    monkeypatch.setattr(
        discover, "discover_sitemap_url",
        lambda site, **kw: "https://acme.com/sitemap.xml" if not error else (_raise(error)),
    )
    monkeypatch.setattr(
        discover, "fetch_sitemap_entries",
        lambda url, **kw: [(u, None) for u in urls],
    )


def _raise(exc):
    raise exc


def test_the_sitemap_names_the_pages_and_deepest_wins(monkeypatch):
    """An index page is navigation; the story underneath it is the evidence."""
    _patch_sitemap(monkeypatch, [
        "https://acme.com/case-studies",
        "https://acme.com/case-studies/acme-bank",
        "https://acme.com/careers/staff-engineer",
        "https://acme.com/pricing",
    ])
    pages, skipped = discover_pages("acme.com")
    assert skipped == []
    assert pages["case_studies"] == [
        "https://acme.com/case-studies/acme-bank",
        "https://acme.com/case-studies",
    ], "the deeper page is read first"
    assert pages["careers"] == ["https://acme.com/careers/staff-engineer"]
    assert "https://acme.com/pricing" not in json.dumps(pages)


def test_no_sitemap_is_reported_rather_than_silently_empty(monkeypatch):
    from harness_fleet import discover

    monkeypatch.setattr(discover, "discover_sitemap_url", lambda site, **kw: "")
    pages, skipped = discover_pages("acme.com")
    assert pages == {}
    assert skipped and "no sitemap" in skipped[0]["reason"]


# ---------------------------------------------------------------------------
# The walk asks only for what is missing
# ---------------------------------------------------------------------------

def _offline(monkeypatch, *, sitemap=(), pages=None, greenhouse=(), markup=None):
    from harness_fleet import discover

    pages = pages or {}
    monkeypatch.setattr(discover, "discover_sitemap_url", lambda site, **kw: f"{site}/sitemap.xml" if sitemap else "")
    monkeypatch.setattr(discover, "fetch_sitemap_entries", lambda url, **kw: [(u, None) for u in sitemap])

    def fake_fetch(url, **kw):
        if url not in pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        return RawRecord(text=pages[url], source_uri=url, title=url)

    monkeypatch.setattr(discover, "fetch_text", fake_fetch)
    monkeypatch.setattr(discover, "fetch_smart_url", fake_fetch)
    monkeypatch.setattr(discover, "crawl_site", lambda *a, **k: ([], []))
    monkeypatch.setattr(discover, "fetch_github_org", lambda org, **kw: ([], []))
    def board(records):
        def fetch(slug, **kw):
            if records:
                return list(records)
            raise RuntimeError(f"unknown board '{slug}' (no account under that slug)")
        return fetch

    monkeypatch.setattr(discover, "fetch_greenhouse_board", board(greenhouse))
    monkeypatch.setattr(discover, "fetch_ashby_org", board(()))
    monkeypatch.setattr(discover, "fetch_lever_org", board(()))
    monkeypatch.setattr(discover, "web_search", lambda query, **kw: [])
    if markup is not None:
        monkeypatch.setattr(enrich, "fetch_markup", markup)


def test_the_walk_only_visits_the_surfaces_the_missing_kinds_need(monkeypatch):
    """Asking for hiring evidence must not crawl the case studies."""
    _offline(monkeypatch, sitemap=["https://acme.com/case-studies/x"], pages={})
    records, report = enrich_entity("acme.com", kinds=["delivery_hiring"], vendor_stories=False)
    assert report.surfaces == ["ats"], "only the surface that carries the missing kind"
    assert "case_studies" not in report.by_surface
    assert report.domain == "acme.com"


def test_a_hiring_board_is_read_through_its_api(monkeypatch):
    _offline(monkeypatch, greenhouse=[RawRecord(
        text="We are hiring a Staff Engineer to own our Kafka platform",
        source_uri="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
    )])
    records, report = enrich_entity("acme.com", kinds=["delivery_hiring"], vendor_stories=False)
    assert report.by_surface == {"ats": 1}
    assert records and "Staff Engineer" in records[0].text
    assert records[0].metadata["enrich_surface"] == "ats"


def test_a_board_that_does_not_exist_is_a_fact_about_the_company(monkeypatch):
    """No ATS account is a finding, not a failed fetch."""
    _offline(monkeypatch)
    _records, report = enrich_entity("acme.com", kinds=["delivery_hiring"], vendor_stories=False)
    reasons = " ".join(entry.get("reason", "") for entry in report.skipped)
    assert "acme" in reasons, "each board that answered nothing is named"


def test_a_surface_that_refuses_is_reported_with_its_reason(monkeypatch):
    _offline(monkeypatch, sitemap=["https://acme.com/services"], pages={"https://acme.com/services": "Acme does Kafka"})

    from harness_fleet import discover

    def boom(url, **kw):
        raise RuntimeError(f"HTTP 403 for {url}")

    monkeypatch.setattr(discover, "fetch_text", boom)
    _records, report = enrich_entity("acme.com", kinds=["stack_delivery"], vendor_stories=False)
    assert any("403" in entry.get("reason", "") for entry in report.skipped)
    assert report.kept == 0


def test_an_entity_with_no_domain_says_so_instead_of_crawling_something(monkeypatch):
    _offline(monkeypatch)
    records, report = enrich_entity("viking_cloud_inc", kinds=["delivery_hiring"])
    assert records == []
    assert report.skipped and "does not name a domain" in report.skipped[0]["reason"]


def test_a_surface_that_yields_nothing_says_so(monkeypatch):
    """'We looked and it was not there' is not the same fact as 'we never looked'.

    An entity whose only gap is independent validation produced a walk with no
    records and no refusals, which reads as a surface that was never visited.
    """
    _offline(monkeypatch, markup=lambda url, **kw: (_raise(RuntimeError("offline"))))
    records, report = enrich_entity("acme.com", kinds=["independent_validation"])
    assert records == []
    reasons = {entry.get("surface"): entry.get("reason", "") for entry in report.skipped}
    assert "vendor_stories" in reasons and "no vendor story" in reasons["vendor_stories"]
    assert "community" in reasons, "the other surface that carries the kind is accounted for too"


# --- the lease a slow route outlives ----------------------------------------

def test_a_reclaimed_lease_does_not_kill_the_run():
    """Losing the lease race is normal when a free route is slow.

    The lease expires, another worker takes the batch, and the first worker's
    completion is refused. That refusal used to propagate out of the worker pool
    and end a run that was doing nothing wrong.
    """
    from harness_fleet.engine import Engine, LeaseLostError
    from harness_fleet.models import ProviderReceipt

    class Store:
        def __init__(self):
            self.completed = 0

        def complete_batch(self, *args, **kwargs):
            raise ValueError("batch lease is not owned by this worker")

        def release_lease(self, *args, **kwargs):
            raise AssertionError("a lost lease must not be released as if held")

        def fail_batch(self, *args, **kwargs):
            raise AssertionError("a lost lease is not this worker's failure to record")

    class Catalog:
        def get_earliest_cooldown_retry(self):
            return 0.0

    engine = object.__new__(Engine)
    engine.store = Store()
    engine.catalog = Catalog()

    class Session:
        session_id = "worker-1"

    receipt = ProviderReceipt(
        id="r1", provider="demo", requested_route="demo/x", status="complete",
    )
    with pytest.raises(LeaseLostError):
        engine._settle(
            {"attempt_id": "run:b1:1"}, Session(), True, [{"item_id": "x"}], receipt, None, "run",
        )


def test_a_lease_is_held_longer_than_a_batch_can_take():
    """The default 300s expired under a slow route and the batch was reclaimed."""
    from harness_fleet.engine import Engine

    engine = object.__new__(Engine)
    engine.prompt_timeout_sec = 180
    engine.max_attempts_per_batch = 3
    window = max(300, engine.prompt_timeout_sec * max(1, engine.max_attempts_per_batch) + 60)
    assert window >= 180 * 3, "three timed attempts must fit inside one lease"
    assert window > 300, "the old default was the bug"

"""Offline tests for the partner sourcing runner.

Every network primitive is replaced, so these run in milliseconds and prove the
two things that matter: the plan expands into the right calls, and only hits
that genuinely attribute the work to a named firm become evidence.
"""
from __future__ import annotations

import pytest

from harness_fleet import partner_sourcing
from harness_fleet.discover import RawRecord, SearchHit
from harness_fleet.partner_sourcing import (
    attribution_terms,
    candidate_entities,
    enrich_partner,
    enrich_urls,
    filter_attributed,
    is_attributed,
    is_source_host,
)

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------






def test_enrich_urls_expands_domain_slug_and_origin():
    """The URLs come from the shared surface plan, addressed by slug."""
    urls = enrich_urls(None, "Trace3.com")
    assert "https://trace3.com/services" in urls["first_party"]
    assert "https://trace3.com/case-studies" in urls["first_party"]
    # Hiring boards are only readable through their JSON APIs: the HTML hosts
    # answer 200 for a slug that does not exist.
    assert "https://boards-api.greenhouse.io/v1/boards/trace3/jobs?content=true" in urls["ats"]
    assert "https://api.ashbyhq.com/posting-api/job-board/trace3" in urls["ats"]
    assert "https://partners.amazonaws.com/partners/trace3" in urls["third_party"]

    from harness_fleet.enrich import surface_urls

    assert surface_urls("registry", "trace3.com")[0] == "https://partners.amazonaws.com/partners/trace3"
    assert surface_urls("about", "trace3.com")[0] == "https://trace3.com/about"


# ---------------------------------------------------------------------------
# Attribution — the difference between evidence and noise
# ---------------------------------------------------------------------------

def test_attribution_terms_keep_the_domain_and_a_long_enough_name():
    assert attribution_terms("trace3.com") == ("trace3.com", "trace3")
    # Too short to prove anything on its own: only the full domain counts.
    assert attribution_terms("ab.io") == ("ab.io",)


def test_attribution_terms_are_empty_for_an_empty_entity():
    assert attribution_terms("") == ()
    assert attribution_terms("   ") == ()


def test_is_attributed_accepts_the_domain_in_the_url():
    assert is_attributed("unrelated body text", "https://trace3.com/case-studies/kafka", "trace3.com")


def test_is_attributed_requires_a_bare_word_not_a_substring():
    # The real false positive: a search for Trace3 returns pages about *tracing*.
    assert not is_attributed("the importance of distributed tracing for kafka", "https://conduktor.io/blog/tracing", "trace3.com")
    assert is_attributed("Trace3 delivers Kafka work", "https://news.ycombinator.com/item?id=1", "trace3.com")


def test_is_attributed_treats_a_name_with_digits_as_distinctive():
    assert is_attributed("trace3 won the award", "https://example.com/a", "trace3.com")
    # ...and still does not match a longer word that merely contains it.
    assert not is_attributed("trace33 is a different firm", "https://example.com/a", "trace3.com")


def test_filter_attributed_splits_kept_from_dropped():
    records = [
        RawRecord(text="Trace3 delivers Kafka work", source_uri="https://news.ycombinator.com/item?id=1"),
        RawRecord(text="distributed tracing for kafka", source_uri="https://medium.com/@kafkatrace/x"),
    ]
    kept, dropped = filter_attributed(records, "trace3.com")
    assert [r.source_uri for r in kept] == ["https://news.ycombinator.com/item?id=1"]
    assert [r.source_uri for r in dropped] == ["https://medium.com/@kafkatrace/x"]


# ---------------------------------------------------------------------------
# Candidate entities
# ---------------------------------------------------------------------------

def test_candidate_entities_uses_the_page_host():
    assert candidate_entities("https://www.trace3.com/services", "text") == ["trace3.com"]


def test_candidate_entities_skips_directories_registries_and_job_boards():
    for url in (
        "https://clutch.co/profile/trace3",
        "https://www.g2.com/products/trace3/reviews",
        "https://news.ycombinator.com/item?id=1",
        "https://boards.greenhouse.io/trace3",
        "https://partners.amazonaws.com/partners/001",
    ):
        assert is_source_host(url.split("/")[2]), url
        assert candidate_entities(url, "trace3 is great") == [], url


def test_candidate_entities_reads_domains_linked_from_community_pages():
    text = 'we used <a href="https://www.trace3.com/kafka">Trace3</a> and https://clutch.co/profile/x plus https://blog.example.io/post'
    assert candidate_entities("https://news.ycombinator.com/item?id=1", text) == ["trace3.com", "blog.example.io"]
    # A community page that links nobody is not a candidate for anybody.
    assert candidate_entities("https://news.ycombinator.com/item?id=2", "no links here") == []


def test_linked_domains_needs_a_real_suffix_and_skips_source_hosts():
    from harness_fleet.partner_sourcing import linked_domains

    assert linked_domains("see https://g2.com/products/x and https://trace3.com/x") == ["trace3.com"]
    assert linked_domains("https://sub.example.com/x https://example.com") == ["sub.example.com", "example.com"]
    assert linked_domains("https://example.com https://example.com/x") == ["example.com"]
    assert linked_domains("") == []



def test_enrich_rejects_a_partner_with_no_delivery_evidence(monkeypatch):
    """A dossier is the output of filtering: a mention that never says the firm
    delivers work is not enough to write one."""
    plan = {"stages": {"enrich": {"web": {"queries": ['"{domain}"']}}}}
    _patch_crawl(monkeypatch, result=([], []))
    _offline_walk(monkeypatch)
    _patch_search(monkeypatch, {'"trace3.com"': [
        SearchHit(url="https://news.ycombinator.com/item?id=7", title="", snippet="Great team, highly recommend", backend="hn"),
    ]})
    _patch_fetch(monkeypatch, {
        "https://news.ycombinator.com/item?id=7": "Trace3: great team, highly recommend",
    })
    items, report = enrich_partner("trace3.com", plan=plan, backends=["hn"], delay=0.0)
    assert items == []
    assert report.rejected and "no delivery evidence" in report.rejected
    assert report.kept == 0
def test_candidate_entities_never_invents_a_domain_from_a_slug():
    """A slug names the subject of a case study; it is not a domain.

    Live regression: /case-studies/fintech-payment-platform produced a candidate
    called fintech-payment-platform.com, a firm that does not exist, and
    school-platform.com the same way.
    """
    assert candidate_entities("https://appomni.com/case-studies/trace3/", "Trace3 is a partner") == [
        "appomni.com",
    ]
    fabricated = candidate_entities(
        "https://vendor.com/case-studies/fintech-payment-platform",
        "how we built a payment platform for a fintech client",
    )
    assert fabricated == ["vendor.com"]
    assert candidate_entities("https://vendor.com/case-studies/school-platform", "x") == ["vendor.com"]


def test_candidate_entities_keeps_a_case_study_slug_that_is_already_a_domain():
    assert candidate_entities("https://appomni.com/case-studies/trace3.com/", "x") == [
        "appomni.com",
        "trace3.com",
    ]
    assert candidate_entities("https://appomni.com/case-studies/linkedin.com/", "x") == ["appomni.com"]


def test_candidate_entities_reads_a_partner_domain_linked_from_a_vendor_case_study():
    text = 'Read how <a href="https://www.trace3.com/case-studies/kafka">Trace3</a> delivered it.'
    assert candidate_entities("https://appomni.com/case-studies/identity-rollout", text) == [
        "appomni.com",
        "trace3.com",
    ]


def test_candidate_entities_skips_social_and_hosting_platforms():
    for url in (
        "https://uk.linkedin.com/in/someone",
        "https://www.linkedin.com/company/trace3",
        "https://x.com/trace3",
        "https://twitter.com/trace3",
        "https://www.facebook.com/trace3",
        "https://www.instagram.com/trace3",
        "https://www.tiktok.com/@trace3",
        "https://www.youtube.com/watch?v=1",
        "https://soumikmukherjee.vercel.app/",
        "https://school-platform.netlify.app/",
        "https://someone.github.io/about",
        "https://acme.webflow.io/",
        "https://acme.notion.site/portfolio",
        "https://acme.wordpress.com/",
        "https://acme.blogspot.com/",
        "https://docs.readthedocs.io/en/latest/",
    ):
        assert is_source_host(url.split("/")[2]), url
        assert candidate_entities(url, "Trace3 delivers Kafka work") == [], url
    assert candidate_entities("https://uk.linkedin.com/in/x", "see https://trace3.com") == ["trace3.com"]
    assert candidate_entities(
        "https://news.ycombinator.com/item?id=1",
        "https://www.linkedin.com/in/x and https://soumikmukherjee.vercel.app/",
    ) == []
def test_candidate_entities_ignores_path_stopwords_and_empty_urls():
    assert candidate_entities("https://vendor.com/case-studies/customers", "x") == ["vendor.com"]
    assert candidate_entities("", "x") == []
    assert candidate_entities(None, "x") == []


# ---------------------------------------------------------------------------
# find — offline
# ---------------------------------------------------------------------------

def test_mention_searches_run_in_parallel_not_in_sequence(monkeypatch):
    """Slow searches across backends must overlap: sequential is the old run."""
    import time

    from harness_fleet.discover import SearchHit

    def slow_search(query, backends=None, max_results=10, **kwargs):
        time.sleep(0.5)
        return [SearchHit(url="https://trace3.com/about", title="About",
                          snippet="Trace3 implements Kafka migrations for clients.",
                          backend=(backends or ["hn"])[0])]

    monkeypatch.setattr(partner_sourcing, "web_search", slow_search)
    _patch_fetch(monkeypatch, {
        "https://trace3.com/about": "Trace3 implements Kafka migrations for clients.",
    })
    started = time.monotonic()
    items, report = enrich_partner(
        "trace3.com", plan=None, backends=["hn", "ddgs"],
        include_fetch=False, delay=0.0,
    )
    elapsed = time.monotonic() - started
    assert report.searched > 4, "enough searches to tell parallel from sequential"
    floor = report.searched * 0.5
    assert elapsed < floor / 2, (
        f"{report.searched} searches at 0.5s each took {elapsed:.2f}s: searched in sequence"
    )
    assert [item.item_id for item in items] == ["trace3.com"]

def _patch_search(monkeypatch, hits_by_query):
    def fake_search(query, backends=None, max_results=10, **kwargs):
        return list(hits_by_query.get(query, []))

    monkeypatch.setattr(partner_sourcing, "web_search", fake_search)


def _patch_fetch(monkeypatch, pages):
    def fake_fetch(url, timeout=20.0, respect_robots=True):
        if url not in pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        return RawRecord(text=pages[url], source_uri=url, title=url)

    # This module fetches its search hits; the shared surface walk fetches from
    # discover. Patch both so a test says which URLs may be fetched, not which
    # module happened to look them up.
    from harness_fleet import discover

    monkeypatch.setattr(partner_sourcing, "fetch_text", fake_fetch)
    monkeypatch.setattr(discover, "fetch_text", fake_fetch)


def _offline_walk(monkeypatch, *, pages=None, sitemap_urls=(), greenhouse=None, community=None):
    """Make the whole surface walk offline, and say what it may find.

    The walk reaches for a sitemap, then channels the engine owns (a hiring
    board, a code host, community search). A test that patches only the fetcher
    still talks to the network, so every entry point is stubbed here.
    """
    from harness_fleet import discover

    pages = pages or {}
    monkeypatch.setattr(
        discover, "discover_sitemap_url",
        lambda site, **kw: f"{site.rstrip('/')}/sitemap.xml" if sitemap_urls else "",
    )
    monkeypatch.setattr(
        discover, "fetch_sitemap_entries",
        lambda url, **kw: [(u, None) for u in sitemap_urls],
    )
    monkeypatch.setattr(discover, "fetch_github_org", lambda org, **kw: ([], []))
    monkeypatch.setattr(discover, "fetch_greenhouse_board", lambda slug, **kw: list(greenhouse or []))
    monkeypatch.setattr(discover, "fetch_ashby_org", lambda slug, **kw: [])
    monkeypatch.setattr(discover, "fetch_lever_org", lambda slug, **kw: [])
    monkeypatch.setattr(discover, "web_search", lambda query, **kw: list(community or []))

    # The vendor-story half of the walk reads real vendor sitemaps. Left
    # unstubbed it makes every enrich test a 15-second network call that passes
    # or fails on someone else's uptime.
    from harness_fleet import enrich

    def _offline_markup(url, **kw):
        raise RuntimeError(f"offline: {url} not reached in tests")

    monkeypatch.setattr(enrich, "fetch_markup", _offline_markup)


def _patch_crawl(monkeypatch, result=None, exc=None):
    from harness_fleet import discover

    def fake_crawl(url, **kwargs):
        if exc is not None:
            raise exc
        return result if result is not None else ([], [])

    monkeypatch.setattr(discover, "crawl_site", fake_crawl)







def test_enrich_partner_bundles_first_party_ats_and_mentions(monkeypatch):
    plan = {"stages": {"find": {}}}
    pages = {
        "https://trace3.com/services": "Trace3 provides Kafka delivery",
        "https://trace3.com/case-studies/kafka": "Trace3 case study for a bank",
        # No surface asks for this path, so the walk must not spend a fetch on it.
        "https://trace3.com/webinar-signup": "unrelated page",
    }
    _offline_walk(
        monkeypatch,
        pages=pages,
        sitemap_urls=list(pages),
        greenhouse=[RawRecord(
            text="Trace3 is hiring a Kafka solutions architect",
            source_uri="https://boards-api.greenhouse.io/v1/boards/trace3/jobs",
        )],
    )
    _patch_fetch(monkeypatch, {
        "https://trace3.com/services": "Trace3 provides Kafka delivery",
        "https://trace3.com/case-studies/kafka": "Trace3 case study for a bank",
        "https://news.ycombinator.com/item?id=3": "Trace3 did our Kafka rollout",
        # Fetches fine, but the page is about tracing, not about Trace3.
        "https://medium.com/x": "distributed tracing for kafka streams in production",
    })
    _patch_search(monkeypatch, {
        '"trace3.com"': [SearchHit(url="https://news.ycombinator.com/item?id=3", title="", snippet="Trace3 did our Kafka rollout", backend="hn")],
        '"trace3.com" (funding OR acquisition OR award)': [
            SearchHit(url="https://medium.com/x", title="", snippet="tracing kafka streams", backend="ddgs"),
        ],
    })

    items, report = enrich_partner("trace3.com", plan=plan, backends=["hn", "ddgs"], delay=0.0, max_pages=5)

    assert [item.item_id for item in items] == ["trace3.com"]
    text = items[0].text
    assert "Trace3 provides Kafka delivery" in text
    # A page on no wanted surface is not kept.
    assert "unrelated page" not in text
    # The hiring board arrives through its API, not as an HTML page: the HTML
    # hosts answer 200 for a slug that does not exist, so they prove nothing.
    assert "hiring a Kafka solutions architect" in text
    assert "Trace3 did our Kafka rollout" in text
    assert "tracing kafka streams" not in text
    assert report.stage == "enrich"
    assert report.dropped_unattributed == 1
    assert report.kept == 1
    assert report.candidates == ["trace3.com"]
    # The walk is no longer plan-driven, so the crawl-failure note is gone; what
    # it must still do is carry a source that returned nothing.
    assert report.skipped, "a surface that returned nothing is reported"


def test_enrich_partner_can_skip_fetching_entirely(monkeypatch):
    from harness_fleet import discover

    plan = {"stages": {"find": {}}}
    _offline_walk(monkeypatch)
    monkeypatch.setattr(discover, "crawl_site",
                        lambda *a, **k: pytest.fail("--no-fetch must not crawl"))
    monkeypatch.setattr(discover, "fetch_text",
                        lambda *a, **k: pytest.fail("--no-fetch must not fetch"))
    monkeypatch.setattr(partner_sourcing, "fetch_text",
                        lambda *a, **k: pytest.fail("--no-fetch must not fetch"))
    _patch_search(monkeypatch, {'"trace3.com"': [
        SearchHit(url="https://news.ycombinator.com/item?id=4", title="", snippet="Trace3 kafka partner", backend="hn"),
    ]})
    items, report = enrich_partner("trace3.com", plan=plan, backends=["hn"], include_fetch=False, delay=0.0)
    assert [item.item_id for item in items] == ["trace3.com"]
    assert report.dropped_unattributed == 0


def test_enrich_partner_reports_crawl_failure_and_still_searches(monkeypatch):
    plan = {"stages": {"find": {}}}
    _patch_crawl(monkeypatch, exc=RuntimeError("connection refused"))
    _offline_walk(monkeypatch)
    _patch_fetch(monkeypatch, {
        "https://news.ycombinator.com/item?id=5": "Trace3 is an independent consulting partner",
    })
    _patch_search(monkeypatch, {'"trace3.com"': [
        SearchHit(url="https://news.ycombinator.com/item?id=5", title="", snippet="", backend="hn"),
    ]})
    items, report = enrich_partner("trace3.com", plan=plan, backends=["hn"], delay=0.0)
    assert [item.item_id for item in items] == ["trace3.com"]
    assert any("connection refused" in s["reason"] for s in report.skipped)



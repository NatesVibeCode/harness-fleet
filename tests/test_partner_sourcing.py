"""Offline tests for the partner sourcing runner.

Every network primitive is replaced, so these run in milliseconds and prove the
two things that matter: the plan expands into the right calls, and only hits
that genuinely attribute the work to a named firm become evidence.
"""
from __future__ import annotations

import json

import pytest

from harness_fleet import partner_sourcing
from harness_fleet.discover import RawRecord, SearchHit
from harness_fleet.partner_sourcing import (
    SourcingError,
    attribution_terms,
    candidate_entities,
    enrich_partner,
    enrich_urls,
    filter_attributed,
    find_partners,
    find_queries,
    is_attributed,
    is_source_host,
    load_plan,
)

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def test_packaged_plan_holds_the_product_and_the_surfaces_are_shared():
    """What is specific to this product is its queries; where evidence lives is not.

    The enrich and vendor-story blocks moved to the shared surface plan the day
    every lane started walking the same surfaces — a second copy here is a
    second thing to keep true.
    """
    plan = load_plan()
    assert plan["schema"] == "partner_sources_v1"
    assert set(plan["stages"]) >= {"find"}
    assert plan["evidence_rules"]["attribution"]

    from harness_fleet.enrich import load_surfaces

    surfaces = load_surfaces()
    assert surfaces["schema"] == "source_surfaces_v1"
    assert set(surfaces) >= {
        "discovery", "first_party_paths", "match", "templates", "channels", "ats", "vendor_stories",
    }
    assert "/sitemap.xml" in surfaces["discovery"]["paths"], "the surface that answers everywhere"
    assert surfaces["ats"]["greenhouse"].endswith("jobs?content=true"), "the API, not the HTML board"
    # A surface that answered 403 on every domain probed is not fetchable; the
    # data file records why it went rather than leaving a silent hole.
    fetchable = str(surfaces.get("templates")) + str(surfaces.get("first_party_paths"))
    assert "clutch.co" not in fetchable and "g2.com" not in fetchable
    assert "clutch.co" in str(surfaces.get("removed")), "recorded, not forgotten"
    assert surfaces["vendor_stories"]["vendors"], "the vendor hubs every lane can read"
    assert "/case-studies" in surfaces["first_party_paths"]["case_studies"]


def test_load_plan_rejects_a_plan_without_stages(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "partner_sources_v1"}), encoding="utf-8")
    with pytest.raises(SourcingError, match="no stages"):
        load_plan(bad)


def test_load_plan_reports_missing_and_malformed_files(tmp_path):
    with pytest.raises(SourcingError, match="could not read"):
        load_plan(tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SourcingError, match="not valid JSON"):
        load_plan(broken)


def test_find_queries_resolves_placeholders_and_drops_unfillable_ones():
    plan = {
        "stages": {
            "find": {
                "comment": "not a backend",
                "ddgs": {"queries": ['"{tech}" "{vertical}" case study', "generic query"]},
                "hn": ["who is hiring {tech}"],
                "fetch": {"urls": ["https://example.com"]},
            }
        }
    }
    resolved = find_queries(plan, tech="Kafka", vertical="fintech")
    assert resolved["ddgs"] == ['"Kafka" "fintech" case study', "generic query"]
    assert resolved["hn"] == ["who is hiring Kafka"]
    # A backend whose only query needs a missing placeholder disappears entirely.
    assert "fetch" not in resolved
    assert "comment" not in resolved


def test_find_queries_skips_templates_that_need_an_absent_value():
    plan = {"stages": {"find": {"ddgs": ['"{tech}" partner', "no placeholders"]}}}
    assert find_queries(plan, tech="", vertical="") == {"ddgs": ["no placeholders"]}


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


def test_practice_signal_gate_keeps_practice_pages_and_drops_commentary():
    from harness_fleet.partner_sourcing import has_practice_signal, practice_signals

    signals = practice_signals(load_plan())
    assert "case study" in signals and "hiring" in signals
    assert has_practice_signal("We are hiring a Snowflake solutions architect", signals)
    assert has_practice_signal("Their client migrated to Kafka last year", signals)
    # Commentary about the technology is not a partner page.
    assert not has_practice_signal("the importance of distributed tracing for kafka", signals)
    assert not has_practice_signal("", signals)


def test_find_partners_drops_pages_with_no_practice_language(monkeypatch):
    plan = {"stages": {"find": {"ddgs": {"queries": ['"{tech}"']}}}}
    _patch_search(monkeypatch, {find_queries(plan, tech="Kafka")["ddgs"][0]: [
        SearchHit(url="https://blog.example.com/kafka-tuning", title="", snippet="", backend="ddgs"),
        SearchHit(url="https://trace3.com/services", title="", snippet="", backend="ddgs"),
    ]})
    _patch_fetch(monkeypatch, {
        "https://blog.example.com/kafka-tuning": "tuning kafka consumer groups for throughput at scale",
        "https://trace3.com/services": "Trace3 provides implementation services for Kafka clients",
    })
    items, report = find_partners(plan=plan, tech="Kafka", backends=["ddgs"], delay=0.0)
    assert [item.item_id for item in items] == ["trace3.com"]
    assert report.dropped_no_practice_signal == 1


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


def test_find_partners_keeps_attributed_hits_and_drops_the_rest(monkeypatch):
    plan = {"stages": {"find": {"ddgs": {"queries": ['"{tech}" case study']}, "hn": ["who is hiring {tech}"]}}}
    queries = find_queries(plan, tech="Kafka")
    _patch_search(monkeypatch, {
        queries["ddgs"][0]: [
            SearchHit(url="https://trace3.com/kafka", title="Kafka", snippet="", backend="ddgs"),
            SearchHit(url="https://conduktor.io/blog/tracing", title="Tracing", snippet="", backend="ddgs"),
        ],
        queries["hn"][0]: [
            SearchHit(url="https://news.ycombinator.com/item?id=9", title="HN", snippet="", backend="hn"),
            SearchHit(url="https://news.ycombinator.com/item?id=10", title="HN", snippet="", backend="hn"),
        ],
    })
    _patch_fetch(monkeypatch, {
        "https://trace3.com/kafka": "Trace3 delivers Kafka migration work for named clients.",
        "https://conduktor.io/blog/tracing": "the importance of distributed tracing for kafka pipelines",
        "https://news.ycombinator.com/item?id=9": (
            "we are hiring Kafka engineers at Trace3 (see https://trace3.com/careers)"
        ),
        # Practice signal, no attributable firm: a lead, not a candidate.
        "https://news.ycombinator.com/item?id=10": "we hired a consulting firm for our kafka migration",
    })

    items, report = find_partners(plan=plan, tech="Kafka", backends=["ddgs", "hn"], delay=0.0)

    # The HN thread is about Trace3, so it files under Trace3; nothing files
    # under news.ycombinator.com, and the tracing page is unattributed noise.
    assert [item.item_id for item in items] == ["trace3.com"]
    assert report.stage == "find"
    assert report.searched == 2
    assert report.fetched == 4
    assert report.kept == 1
    # The tracing blog never mentions doing the work; the HN thread mentions
    # doing the work but names nobody.
    assert report.dropped_no_practice_signal == 1
    # The second HN thread links nobody; it is a lead, not a dossier.
    assert report.dropped_unattributed == 1
    assert report.candidates == ["trace3.com"]
    assert "news.ycombinator.com" not in report.candidates
    assert "Trace3 delivers Kafka migration work" in items[0].text
    assert "hiring Kafka engineers" in items[0].text


def test_find_partners_records_fetch_failures_instead_of_raising(monkeypatch):
    plan = {"stages": {"find": {"ddgs": {"queries": ['"{tech}"']}}}}
    _patch_search(monkeypatch, {find_queries(plan, tech="Kafka")["ddgs"][0]: [
        SearchHit(url="https://trace3.com/dead", title="", snippet="", backend="ddgs"),
    ]})
    _patch_fetch(monkeypatch, {})

    items, report = find_partners(plan=plan, tech="Kafka", backends=["ddgs"], delay=0.0)
    assert items == []
    assert report.fetched == 0
    assert report.skipped and "404" in report.skipped[0]["reason"]


def test_find_partners_snippets_only_never_fetches(monkeypatch):
    plan = {"stages": {"find": {"ddgs": {"queries": ['"{tech}"']}}}}
    _patch_search(monkeypatch, {find_queries(plan, tech="Kafka")["ddgs"][0]: [
        SearchHit(url="https://trace3.com/kafka", title="Trace3 Kafka", snippet="Trace3 delivers Kafka migrations for banks", backend="ddgs"),
    ]})

    def explode(*args, **kwargs):
        raise AssertionError("snippets-only must not fetch")

    monkeypatch.setattr(partner_sourcing, "fetch_text", explode)
    items, report = find_partners(plan=plan, tech="Kafka", backends=["ddgs"], snippets_only=True, delay=0.0)
    assert [item.item_id for item in items] == ["trace3.com"]
    assert report.fetched == 1


def test_find_partners_refuses_a_plan_with_nothing_to_search():
    with pytest.raises(SourcingError, match="no searchable queries"):
        find_partners(plan={"stages": {"find": {"ddgs": {"queries": ["{tech} only"]}}}}, tech="")


def test_find_partners_survives_a_backend_that_raises(monkeypatch):
    plan = {"stages": {"find": {"ddgs": {"queries": ['"{tech}"']}, "hn": ["who is hiring {tech}"]}}}
    queries = find_queries(plan, tech="Kafka")

    def flaky(query, backends=None, **kwargs):
        if backends == ["hn"]:
            raise RuntimeError("rate limited")
        return [SearchHit(url="https://trace3.com/x", title="", snippet="Trace3 delivers Kafka consulting", backend="ddgs")]

    monkeypatch.setattr(partner_sourcing, "web_search", flaky)
    monkeypatch.setattr(partner_sourcing, "fetch_text",
                        lambda url, **kw: RawRecord(text="Trace3 delivers Kafka consulting", source_uri=url))
    items, report = find_partners(plan=plan, tech="Kafka", backends=["ddgs", "hn"], delay=0.0)
    assert [item.item_id for item in items] == ["trace3.com"]
    assert any(s.get("backend") == "hn" and "rate limited" in s["reason"] for s in report.skipped)
    assert queries  # the plan really did expand


# ---------------------------------------------------------------------------
# enrich — offline
# ---------------------------------------------------------------------------

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


def test_report_serializes_for_the_cli(monkeypatch):
    plan = {"stages": {"find": {"ddgs": {"queries": ['"{tech}"']}}}}
    _patch_search(monkeypatch, {})
    items, report = find_partners(plan=plan, tech="Kafka", backends=["ddgs"], delay=0.0)
    payload = report.as_dict()
    assert payload["stage"] == "find"
    assert set(payload) == {
        "stage", "searched", "fetched", "kept", "dropped_unattributed",
        "dropped_no_practice_signal", "vendor_stories", "rejected", "candidates", "skipped",
    }
    assert json.loads(json.dumps(payload))["kept"] == 0
    assert items == []

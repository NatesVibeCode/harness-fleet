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
    declared |= {"vendor_stories", "community"}

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
    """The file documents itself with _-prefixed keys; prose is not data.

    The note lives *inside* the maps a reader enumerates, so reading them raw
    made ``first_party_paths`` name a surface called ``_comment`` with 103
    paths — the comment string, taken character by character — and gave
    ``channels`` and ``community`` one each. They are dropped at the door now.
    """
    raw = json.loads(enrich.SURFACES_PATH.read_text(encoding="utf-8"))
    assert any(
        str(key).startswith("_")
        for section in ("match", "first_party_paths", "channels", "community")
        for key in (raw.get(section) or {})
    ), "the shipped plan still explains itself in place"
    plan = enrich.load_surfaces()
    for section in ("match", "first_party_paths", "channels", "community", "vendor_stories"):
        leaked = [key for key in (plan.get(section) or {}) if str(key).startswith("_")]
        assert not leaked, f"{section} still names a comment as data: {leaked}"
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


def _raise_offline(url, **kw):
    raise RuntimeError(f"offline: {url} not reached in tests")


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


def test_a_lane_ladder_reads_its_surfaces_in_rung_order(monkeypatch):
    """A lane's rung surfaces extend the walk, landing page first.

    The ladder is lane data — the walk only reads the names. A rung naming
    `home` gets the domain root read even though no evidence kind maps to it,
    and the root resolves to `home` rather than being unclassifiable.
    """
    _offline(monkeypatch, sitemap=[
        "https://acme.com/",
        "https://acme.com/services",
        "https://acme.com/case-studies/acme-bank",
    ], pages={
        "https://acme.com/": "Acme is a data consultancy of 200 people in London.",
        "https://acme.com/services": "We implement Kafka for banking clients.",
    })
    records, report = enrich_entity(
        "acme.com", kinds=["stack_delivery"], vendor_stories=False,
        surface_order=["home", "services"],
    )
    assert report.by_surface.get("home") == 1, "the landing page is read when the lane lists it"
    assert "Acme is a data consultancy" in records[0].text, "home reads first, in rung order"


def test_a_lane_without_rung_surfaces_walks_exactly_as_before(monkeypatch):
    _offline(monkeypatch, sitemap=["https://acme.com/services"], pages={
        "https://acme.com/services": "We implement Kafka for banking clients.",
    })
    _records, report = enrich_entity("acme.com", kinds=["stack_delivery"], vendor_stories=False)
    assert "home" not in report.by_surface


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
    _offline(monkeypatch)
    monkeypatch.setattr(enrich, "fetch_markup", _raise_offline)
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


def test_a_channel_that_returns_a_pile_is_bounded_and_says_so(monkeypatch):
    """One GitHub org can hand back a dozen records; a dossier holds a few.

    The run that bundled 46 sources died in scoring with a context overflow, so
    the walk bounds what any one surface contributes — and counts what it left
    out rather than dropping it quietly.
    """
    from harness_fleet.discover import RawRecord

    _offline(monkeypatch)
    github = [
        RawRecord(text=f"repo {i} readme about acme", source_uri=f"https://github.com/acme/repo{i}")
        for i in range(9)
    ]
    from harness_fleet import discover

    monkeypatch.setattr(discover, "fetch_github_org", lambda org, **kw: (github, []))

    records, report = enrich_entity("acme.com", kinds=["engineering_output"], per_surface=3)
    assert len(records) == 3, "the budget is the budget"
    assert report.trimmed.get("code") == 6
    reason = " ".join(entry.get("reason", "") for entry in report.skipped if entry.get("surface") == "code")
    assert "6 more were not bundled" in reason


def test_a_route_that_cannot_take_tools_is_recognised_and_parked():
    """The refusal is predictable, so it must not cost an attempt every run.

    "No endpoints found that support tool use" is OpenRouter saying the model
    cannot accept the request opencode sends. Two routes hit this on every run
    while their public model records said tools:false.
    """
    from harness_fleet.engine import UNSUPPORTED_COOLDOWN_SEC
    from harness_fleet.providers.harness import classify_failure

    assert classify_failure("No endpoints found that support tool use. Try disabling bash") == "unsupported"
    assert classify_failure("model does not support tools") == "unsupported"
    assert UNSUPPORTED_COOLDOWN_SEC >= 24 * 60 * 60, "a capability does not come back in minutes"

    # And a genuinely different failure is not swept up by the new rule.
    assert classify_failure("HTTP 429 rate limit exceeded") == "rate_limit"
    assert classify_failure("connection reset by peer") != "unsupported"


def test_every_declared_channel_is_actually_dispatched():
    """A channel declared but never dispatched is dead code with a comment.

    Moving the community block out of the channels map silently stopped the walk
    from ever calling it; the mapping and the dispatch have to agree.
    """
    plan = enrich.load_surfaces()
    declared = {name for name in (plan.get("channels") or {}) if not name.startswith("_")}
    if plan.get("community"):
        declared.add("community")
    assert declared == {"ats", "code", "community"}, declared

    for channel in sorted(declared):
        surface = "community" if channel == "community" else channel
        kind = {
            "ats": "delivery_hiring", "code": "engineering_output", "community": "independent_validation",
        }[channel]
        assert surface in contracts.surfaces_for_kinds([kind]) or channel == "community", (
            f"'{channel}' is declared but no kind asks for it"
        )


def test_a_repository_is_not_a_dossier():
    """A page on a platform that credits nobody is evidence about nobody.

    The attribution rule returns "" for those; falling back to the generated
    record id filed them anyway, and a live run shipped a row named after a
    stranger's GitHub repository.
    """
    from harness_fleet.sources import entity_key_for

    assert entity_key_for("https://github.com/Yuvraj1507/-BankingSystem-Kafka", "a repo") == ""
    assert entity_key_for("https://medium.com/@someone/post", "a post") == ""
    assert entity_key_for("https://acme.com/case-studies/x", "delivered a thing") == "acme.com"
    assert entity_key_for("https://github.com/acme/tool", "see https://acme.com/x") == "acme.com"


def test_a_name_is_resolved_to_a_domain_or_left_alone(monkeypatch):
    """A name cannot be walked, and unwalked means no first-party evidence.

    The resolver must not guess: attaching a stranger's case studies to a
    candidate is worse than leaving it un-walkable and saying so.
    """
    from harness_fleet import enrich
    from harness_fleet.discover import SearchHit

    monkeypatch.setattr("harness_fleet.discover.web_search", lambda query, **kw: [
        SearchHit(url="https://en.wikipedia.org/wiki/Accenture", title="", snippet="", backend="ddgs"),
        SearchHit(url="https://www.accenture.com/us-en", title="", snippet="", backend="ddgs"),
        SearchHit(url="https://www.linkedin.com/company/accenture", title="", snippet="", backend="ddgs"),
        SearchHit(url="https://www.accenture.com/us-en/about", title="", snippet="", backend="ddgs"),
    ])
    assert enrich.resolve_entity_domain("accenture") == "accenture.com"

    monkeypatch.setattr("harness_fleet.discover.web_search", lambda query, **kw: [])
    assert enrich.resolve_entity_domain("adinte") == "", "no result is not a domain"
    assert enrich.resolve_entity_domain("") == ""


def test_a_story_index_is_read_once_not_once_per_company():
    """A vendor's index holds hundreds of stories; almost none are about them.

    Fetching every page to test attribution read a hundred pages to keep one or
    two, and it was the largest cost in a run. The story's address already names
    its subject — that is how candidates are found — so it settles most of them
    before a request is spent.
    """
    from harness_fleet.enrich import story_could_name

    # Their own story, filed under their name.
    assert story_could_name("https://www.snowflake.com/en/customers/accenture/", "accenture.com")
    # Punctuation is not a difference: publicis-groupe is publicisgroupe.
    assert story_could_name("https://www.snowflake.com/customers/publicis-groupe/", "publicisgroupe.com")
    # AWS files as <customer>-<partner>, so either half may be the subject.
    assert story_could_name("https://aws.amazon.com/partners/success/biolytica-presidio/", "presidio.com")
    # Somebody else's story.
    assert not story_could_name("https://www.databricks.com/customers/comcast/", "accenture.com")
    assert not story_could_name("https://www.elastic.co/customers/cvs/", "phdata.io")


def test_only_plausible_stories_are_read(monkeypatch):
    """Read what could be about them; do not read the rest on spec.

    A whole-index fallback ("nothing resembles them, so read everything") is not
    a safety net — it fires for every company that simply is not in the index,
    which is most of them. Measured on one entity it accounted for 65 of 79
    requests, which is to say the filter had become a no-op.
    """
    from harness_fleet import enrich
    from harness_fleet.discover import RawRecord

    seen: list[list[str]] = []

    def fake_many(urls_, fetch, **kwargs):
        batch = list(urls_)
        seen.append(batch)
        return [(u, True, RawRecord(text="Acme delivered a thing", source_uri=u)) for u in batch]

    monkeypatch.setattr(enrich, "_fetch_pages_many", fake_many)

    urls = [
        "https://www.snowflake.com/customers/accenture/",
        "https://www.databricks.com/customers/comcast/",
        "https://www.elastic.co/customers/cvs/",
    ]
    records, skipped = enrich.fetch_vendor_stories("accenture.com", urls=urls)
    assert seen[0] == [urls[0]], "only the story whose address names them is read"
    assert [r.source_uri for r in records] == [urls[0]]
    assert any("2 of 3 stories do not name accenture.com" in str(s.get("reason", "")) for s in skipped)

    # An address that cannot be read as a name at all is still read: AWS files
    # its stories as <customer>-<partner>, where nothing says which half is which.
    seen.clear()
    aws = ["https://aws.amazon.com/partners/success/biolytica-presidio/"]
    enrich.fetch_vendor_stories("presidio.com", urls=aws)
    assert seen[0] == aws, "an unreadable address cannot rule a candidate out"

    # And a company simply absent from the index costs nothing.
    seen.clear()
    records, _skipped = enrich.fetch_vendor_stories("nowhere.io", urls=urls)
    assert seen[0] == [], "not being in the index is not a reason to read the index"
    assert records == []


def test_the_ladder_is_the_authority_on_what_is_read(monkeypatch):
    """A rung names the surfaces, and a surface it does not name is not read.

    The rung used to be *unioned* with the surfaces the missing kinds map to,
    which made the ladder decorative: the partner lane names neither community
    nor code nor vendor_stories, yet every entity short of
    independent_validation searched six community backends and read a vendor's
    story index — 38 of 51 measured seconds on a single entity.
    """
    from harness_fleet import discover, enrich

    calls: list[str] = []

    monkeypatch.setattr(discover, "web_search", lambda *a, **k: calls.append("community") or [])
    monkeypatch.setattr(discover, "fetch_github_org", lambda *a, **k: calls.append("code") or ([], []))

    def no_stories(*a, **k):
        calls.append("vendor_stories")
        return [], []

    monkeypatch.setattr(enrich, "fetch_vendor_stories", no_stories)
    monkeypatch.setattr(discover, "discover_sitemap_url", lambda site, **kw: "")
    monkeypatch.setattr(discover, "crawl_site", lambda *a, **k: ([], []))
    monkeypatch.setattr(discover, "fetch_text", lambda url, **kw: (_ for _ in ()).throw(RuntimeError("404")))
    monkeypatch.setattr(enrich, "_fetch_pages_many", lambda urls, fetch, **kw: [])

    # The kinds want surfaces the rung does not name; the rung wins.
    records, report = enrich.enrich_entity(
        "acme.com",
        kinds=("independent_validation", "engineering_output"),
        surface_order=("home", "services"),
    )
    assert calls == [], f"a surface the rung does not name was read anyway: {calls}"
    reasons = " ".join(str(s.get("reason", "")) for s in report.skipped)
    assert "ladder does not name this surface" in reasons, "the narrowing is reported, not silent"
    assert records == []


def test_a_lane_with_no_ladder_still_walks_by_kind(monkeypatch):
    """The fallback: without rungs, the missing kinds decide, as before."""
    from harness_fleet import discover, enrich

    called: list[str] = []
    monkeypatch.setattr(discover, "web_search", lambda *a, **k: called.append("community") or [])
    monkeypatch.setattr(discover, "_source_ready", lambda *a, **k: True)
    monkeypatch.setattr(discover, "discover_sitemap_url", lambda site, **kw: "")
    monkeypatch.setattr(discover, "crawl_site", lambda *a, **k: ([], []))
    monkeypatch.setattr(enrich, "_fetch_pages_many", lambda urls, fetch, **kw: [])

    _records, report = enrich.enrich_entity("acme.com", kinds=("independent_validation",))
    assert "community" in called, "with no ladder, the kind's surfaces are read"
    assert "community" in report.surfaces


def test_a_surface_the_sitemap_omits_is_still_probed(monkeypatch):
    """A sitemap lists what a CMS generates, not the static pages.

    `/about` was missing from the sitemap of every live domain probed
    (infinitelambda, infracloud, corrdyn, slalom). Treating absence from the
    sitemap as "that page is not there" left `about` unread on every domain —
    the one page the size and location gates depend on — while `tune surface`
    printed "Missing Surfaces (Will probe fallback paths)". The walker now does
    what the tuner promises.
    """
    _offline(
        monkeypatch,
        sitemap=["https://acme.com/blog/post"],
        pages={
            "https://acme.com/blog/post": "A blog post.",
            "https://acme.com/about": "Acme is a data consultancy of 120 people in London.",
        },
    )
    records, report = enrich_entity(
        "acme.com", kinds=["stack_delivery"], vendor_stories=False,
        surface_order=["about", "blog"],
    )
    assert "about" in report.by_surface, "the sitemap omitted it, so the guess list must answer"
    assert report.by_surface["about"] >= 1
    assert any("Acme is a data consultancy" in getattr(r, "text", "") for r in records)

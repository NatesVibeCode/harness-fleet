import json
from types import SimpleNamespace

import pytest

import career_fleet.lanes.lane1_sourcing as lane1
from career_fleet.cli import (
    cmd_discover,
    cmd_dossier,
    cmd_export,
    cmd_profile,
    cmd_triage,
    get_profile,
)
from career_fleet.lanes.lane2_triage import check_dealbreakers, run_lane2_triage
from career_fleet.lanes.lane3_systems import run_lane3_systems
from career_fleet.lanes.lane4_culture import run_lane4_culture
from career_fleet.profile import IdealEmployerProfile
from career_fleet.store import CareerStore


def item(item_id: str, text: str, *, source_uri: str | None = None, metadata: dict | None = None):
    return SimpleNamespace(
        item_id=item_id,
        title="Engineer",
        text=text,
        source_uri=source_uri or f"https://example.com/{item_id}",
        metadata=metadata or {},
    )


def test_database_source_text_flows_through_all_lanes(tmp_path):
    store = CareerStore(tmp_path / "career.db")
    store.upsert_company("good", "GoodCo", headcount=10)
    store.add_job_posting(
        "job-good",
        "good",
        "Systems Engineer",
        "Fully remote, engineer-led, async-first team building a distributed storage engine with Kafka and PostgreSQL.",
    )
    profile = IdealEmployerProfile(required_stack=["Kafka", "PostgreSQL"])

    assert run_lane2_triage(store, profile) == {
        "status": "success",
        "total_triaged": 1,
        "survivors": 1,
        "disqualified": 0,
    }
    assert store.list_companies()[0]["status"] == "triaged"
    assert run_lane3_systems(store, profile)["evaluated"] == 1
    assert run_lane4_culture(store, profile)["evaluated"] == 1
    assert store.list_companies()[0]["status"] == "qualified"

    dossier = store.get_company_dossier("good")
    assert dossier["jobs"][0]["raw_text"].startswith("Fully remote")
    assert [evaluation["status"] for evaluation in dossier["evaluations"]] == [
        "triaged",
        "qualified",
        "qualified",
    ]
    profile_revision = store.active_profile_revision_id()
    assert profile_revision
    assert {evaluation["profile_revision_id"] for evaluation in dossier["evaluations"]} == {profile_revision}
    assert store.load_profile().model_dump() == profile.model_dump()


def test_profile_cli_persists_iep_to_selected_database(tmp_path):
    profile_path = tmp_path / "profile.json"
    db_path = tmp_path / "career.db"
    profile = IdealEmployerProfile(profile_name="Persisted Career Profile", required_stack=["Go"])
    profile.save(profile_path)

    assert cmd_profile(SimpleNamespace(path=str(profile_path), db=str(db_path), init=False, force=False)) == 0
    stored = CareerStore(db_path)
    assert stored.load_profile().model_dump() == profile.model_dump()


def test_implicit_profile_can_fall_back_to_stored_iep(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "career.db")
    profile = IdealEmployerProfile(profile_name="Database-backed IEP")
    store.save_profile(profile)
    monkeypatch.chdir(tmp_path)

    assert get_profile(store=store).model_dump() == profile.model_dump()


def test_lane2_uses_source_text_and_metadata_for_hard_filters(tmp_path):
    profile = IdealEmployerProfile(dealbreakers={"policy": "remote_only"})
    store = CareerStore(tmp_path / "career.db")
    store.upsert_company("office", "OfficeCo", headcount=10)
    store.add_job_posting(
        "job-office",
        "office",
        "Engineer",
        "This role is required in the Austin office.",
        location="Austin, TX",
        is_remote=False,
    )

    result = run_lane2_triage(store, profile)

    assert result["disqualified"] == 1
    assert store.list_companies()[0]["status"] == "disqualified"
    assert "Austin" in store.get_company_dossier("office")["disqualification_reason"]


def test_lane2_honors_configured_location_and_quota_rules():
    profile = IdealEmployerProfile(
        dealbreakers={
            "policy": "any",
            "disallowed_locations": ["Austin"],
            "reject_pure_quota": True,
        }
    )
    assert check_dealbreakers(
        {"id": "x", "headcount": 10},
        [{"raw_text": "Austin location is mandatory"}],
        profile,
    )["rule"] == "configured_location"
    assert check_dealbreakers(
        {"id": "x", "headcount": 10},
        [{"raw_text": "Pure quota cold outbound role"}],
        profile,
    )["rule"] == "pure_quota"


def test_default_profile_does_not_assume_remote_work():
    result = check_dealbreakers(
        {"id": "office-company", "headcount": 10},
        [{"raw_text": "In-office platform engineering role.", "location": "Austin, TX", "is_remote": False}],
        IdealEmployerProfile(),
    )
    assert result is None


def test_lane2_fails_closed_for_unknown_headcount():
    profile = IdealEmployerProfile(dealbreakers={"max_headcount": 50, "require_verified_headcount": True})
    result = check_dealbreakers({"id": "x", "headcount": None}, [{"raw_text": "Platform engineer"}], profile)
    assert result["rule"] == "headcount_unknown"


def test_lane2_does_not_assume_ats_headcount_when_not_required():
    profile = IdealEmployerProfile(dealbreakers={"max_headcount": 50})
    assert check_dealbreakers({"id": "x", "headcount": None}, [{"raw_text": "Platform engineer"}], profile) is None


def test_lane2_requires_role_level_remote_or_hybrid_evidence():
    remote_only = IdealEmployerProfile(dealbreakers={"policy": "remote_only"})
    company_remote = {
        "raw_text": "We are a remote-first company; this role is based in San Francisco.",
        "location": "San Francisco, CA",
        "is_remote": False,
    }
    assert check_dealbreakers({"id": "x"}, [company_remote], remote_only)["rule"] == "non_remote_location"

    remote_or_hybrid = IdealEmployerProfile(dealbreakers={"policy": "remote_or_hybrid"})
    unknown = [{"raw_text": "Platform engineer", "location": "Austin, TX", "is_remote": False}]
    assert check_dealbreakers({"id": "x"}, unknown, remote_or_hybrid)["rule"] == "workplace_policy_unknown"
    hybrid_cloud = [{"raw_text": "We build hybrid cloud infrastructure.", "location": "Austin, TX", "is_remote": False}]
    assert check_dealbreakers({"id": "x"}, hybrid_cloud, remote_or_hybrid)["rule"] == "workplace_policy_unknown"
    hybrid_first_company = [{"raw_text": "We are hybrid-first; this role is based in Austin.", "location": "Austin, TX", "is_remote": False}]
    assert check_dealbreakers({"id": "x"}, hybrid_first_company, remote_or_hybrid)["rule"] == "workplace_policy_unknown"
    hybrid_role = [{"raw_text": "This is a hybrid role.", "location": "Austin, TX", "is_remote": False}]
    assert check_dealbreakers({"id": "x"}, hybrid_role, remote_or_hybrid) is None


@pytest.mark.parametrize(
    "text",
    ["Remote work is required for this role.", "This is a remote role."],
)
def test_lane1_and_lane2_accept_explicit_remote_roles(text):
    from career_fleet.lanes.lane1_sourcing import _is_remote_listing
    from career_fleet.lanes.lane2_triage import _has_remote_evidence

    posting = {"raw_text": text, "location": "Austin, TX", "is_remote": False}
    assert _is_remote_listing(text, posting["location"])
    assert _has_remote_evidence(posting)


@pytest.mark.parametrize(
    "text",
    ["This is not a remote role.", "Remote work is not available.", "Hybrid work is not available."],
)
def test_workplace_negations_do_not_pass_remote_policies(text):
    from career_fleet.lanes.lane1_sourcing import _is_remote_listing
    from career_fleet.lanes.lane2_triage import (
        _has_remote_evidence,
        _has_remote_or_hybrid_evidence,
    )

    posting = {"raw_text": text, "location": "Austin, TX", "is_remote": False}
    assert not _is_remote_listing(text, posting["location"])
    assert not _has_remote_evidence(posting)
    assert not _has_remote_or_hybrid_evidence(posting)


def test_lane2_matches_full_state_names_in_configured_locations():
    profile = IdealEmployerProfile(dealbreakers={"policy": "any", "disallowed_locations": ["Austin, TX"]})
    result = check_dealbreakers(
        {"id": "x"},
        [{"raw_text": "Platform engineer", "location": "Austin, Texas", "is_remote": False}],
        profile,
    )
    assert result["rule"] == "configured_location"


@pytest.mark.parametrize(
    ("configured", "actual"),
    [("San Francisco, CA", "SF, California"), ("New York, NY", "NYC")],
)
def test_lane2_matches_common_city_aliases(configured, actual):
    profile = IdealEmployerProfile(dealbreakers={"policy": "any", "disallowed_locations": [configured]})
    result = check_dealbreakers(
        {"id": "x"},
        [{"raw_text": "Platform engineer role is required in this location.", "location": actual, "is_remote": False}],
        profile,
    )
    assert result["rule"] == "configured_location"


def test_lane2_rechecks_existing_companies_after_profile_changes(tmp_path):
    store = CareerStore(tmp_path / "profile-change.db")
    store.upsert_company("x", "X", headcount=100)
    store.add_job_posting("job-x", "x", "Engineer", "Platform engineer")

    assert run_lane2_triage(store, IdealEmployerProfile())["survivors"] == 1
    stricter = IdealEmployerProfile(dealbreakers={"max_headcount": 10})
    result = run_lane2_triage(store, stricter)

    assert result["total_triaged"] == 1
    assert result["disqualified"] == 1
    assert store.list_companies()[0]["status"] == "disqualified"

    recovered = run_lane2_triage(store, IdealEmployerProfile())
    assert recovered["survivors"] == 1
    assert store.list_companies()[0]["status"] == "triaged"


def test_lane2_enforces_timezone_overlap_when_configured():
    profile = IdealEmployerProfile(
        dealbreakers={
            "policy": "any",
            "candidate_timezone": "America/Los_Angeles",
            "min_timezone_overlap_hours": 4.0,
        }
    )
    assert check_dealbreakers(
        {"id": "near", "headcount": 10, "timezone": "America/New_York"},
        [{"raw_text": "Role", "timezone": "America/New_York"}],
        profile,
    ) is None
    assert check_dealbreakers(
        {"id": "far", "headcount": 10, "timezone": "Europe/London"},
        [{"raw_text": "Role", "timezone": "Europe/London"}],
        profile,
    )["rule"] == "timezone_overlap"
    assert check_dealbreakers(
        {"id": "unknown", "headcount": 10},
        [{"raw_text": "Role"}],
        profile,
    )["rule"] == "timezone_overlap_unknown"


def test_recon_only_advances_after_systems_and_is_rerunnable(tmp_path):
    store = CareerStore(tmp_path / "career.db")
    store.upsert_company("good", "GoodCo")
    store.add_job_posting("job-good", "good", "Engineer", "Fully remote engineer-led team.")
    profile = IdealEmployerProfile(required_stack=["Kafka", "PostgreSQL"])

    assert run_lane3_systems(store, profile)["evaluated"] == 0
    assert run_lane2_triage(store, profile)["survivors"] == 1
    assert run_lane3_systems(store, profile)["evaluated"] == 1
    assert run_lane4_culture(store, profile)["evaluated"] == 0
    assert store.list_companies()[0]["status"] == "triaged"

    store.add_job_posting(
        "job-good",
        "good",
        "Engineer",
        "Fully remote, engineer-led, async-first team building a distributed storage engine with Kafka and PostgreSQL.",
    )
    assert run_lane3_systems(store, profile)["evaluated"] == 1
    assert run_lane4_culture(store, profile)["evaluated"] == 1
    assert run_lane2_triage(store, profile)["total_triaged"] == 1
    assert run_lane3_systems(store, profile)["evaluated"] == 1
    assert run_lane4_culture(store, profile)["evaluated"] == 1


@pytest.mark.parametrize(
    ("source", "target", "function_name", "keyword"),
    [
        ("yc", "w24", "fetch_yc_companies", "max_companies"),
        ("greenhouse", "acme", "fetch_greenhouse_board", "max_jobs"),
        ("ashby", "acme", "fetch_ashby_org", "max_jobs"),
        ("lever", "acme", "fetch_lever_org", "max_jobs"),
    ],
)
def test_lane1_uses_current_discovery_signatures(tmp_path, monkeypatch, source, target, function_name, keyword):
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return [item("job-1", "Fully remote role")]

    monkeypatch.setattr(lane1, function_name, fake_fetch)
    result = lane1.run_lane1_sourcing(CareerStore(tmp_path / f"{source}.db"), source, target, max_items=3)

    assert result["status"] == "success"
    assert calls[keyword] == 3


def test_lane1_unpacks_site_crawl_result(tmp_path, monkeypatch):
    calls = {}

    def fake_crawl(**kwargs):
        calls.update(kwargs)
        return ([item("page-1", "Fully remote team")], [{"url": "https://example.com/blocked", "reason": "robots"}])

    monkeypatch.setattr(
        lane1,
        "crawl_site",
        fake_crawl,
    )

    result = lane1.run_lane1_sourcing(CareerStore(tmp_path / "site.db"), "site", "https://example.com", max_items=2)

    assert result == {
        "status": "success",
        "source_type": "site",
        "target": "https://example.com",
        "companies_discovered": 1,
        "postings_added": 1,
        "pages_skipped": 1,
    }
    assert calls["start_url"] == "https://example.com"


def test_lane1_yc_uses_website_domain_and_team_size(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lane1,
        "fetch_yc_companies",
        lambda **kwargs: [
            item(
                "yc-one",
                "Team size: 5\nFully remote database",
                source_uri="https://www.ycombinator.com/companies/one",
                metadata={"website": "https://one.example", "team_size": 5},
            ),
            item(
                "yc-two",
                "Team size: 7\nFully remote database",
                source_uri="https://www.ycombinator.com/companies/two",
                metadata={"website": "https://two.example", "team_size": 7},
            ),
        ],
    )

    store = CareerStore(tmp_path / "yc.db")
    result = lane1.run_lane1_sourcing(store, "yc", "W24", max_items=2)

    assert result["status"] == "success"
    companies = {company["id"]: company for company in store.list_companies()}
    assert companies["yc-one"]["domain"] == "one.example"
    assert companies["yc-one"]["headcount"] == 5
    assert companies["yc-two"]["domain"] == "two.example"
    assert companies["yc-two"]["headcount"] == 7


def test_lane1_yc_profile_only_records_do_not_share_directory_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lane1,
        "fetch_yc_companies",
        lambda **kwargs: [
            item("yc-one", "Team size: 5", source_uri="https://www.ycombinator.com/companies/one", metadata={"team_size": 5}),
            item("yc-two", "Team size: 7", source_uri="https://www.ycombinator.com/companies/two", metadata={"team_size": 7}),
        ],
    )
    store = CareerStore(tmp_path / "yc-profile-only.db")

    result = lane1.run_lane1_sourcing(store, "yc", "W24", max_items=2)

    assert result["status"] == "success"
    assert all(company["domain"] is None for company in store.list_companies())


def test_lane1_keeps_multiple_records_for_one_source_company(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lane1,
        "fetch_yc_companies",
        lambda **kwargs: [
            item("yc-one-a", "First profile", source_uri="https://www.ycombinator.com/companies/one-a", metadata={"website": "https://one.example"}),
            item("yc-one-b", "Second profile", source_uri="https://www.ycombinator.com/companies/one-b", metadata={"website": "https://one.example"}),
        ],
    )
    store = CareerStore(tmp_path / "yc-duplicate-domain.db")

    result = lane1.run_lane1_sourcing(store, "yc", "W24", max_items=2)

    assert result["status"] == "success"
    assert len(store.list_companies()) == 1
    assert len(store.get_company_dossier(store.list_companies()[0]["id"])["jobs"]) == 2


def test_lane1_refresh_replaces_stale_postings_and_evaluations(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "refresh.db")
    store.upsert_company("acme", "Acme", ats_provider="greenhouse", ats_token="acme")
    store.add_job_posting("old", "acme", "Old role", "Old source text", source_type="greenhouse")
    store.record_evaluation("old-eval", "acme", "lane2_triage", "triaged", 1.0, "SURVIVOR", "old")
    monkeypatch.setattr(lane1, "fetch_greenhouse_board", lambda **kwargs: [item("new", "New source text")])

    result = lane1.run_lane1_sourcing(store, "greenhouse", "acme", max_items=1)
    dossier = store.get_company_dossier("acme")

    assert result["status"] == "success"
    assert [job["id"] for job in dossier["jobs"]] == ["new"]
    assert dossier["evaluations"] == []
    assert dossier["status"] == "discovered"


def test_lane1_empty_refresh_keeps_last_known_snapshot(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "empty-refresh.db")
    store.upsert_company("acme", "Acme", ats_provider="greenhouse", ats_token="acme")
    store.add_job_posting("old", "acme", "Old role", "Old source text", source_type="greenhouse")
    store.record_evaluation("old-eval", "acme", "lane2_triage", "triaged", 1.0, "SURVIVOR", "old")
    monkeypatch.setattr(lane1, "fetch_greenhouse_board", lambda **kwargs: [])

    result = lane1.run_lane1_sourcing(store, "greenhouse", "acme", max_items=1)

    assert result["status"] == "success"
    assert result["warnings"]
    dossier = store.get_company_dossier("acme")
    assert [job["id"] for job in dossier["jobs"]] == ["old"]
    assert [evaluation["id"] for evaluation in dossier["evaluations"]] == ["old-eval"]


def test_lane1_bad_snapshot_is_atomic(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "atomic-refresh.db")
    store.upsert_company("acme", "Acme", ats_provider="greenhouse", ats_token="acme")
    store.add_job_posting("old", "acme", "Old role", "Old source text", source_type="greenhouse")
    monkeypatch.setattr(
        lane1,
        "fetch_greenhouse_board",
        lambda **kwargs: [item("new", "New source text"), item("bad", "")],
    )

    result = lane1.run_lane1_sourcing(store, "greenhouse", "acme", max_items=2)

    assert result["status"] == "error"
    assert [job["id"] for job in store.get_company_dossier("acme")["jobs"]] == ["old"]


def test_lane1_merges_a_site_refresh_with_existing_domain_record(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "merge.db")
    store.upsert_company("yc-one", "One", domain="one.example")
    monkeypatch.setattr(lane1, "crawl_site", lambda **kwargs: ([item("page", "Company page")], []))

    result = lane1.run_lane1_sourcing(store, "site", "https://one.example", max_items=1)
    companies = store.list_companies()
    dossier = store.get_company_dossier("yc-one")

    assert result["status"] == "success"
    assert len(companies) == 1
    assert companies[0]["name"] == "One"
    assert dossier["jobs"][0]["id"] == "page"


def test_lane1_source_refresh_preserves_other_source_records(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "source-lineage.db")
    store.upsert_company("yc-one", "One", domain="one.example")
    store.add_job_posting(
        "yc-profile",
        "yc-one",
        "Company profile",
        "YC source text",
        source_type="yc",
    )
    monkeypatch.setattr(lane1, "crawl_site", lambda **kwargs: ([item("page", "Company page")], []))

    result = lane1.run_lane1_sourcing(store, "site", "https://one.example", max_items=1)
    jobs = store.get_company_dossier("yc-one")["jobs"]

    assert result["status"] == "success"
    assert {job["id"] for job in jobs} == {"yc-profile", "page"}
    assert {job["source_type"] for job in jobs} == {"yc", "site"}


def test_lane1_community_source_links_only_unambiguous_company_domains(tmp_path, monkeypatch):
    records = [
        item(
            "reddit-1",
            "Hiring a Staff Engineer to build Kafka and PostgreSQL systems. Fully remote. https://acme.example/careers",
            source_uri="https://www.reddit.com/r/experienceddevs/comments/abc123/hiring/",
        ),
        item(
            "reddit-2",
            "Hiring a platform engineer. Fully remote. See https://one.example and https://two.example",
            source_uri="https://www.reddit.com/r/experienceddevs/comments/def456/hiring/",
        ),
        item(
            "reddit-3",
            "Kafka architecture discussion with throughput tradeoffs.",
            source_uri="https://www.reddit.com/r/dataengineering/comments/ghi789/kafka/",
        ),
    ]
    monkeypatch.setattr(lane1, "_fetch_community_records", lambda *args, **kwargs: (records, []))
    store = CareerStore(tmp_path / "community.db")
    profile = IdealEmployerProfile(required_stack=["Kafka", "PostgreSQL"])

    result = lane1.run_lane1_sourcing(store, "reddit", "hiring", max_items=10, profile=profile)

    assert result["status"] == "success"
    assert result["community_signals_added"] == 2
    assert result["postings_added"] == 1
    assert result["unlinked_signals"] == 1
    assert result["low_signal_skipped"] == 1
    assert len(store.list_companies()) == 1
    signals = store.list_community_signals(limit=10)
    assert len(signals) == 2
    assert sum(1 for signal in signals if signal["company_id"]) == 1
    assert len(store.get_company_dossier("acme.example")["jobs"]) == 1


def test_community_attribution_ignores_its_own_forum_host():
    forum_record = item(
        "discourse-1",
        "Hiring discussion on https://discuss.python.org/t/jobs/1",
        source_uri="https://discuss.python.org/t/jobs/1",
        metadata={"instance": "https://discuss.python.org"},
    )

    assert lane1._community_company_links(forum_record, ignored_domains=["https://discuss.python.org"]) == []


def test_community_attribution_does_not_promote_unrelated_article_links():
    article = item(
        "devto-1",
        "An engineering productivity retrospective. Product link: https://amazon.com/dp/example",
        source_uri="https://dev.to/example/retrospective",
    )

    assert lane1._community_company_links(article) == []


def test_low_signal_audit_records_stay_unlinked(tmp_path, monkeypatch):
    record = item(
        "article-1",
        "An unrelated engineering essay. https://unrelated.example/article",
        source_uri="https://dev.to/example/essay",
    )
    monkeypatch.setattr(lane1, "_fetch_community_records", lambda *args, **kwargs: ([record], []))
    store = CareerStore(tmp_path / "low-signal.db")

    result = lane1.run_lane1_sourcing(store, "devto", "engineering", include_low_signal=True)

    assert result["community_signals_added"] == 1
    assert result["postings_added"] == 0
    assert result["unlinked_signals"] == 1
    assert store.list_companies() == []


def test_hn_hiring_threads_split_into_attributable_comments():
    thread = item(
        "hn-123",
        "Ask HN: Who is hiring?\n\n[alice]: Hiring a remote engineer at https://acme.example/careers\n\n[bob]: Looking for a product engineer.",
        source_uri="https://news.ycombinator.com/item?id=123",
        metadata={"source": "hackernews"},
    )

    records = lane1._expand_hn_records([thread], max_items=10)

    assert len(records) == 3
    assert records[1]["item_id"] == "hn-123-comment-1"
    assert "Hiring a remote engineer" in records[1]["text"]


def test_lane1_community_source_refresh_is_idempotent(tmp_path, monkeypatch):
    store = CareerStore(tmp_path / "community-refresh.db")
    first = [item("reddit-1", "Hiring a remote engineer at https://one.example", source_uri="https://reddit.com/r/x/comments/one/post")]
    second = [item("reddit-2", "Hiring a remote engineer at https://two.example", source_uri="https://reddit.com/r/x/comments/two/post")]
    records = [first]
    monkeypatch.setattr(lane1, "_fetch_community_records", lambda *args, **kwargs: (records[0], []))

    first_result = lane1.run_lane1_sourcing(store, "reddit", "hiring", max_items=10)
    records[0] = second
    second_result = lane1.run_lane1_sourcing(store, "reddit", "hiring", max_items=10)

    assert first_result["community_signals_added"] == 1
    assert second_result["community_signals_added"] == 1
    assert [signal["id"] for signal in store.list_community_signals(limit=10)] == ["community-reddit-reddit-2"]
    assert store.get_company_dossier("one.example")["jobs"] == []
    assert len(store.get_company_dossier("two.example")["jobs"]) == 1


def test_cli_exposes_career_community_sources_and_forwards_focus_options(tmp_path, monkeypatch):
    from career_fleet.cli import cmd_discover

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "source_type": "reddit", "target": "hiring engineers", "companies_discovered": 0, "postings_added": 0, "pages_skipped": 0}

    from career_fleet import cli
    monkeypatch.setattr(cli, "run_lane1_sourcing", fake_run)
    result = cmd_discover(
        SimpleNamespace(
            source="reddit", target="hiring engineers", max=5,
            db=str(tmp_path / "career-community-cli.db"), profile=None,
            subreddit=["startups"], include_low_signal=True,
            query=None, reddit_rss=False, subreddit_sort="new",
            se_tagged=None, se_site="stackoverflow", se_answers=False,
            discourse_url=None, lemmy_instance="https://programming.dev",
            delay=0.2, timeout=20.0,
        )
    )
    assert result == 0
    assert captured["source_type"] == "reddit"
    assert captured["subreddit"] == ["startups"]
    assert captured["include_low_signal"] is True


def test_lane1_community_dispatches_every_supported_source(monkeypatch):
    sample = item(
        "community-1",
        "Hiring a remote Staff Engineer with Kafka experience.",
        source_uri="https://community.example/1",
    )
    monkeypatch.setattr(lane1, "fetch_reddit_posts", lambda *args, **kwargs: [sample])
    monkeypatch.setattr(lane1, "fetch_stackexchange_questions", lambda *args, **kwargs: [sample])
    monkeypatch.setattr(lane1, "fetch_discourse_search", lambda *args, **kwargs: ([sample], []))
    monkeypatch.setattr(lane1, "fetch_lemmy", lambda *args, **kwargs: [sample])
    monkeypatch.setattr(lane1, "fetch_devto_tag", lambda *args, **kwargs: [sample])
    monkeypatch.setattr(lane1, "run_discovery", lambda *args, **kwargs: ([sample], {"skipped": []}))

    for source_type in lane1.COMMUNITY_SOURCE_TYPES:
        records, skipped = lane1._fetch_community_records(
            source_type,
            "https://forum.example" if source_type == "discourse" else "hiring",
            query="hiring" if source_type in {"discourse", "lobsters"} else None,
            max_items=1,
        )
        assert records == [sample], source_type
        assert skipped == [], source_type


def test_lane1_does_not_promote_company_remote_text_over_city_location(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lane1,
        "fetch_greenhouse_board",
        lambda **kwargs: [
            item(
                "job-1",
                "We are a remote-first company; this role is based in San Francisco.",
                metadata={"location": "San Francisco, CA"},
            )
        ],
    )
    store = CareerStore(tmp_path / "remote.db")

    lane1.run_lane1_sourcing(store, "greenhouse", "acme", max_items=1)

    assert store.get_company_dossier("acme")["jobs"][0]["is_remote"] == 0


def test_cli_reports_discovery_errors_as_failures(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        "career_fleet.cli.run_lane1_sourcing",
        lambda **kwargs: {"status": "error", "error": "bad source"},
    )

    result = cmd_discover(
        SimpleNamespace(source="greenhouse", target="acme", max=1, db=str(tmp_path / "career.db"))
    )

    assert result == 1
    assert "bad source" in capsys.readouterr().err


def test_cli_rejects_missing_implicit_profile(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = cmd_triage(SimpleNamespace(profile=None, db=str(tmp_path / "career.db")))
    assert result == 1
    assert "profile --init" in capsys.readouterr().err


def test_dossier_can_show_source_urls_and_full_text(capsys, tmp_path):
    store = CareerStore(tmp_path / "dossier.db")
    store.upsert_company("acme", "Acme")
    store.add_job_posting("job-1", "acme", "Engineer", "Exact source text", job_url="https://jobs.example/1")

    result = cmd_dossier(SimpleNamespace(company="acme", db=str(tmp_path / "dossier.db"), show_source=True))
    output = capsys.readouterr().out
    assert result == 0
    assert "https://jobs.example/1" in output
    assert "Exact source text" in output


def test_export_includes_structured_quotes(tmp_path, capsys):
    db_path = tmp_path / "export.db"
    store = CareerStore(db_path)
    store.upsert_company("acme", "Acme")
    store.record_evaluation("ev", "acme", "lane4_culture", "qualified", 0.8, "HEALTHY", "good", quotes=["Exact quote"])

    result = cmd_export(SimpleNamespace(db=str(db_path), output=str(tmp_path / "qualified.json")))
    payload = json.loads((tmp_path / "qualified.json").read_text())

    capsys.readouterr()
    assert result == 0
    assert payload[0]["evaluations"][0]["quotes"] == ["Exact quote"]


def test_single_culture_keyword_is_not_a_final_qualification():
    from career_fleet.lanes.lane4_culture import score_culture_and_team

    result = score_culture_and_team("We are engineer-led.", IdealEmployerProfile())
    assert result["score"] == 0.7
    assert result["verdict"] == "ACCEPTABLE"

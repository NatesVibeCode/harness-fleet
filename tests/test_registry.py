"""The taxonomy grows from what it sees, without guessing silently."""
import pytest

from harness_fleet import registry


def test_an_unlisted_source_earns_its_category_from_what_is_seen(tmp_path):
    """No queue, no review: the record decides, and the record is kept."""
    store = tmp_path / "reg.json"
    assert registry.lookup("jobs.newats.io", store) is None

    registry.observe("https://jobs.newats.io/acme/positions", text="Apply now", path=store)
    # A hiring page is a hiring board; it promotes itself on the first sighting.
    assert registry.lookup("jobs.newats.io", store) == "ats_requisitions"
    entry = registry.load(store)["domains"]["jobs.newats.io"]
    assert entry["promoted_by"] == "auto"
    assert entry["signals"]["ats_requisitions"] > 0
    assert entry["sample_urls"] == ["https://jobs.newats.io/acme/positions"]


def test_a_domain_promotes_itself_once_the_signals_agree(tmp_path):
    """Nobody reviews a queue: consistent sightings promote on their own."""
    store = tmp_path / "reg.json"
    registry.observe("https://jobs.newats.io/acme/jobs/1", text="Apply now", path=store)
    # A hiring page carries a precise marker, so one sighting is already enough.
    assert registry.lookup("jobs.newats.io", store) == "ats_requisitions"
    entry = registry.load(store)["domains"]["jobs.newats.io"]
    assert entry["promoted_by"] == "auto"
    assert entry["reason"] and entry["sample_urls"]
    assert registry.propose(store) == [], "a promoted domain is no longer a candidate"


def test_weaker_signals_need_a_second_look(tmp_path):
    """An ambiguous pattern waits for agreement instead of guessing."""
    store = tmp_path / "reg.json"
    registry.observe("https://dir.example/profile/acme/reviews", path=store)
    assert registry.lookup("dir.example", store) is None, "one weak sighting is not enough"
    registry.observe("https://dir.example/profile/beta/reviews", path=store)
    assert registry.lookup("dir.example", store) == "b2b_directory_audit"


def test_a_wrong_automatic_promotion_is_one_command_to_undo(tmp_path):
    """Automatic growth is only safe if correction is cheaper than review."""
    store = tmp_path / "reg.json"
    registry.observe("https://jobs.newats.io/acme/jobs/1", text="Apply now", path=store)
    assert registry.lookup("jobs.newats.io", store) == "ats_requisitions"

    registry.demote("jobs.newats.io", reason="not a hiring board", path=store)
    assert registry.lookup("jobs.newats.io", store) is None
    record = registry.load(store)["demotions"]["jobs.newats.io"]
    assert record["was"] == "ats_requisitions" and record["reason"] == "not a hiring board"

    with pytest.raises(ValueError, match="not in the registry"):
        registry.demote("never.example", path=store)


def test_an_explicit_promotion_is_recorded_as_manual(tmp_path):
    store = tmp_path / "reg.json"
    registry.promote("vendorhub.example", "vendor_registry", reason="a person said so", path=store)
    entry = registry.load(store)["domains"]["vendorhub.example"]
    assert entry["promoted_by"] == "manual" and entry["reason"] == "a person said so"


def test_an_ambiguous_domain_waits_as_a_candidate(tmp_path):
    """Weak signals accumulate without being promoted, and show up as waiting."""
    store = tmp_path / "reg.json"
    for index in range(2):
        registry.observe(f"https://weak.example/x{index}/clutch", path=store)
    assert registry.lookup("weak.example", store) is None, "one weak marker is not a category"
    proposals = registry.propose(store)
    assert [p["domain"] for p in proposals] == ["weak.example"]
    assert registry.load(store)["candidates"]["weak.example"]["sightings"] == 2


def test_a_promoted_source_is_evidence_and_an_unknown_one_is_a_lead(tmp_path):
    """Promotion is what turns a lead into a source that can carry claims."""
    from harness_fleet import contracts

    store = tmp_path / "reg.json"
    assert registry.lookup("vendorhub.example", store) is None
    assert contracts.qualifies("q8_independent_validation", "GENERAL_WEB") is False

    registry.promote("vendorhub.example", "vendor_registry", reason="vendor stories", path=store)
    assert registry.lookup("vendorhub.example", store) == "vendor_registry"
    assert contracts.qualifies("q8_independent_validation", "VENDOR_REGISTRY") is True


def test_signals_are_a_likelihood_not_a_lookup():
    """The math suggests; it does not decide."""
    ats = registry.score_signals("https://boards.example/acme/jobs", "Apply now, join our team")
    cases = registry.score_signals("https://vendor.example/customers/acme", "customer story")
    assert ats["ats_requisitions"] > 0
    assert cases["vendor_registry"] > 0
    assert registry.score_signals("") == {}


def test_classification_uses_a_promoted_domain(tmp_path, monkeypatch):
    """The loop closes: promote a domain and it classifies from then on."""
    from harness_fleet import registry, sources

    monkeypatch.chdir(tmp_path)
    # A path the seed rules cannot read: it says nothing about what the site is.
    assert sources.classify_source_category("https://vendorhub.example/press/acme") == "general_web"

    registry.promote("vendorhub.example", "vendor_registry", reason="vendor stories")
    # Promotion is authoritative: it overrides what the seed rules would guess.
    assert sources.classify_source_category("https://vendorhub.example/press/acme") == "vendor_registry"
    assert sources.classify_source_category("https://vendorhub.example/customers/acme") == "vendor_registry"
    # And a seed-list host still classifies exactly as before.
    assert sources.classify_source_category("https://aws.amazon.com/partners/success/acme/") == "vendor_registry"


def test_discovery_records_unknown_hosts_as_candidates(tmp_path, monkeypatch):
    """A source nobody listed is observed, not silently dropped."""

    from harness_fleet import discover, registry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discover, "search_hn", lambda q, **kw: [
        discover.SearchHit(url="https://brandnew.example/careers/1", title="Jobs", snippet="s", backend="hn"),
    ])
    monkeypatch.setattr(discover, "fetch_smart_url", lambda url, **kw: discover.RawRecord(
        text="Apply now", source_uri=url, title="t"))
    discover.run_discovery(["q"], backends=["hn"], delay=0)

    stored = registry.load(tmp_path / "source_registry.json")
    # A /careers page is precise, so discovery's own observation promotes it —
    # the taxonomy grew in the course of a normal run, with nobody promoting.
    assert stored["domains"]["brandnew.example"]["category"] == "ats_requisitions"
    assert stored["domains"]["brandnew.example"]["promoted_by"] == "auto"

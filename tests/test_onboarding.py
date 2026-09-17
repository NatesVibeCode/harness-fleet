"""What the onboarding supplies to a lane's questions.

The product has no seed. A lane declares the *shape* of its questions as
templates; the onboarding profile declares the *traits* — stack, industry, role,
pain — that fill them. These tests pin both halves of that join, and the two
things it must never do: invent a company query, or emit a placeholder that
nothing filled.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from harness_fleet.lanes import Lane, shipped_lanes  # noqa: E402
from harness_fleet.onboarding import (  # noqa: E402
    lane_queries,
    profile_query_terms,
)
from harness_fleet.partner import IdealPartnerProfile  # noqa: E402
from harness_fleet.profile import IdealCompanyProfile  # noqa: E402


def _lane(**fields):
    payload = {
        "name": "probe",
        "preset": "account-research",
        "queries": ['"{tech}" implementation'],
        "query_terms": {"tech": ["FallbackTech"]},
        "funnel": {"allows": "any"},
    }
    payload.update(fields)
    return Lane.model_validate(payload)


def test_a_lane_has_no_seed_field():
    """The idea is gone from the product, not merely unused.

    A lane that could still declare a seed would let a hard-coded anchor leak
    back in; the closed schema refuses the key outright.
    """
    assert not hasattr(_lane(), "seeds")
    with pytest.raises(Exception):
        _lane(seeds=["snowflake"])


def test_every_shipped_lane_names_its_onboarding_document():
    for name, lane in shipped_lanes().items():
        assert lane.onboarding_profile, (
            f"{name} names the onboarding document that supplies its traits"
        )


def test_the_profile_supplies_the_traits_the_template_asks_for():
    lane = _lane()
    icp = IdealCompanyProfile(required_stack=["Kafka", "Flink"])
    assert lane_queries(lane, icp) == ['"Kafka" implementation', '"Flink" implementation']


def test_the_lanes_own_terms_do_not_outvote_the_onboarding():
    lane = _lane()
    icp = IdealCompanyProfile(required_stack=["Kafka"])
    got = lane_queries(lane, icp)
    assert got == ['"Kafka" implementation']
    assert all("FallbackTech" not in query for query in got)


def test_without_onboarding_the_lane_still_runs_its_own_terms():
    """A fresh install with no profile is unchanged, which is the fallback."""
    assert lane_queries(_lane(), None) == ['"FallbackTech" implementation']


def test_axes_the_profile_does_not_state_keep_the_lanes():
    lane = _lane(
        queries=['"{tech}" "{vertical}"'],
        query_terms={"tech": ["FallbackTech"], "vertical": ["retail"]},
    )
    icp = IdealCompanyProfile(required_stack=["Kafka"])  # states tech only
    assert lane_queries(lane, icp) == ['"Kafka" "retail"']


def test_a_company_is_never_a_query():
    """An anchor calibrates a judgement; it is not a member to go and find.

    Seeding a search with a firm's name returns that firm's own pages, which can
    never be evidence about anybody else, so the anchor axis does not exist.
    """
    icp = IdealCompanyProfile(required_stack=["Kafka"], anchor_logos=["Stripe", "Monzo"])
    assert "anchor" not in profile_query_terms(icp)
    lane = _lane(
        queries=['"{tech}" case study', '"{anchor}" case study'],
        query_terms={"tech": ["FallbackTech"]},
    )
    got = lane_queries(lane, icp)
    assert got == ['"Kafka" case study']
    assert all("Stripe" not in query and "Monzo" not in query for query in got)


def test_a_template_nothing_can_fill_is_skipped_not_emitted():
    """`{role}` is not a search, so the question is not asked at all."""
    assert lane_queries(_lane(queries=['"{role}" remote'], query_terms={}), IdealCompanyProfile()) == []


def test_the_partner_profile_leads_with_the_ecosystem():
    ipp = IdealPartnerProfile(target_ecosystem="Kafka", required_adjacent_competencies=["dbt"])
    lane = _lane(queries=['"{tech}" consultancy'], query_terms={"tech": ["fallback"]})
    assert lane_queries(lane, ipp) == ['"Kafka" consultancy', '"dbt" consultancy']


def test_the_authoring_file_is_read_every_run(tmp_path):
    """Editing the profile must not be a no-op once a revision exists.

    Reading the store first made an edit to `ideal_partner_profile.json` silent
    the moment one revision was stored: an operator changes a size floor,
    re-runs, and gets the old profile. The file is the authoring form; the store
    is the record of it.
    """
    from harness_fleet.onboarding import load_onboarding_profile
    from harness_fleet.partner import IdealPartnerProfile
    from harness_fleet.store import HarnessStore

    class _Lane:
        onboarding_profile = "ideal_partner"

    (tmp_path / "ideal_partner_profile.json").write_text(
        '{"profile_name": "first", "target_ecosystem": "Kafka", "partner_size_min": 10}',
        encoding="utf-8",
    )
    store = HarnessStore(str(tmp_path / "s.db"))
    lane = _Lane()

    first = load_onboarding_profile(tmp_path, lane, None, store)
    assert first.partner_size_min == 10

    # The operator edits the file. The next run must see the edit.
    (tmp_path / "ideal_partner_profile.json").write_text(
        '{"profile_name": "second", "target_ecosystem": "Kafka", "partner_size_min": 250}',
        encoding="utf-8",
    )
    second = load_onboarding_profile(tmp_path, lane, None, store)
    assert second.partner_size_min == 250
    assert isinstance(second, IdealPartnerProfile)
    # Both edits are still on the record.
    assert store.active_profile_revision_id("ideal_partner")

"""The Ideal Employer Profile is the career lane's onboarding document.

It used to live in a ``career_fleet`` distribution that shipped its own CLI, its
own store and four company-screening lanes. Nothing called those lanes — the
package was kept alive by its own tests — so the cutover moved the one contract
that was live into the shared engine and deleted the rest.

What did not come with it is the ``dealbreakers`` block. Most of that block was
preference — no in-office mandate, no pure quota-carrying, nothing thinner than a
wrapper — and a preference is a value in one person's profile, not a field the
engine ships to every install. The one structural item, a headcount ceiling, was
already ``size_max``.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness_fleet.profile import IdealEmployerProfile


def test_default_profile():
    profile = IdealEmployerProfile()
    assert profile.profile_kind == "ideal_employer"
    assert profile.size_min == 0 and profile.size_max == 0, "unset leaves the gate off"
    assert profile.wedge_capabilities == []
    assert profile.target_roles == []


def test_save_and_load(tmp_path):
    path = tmp_path / "test_prof.json"
    IdealEmployerProfile(profile_name="Test Operator").save(path)
    assert path.exists()

    loaded = IdealEmployerProfile.load(path)
    assert loaded.profile_name == "Test Operator"
    assert loaded.size_max == 0


def test_a_missing_file_names_the_contract_it_wanted(tmp_path):
    with pytest.raises(FileNotFoundError, match="Ideal Employer Profile"):
        IdealEmployerProfile.load(tmp_path / "absent.json")


def test_unknown_profile_fields_are_rejected():
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"required_stak": ["Python"]})
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"anchor_company": ["Stripe"]})


def test_preferences_are_values_not_schema():
    """A stance is the author's, so the engine does not carry a field for it.

    ``extra="forbid"`` is the point: a document that still states one is told so
    rather than having its preference silently ignored.
    """
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"dealbreakers": {"reject_pure_quota": True}})
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"dealbreakers": {"max_headcount": 80}})


def test_a_negative_size_bound_is_rejected():
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"size_max": -1})


def test_the_employer_gate_reads_its_own_firmographics():
    profile = IdealEmployerProfile(
        size_min=50,
        size_max=800,
        target_territories=["United Kingdom"],
        target_industries=["fintech"],
    )
    funnel = profile.funnel_profile()
    assert funnel["allows"] == "any", "a product company employs people too"
    assert funnel["size_min"] == 50
    assert funnel["size_max"] == 800
    assert funnel["locations"] == ("United Kingdom",)
    assert funnel["verticals"] == ("fintech",)


def test_the_employer_query_terms_lead_with_the_role():
    profile = IdealEmployerProfile(
        target_roles=["enterprise sales"], required_stack=["Kafka"], hiring_catalysts=["new CRO"]
    )
    terms = profile.query_terms()
    assert terms["role"] == ["enterprise sales"]
    assert terms["tech"] == ["Kafka"]
    assert terms["pain"] == ["new CRO"]

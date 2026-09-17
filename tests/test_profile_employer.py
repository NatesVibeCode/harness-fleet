"""The Ideal Employer Profile is the career lane's onboarding document.

It used to live in a ``career_fleet`` distribution that shipped its own CLI, its
own store and four company-screening lanes. Nothing called those lanes — the
package was kept alive by its own tests — so the cutover moved the one contract
that was live into the shared engine and deleted the rest.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness_fleet.profile import IdealEmployerProfile


def test_default_profile():
    profile = IdealEmployerProfile()
    assert profile.profile_kind == "ideal_employer"
    assert profile.dealbreakers.max_headcount is None
    assert profile.dealbreakers.policy == "any"
    assert profile.dealbreakers.reject_thin_wrappers is False
    assert profile.wedge_capabilities == []


def test_save_and_load(tmp_path):
    path = tmp_path / "test_prof.json"
    IdealEmployerProfile(profile_name="Test Operator").save(path)
    assert path.exists()

    loaded = IdealEmployerProfile.load(path)
    assert loaded.profile_name == "Test Operator"
    assert loaded.dealbreakers.max_headcount is None


def test_a_missing_file_names_the_contract_it_wanted(tmp_path):
    with pytest.raises(FileNotFoundError, match="Ideal Employer Profile"):
        IdealEmployerProfile.load(tmp_path / "absent.json")


def test_unknown_profile_fields_are_rejected():
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"required_stak": ["Python"]})
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"dealbreakers": {"max_headcont": 10}})


def test_non_positive_headcount_limit_is_rejected():
    with pytest.raises(ValidationError):
        IdealEmployerProfile.model_validate({"dealbreakers": {"max_headcount": 0}})


def test_the_employer_gate_reads_its_own_firmographics():
    profile = IdealEmployerProfile(
        size_min=50,
        target_territories=["United Kingdom"],
        target_industries=["fintech"],
        dealbreakers={"max_headcount": 800},
    )
    funnel = profile.funnel_profile()
    assert funnel["allows"] == "any", "a product company employs people too"
    assert funnel["size_min"] == 50
    assert funnel["size_max"] == 800, "the ceiling falls back to the stated dealbreaker"
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

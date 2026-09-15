"""Tests for partner-fleet research preset and IdealPartnerProfile."""
from __future__ import annotations

import csv
from pathlib import Path

from harness_fleet.bundler import (
    bundle_records,
    canonicalize_entity_id,
    classify_source_category,
)
from harness_fleet.input_data import load_input_items
from harness_fleet.partner import IdealPartnerProfile
from harness_fleet.profile import IdealCompanyProfile
from harness_fleet.task import (
    PARTNER_CHECKLIST,
    PARTNER_CHECKLIST_DESCRIPTIONS,
    PARTNER_EVIDENCE_TERMS,
    PARTNER_HALF_LIVES,
    PRESETS,
    create_task_from_preset,
)


def test_partner_research_preset_structure():
    assert "partner-research" in PRESETS
    preset = PRESETS["partner-research"]
    assert preset["checklist"] == PARTNER_CHECKLIST
    assert sum(PARTNER_CHECKLIST.values()) == 100
    assert set(PARTNER_CHECKLIST.keys()) == {
        "q1_billable_delivery",
        "q2_stack_delivery",
        "q3_delivery_hiring",
        "q4_client_outcome",
        "q5_commercial_scale",
        "q6_vendor_alliance",
        "q7_vertical_focus",
        "q8_independent_validation",
        "q9_published_engineering",
        "q10_growth_signal",
    }
    assert set(PARTNER_CHECKLIST_DESCRIPTIONS.keys()) == set(PARTNER_CHECKLIST.keys())
    assert len(PARTNER_EVIDENCE_TERMS) >= 10
    assert "partner" in PARTNER_EVIDENCE_TERMS
    assert "consulting" in PARTNER_EVIDENCE_TERMS
    assert "implementation" in PARTNER_EVIDENCE_TERMS
    # The revenue checklist must stay solvable from public sourcing signals only.
    assert PARTNER_HALF_LIVES["q3_delivery_hiring"] == 21.0
    assert PARTNER_HALF_LIVES["q10_growth_signal"] == 90.0
    assert set(PARTNER_HALF_LIVES.keys()) == set(PARTNER_CHECKLIST.keys())


def test_create_task_from_partner_research_preset():
    task = create_task_from_preset("partner_qual", preset_name="partner-research")
    assert task.name == "partner_qual"
    assert task.format_version == "harness_fleet_task_v1"
    assert task.checklist == PARTNER_CHECKLIST
    assert task.evidence_terms == PARTNER_EVIDENCE_TERMS
    assert task.recency_half_lives == PARTNER_HALF_LIVES
    assert "checklist" in task.claims_schema["properties"]
    assert "score" in task.claims_schema["properties"]
    assert "answers" in task.claims_schema["properties"]
    assert "source_diversity_count" in task.claims_schema["properties"]
    assert "fit_tier" in task.claims_schema["properties"]
    assert task.claims_schema["required"] == [
        "checklist", "identified_practice", "revenue_hypothesis", "reasoning",
    ]
    answers = task.claims_schema["properties"]["answers"]
    assert {"client_logos", "hiring_signals", "commercial_terms", "revenue_motion",
            "third_party_mentions", "engineering_output", "growth_signals",
            "evidence_categories"} <= set(answers["properties"])
    # Every attribute is required so a blank is an explicit "not in the sources".
    assert set(answers["required"]) == set(answers["properties"])


def test_bundler_canonicalize_and_classify():
    assert canonicalize_entity_id("https://www.trace3.com/services/cloud") == "trace3.com"
    assert canonicalize_entity_id("https://boards.greenhouse.io/trace3/jobs/123") == "trace3.com"
    assert canonicalize_entity_id("https://jobs.ashbyhq.com/slalom/architect") == "slalom.com"
    assert canonicalize_entity_id("https://partners.amazonaws.com/partners/trace3") == "trace3.com"
    assert canonicalize_entity_id("https://clutch.co/profile/trace3") == "trace3.com"

    assert classify_source_category("https://partners.amazonaws.com/partners/trace3") == "vendor_registry"
    assert classify_source_category("https://clutch.co/profile/trace3") == "b2b_directory_audit"
    assert classify_source_category("https://github.com/trace3") == "community_and_social"
    assert classify_source_category("https://boards.greenhouse.io/trace3/123") == "ats_requisitions"
    assert classify_source_category("https://trace3.com/case-studies/kafka") == "first_party_case_study"
    assert classify_source_category("https://trace3.com/services/cloud") == "first_party_practice"


def test_bundler_groups_multi_source_hits_into_composite_dossier():
    raw_records = [
        {
            "item_id": "https://trace3.com/services/cloud",
            "text": "Trace3 provides cloud architecture, systems integration, and migration services.",
            "source_uri": "https://trace3.com/services/cloud",
        },
        {
            "item_id": "https://trace3.com/case-studies/kafka",
            "text": "Case Study: Leading Apache Kafka streaming deployment for top-10 financial services customer.",
            "source_uri": "https://trace3.com/case-studies/kafka",
        },
        {
            "item_id": "https://partners.amazonaws.com/partners/trace3",
            "text": "AWS Premier Tier Services Partner with Financial Services Competency.",
            "source_uri": "https://partners.amazonaws.com/partners/trace3",
        },
        {
            "item_id": "https://boards.greenhouse.io/trace3/lead-architect",
            "text": "Hiring Solutions Architects to architect distributed Kafka clusters for clients.",
            "source_uri": "https://boards.greenhouse.io/trace3/lead-architect",
        },
    ]

    bundled = bundle_records(raw_records, min_sources=2, min_categories=2)
    assert len(bundled) == 1
    dossier = bundled[0]
    assert dossier.item_id == "trace3.com"
    assert dossier.metadata["source_count"] == 4
    assert dossier.metadata["category_count"] >= 3
    assert "SECTION: FIRST_PARTY_PRACTICE" in dossier.text
    assert "SECTION: FIRST_PARTY_CASE_STUDY" in dossier.text
    assert "SECTION: VENDOR_REGISTRY" in dossier.text
    assert "SECTION: ATS_REQUISITIONS" in dossier.text


def test_ideal_partner_profile_lifecycle(tmp_path):
    profile = IdealPartnerProfile(
        profile_name="Snowflake Partner Profile",
        version="1.0.0",
        target_ecosystem="Snowflake",
        service_models=["Systems Integration", "Migration"],
        required_adjacent_competencies=["dbt", "AWS"],
        target_client_segment=["Enterprise"],
        target_partner_tier="Regional SI",
        key_delivery_roles=["Solutions Architect", "Data Consultant"],
        negative_exclusions=["Pure SaaS", "Staffing"],
        anchor_partners=["slalom.com"],
        calibrated_scoring_rubric={"tier_1": {"definition": "Certified partner with case study"}},
    )

    # Context rendering
    context = profile.to_prompt_context()
    assert "Snowflake" in context
    assert "Systems Integration" in context
    assert "Solutions Architect" in context

    # Save and reload
    save_path = tmp_path / "ideal_partner_profile.json"
    profile.save(save_path)
    assert save_path.is_file()

    loaded = IdealPartnerProfile.load(save_path)
    assert loaded.target_ecosystem == "Snowflake"
    assert loaded.anchor_partners == ["slalom.com"]

    # Bridge to IdealCompanyProfile
    company_profile = profile.to_company_profile()
    assert isinstance(company_profile, IdealCompanyProfile)
    assert "Snowflake" in company_profile.required_stack
    assert "dbt" in company_profile.required_stack
    assert "Solutions Architect" in company_profile.target_roles


def test_sample_partners_csv_validity():
    import pytest

    csv_path = Path(__file__).resolve().parents[1] / "examples/partner_research/sample_partners.csv"
    if not csv_path.is_file():
        pytest.skip("this distribution does not ship the partner research examples")

    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) >= 5
    assert set(reader.fieldnames or []) == {"domain", "partner_name", "research"}
    for row in rows:
        assert row["domain"]
        assert row["partner_name"]
        assert len(row["research"]) > 30

    items = load_input_items(csv_path, id_column="domain", text_column="research")
    assert len(items) == len(rows)
    assert all(item.item_id for item in items)
    assert all(item.text for item in items)

"""Evidence kinds, enforced minimums, contradictions and snapshot diffs."""
from harness_fleet.evidence import (
    contradictions,
    coverage,
    diff_snapshots,
    enforce_tier,
    entity_evidence,
    missing_minimums,
    parse_sections,
    summarize,
)

DOSSIER = """# Multi-Source Evidence Dossier: acme.example

=== SECTION: FIRST_PARTY_CASE_STUDY (URI: https://acme.example/case-studies/bank) ===
Acme implemented a Kafka migration for Northwind Bank in 2025 and cut latency by 40%.
Certified Snowflake Premier Partner.

=== SECTION: FIRST_PARTY_PRACTICE (URI: https://acme.example/services) ===
We deliver managed services and integration work for clients.

=== SECTION: ATS_REQUISITIONS (URI: https://boards.greenhouse.io/acme) ===
Senior Kafka Solutions Architect, remote, posted 2026.

=== SECTION: VENDOR_REGISTRY (URI: https://aws.amazon.com/partners/success/acme-bank/) ===
Acme built a streaming platform for Northwind Bank on AWS.
"""

FIRST_PARTY_ONLY = """# Multi-Source Evidence Dossier: solo.example

=== SECTION: FIRST_PARTY_PRACTICE (URI: https://solo.example/services) ===
We deliver managed services for clients and hold a Premier Partner certification.
Engagements start at a $50,000 minimum project size with 40 employees on staff.
"""

#: One firm's own case study: quantified delivery and a named stack, but nothing
#: independent. It supports tier_2 and cannot support tier_1.
FIRST_PARTY_ONLY_WITH_STACK = (
    "=== SECTION: FIRST_PARTY_CASE_STUDY (URI: https://acme.com/case-studies/bank) ===\n"
    "Acme implemented a Kafka migration for Northwind Bank and cut latency by 40%."
)


def test_parse_sections_reads_category_uri_and_body():
    sections = parse_sections(DOSSIER)
    assert [s["category"] for s in sections] == [
        "FIRST_PARTY_CASE_STUDY", "FIRST_PARTY_PRACTICE", "ATS_REQUISITIONS", "VENDOR_REGISTRY",
    ]
    assert sections[0]["uri"] == "https://acme.example/case-studies/bank"
    assert "Northwind Bank" in sections[0]["text"]


def test_coverage_separates_kinds_from_scaffolding():
    kinds = coverage(DOSSIER)
    assert kinds["delivery_proof"] is True
    assert kinds["independent_validation"] is True
    assert kinds["delivery_hiring"] is True
    assert kinds["dated_events"] is True
    assert kinds["named_clients"] is True


def test_coverage_of_a_first_party_only_dossier_lacks_independence():
    kinds = coverage(FIRST_PARTY_ONLY)
    assert kinds["independent_validation"] is False
    assert kinds["delivery_hiring"] is False
    # The terms are stated — by the firm itself, which is why the contradiction
    # pass flags them as unverified rather than counting them as evidence.
    assert kinds["commercial_terms"] is True


def test_minimums_cap_a_tier_the_evidence_cannot_support():
    kinds = coverage(FIRST_PARTY_ONLY)
    assert "independent_validation" in missing_minimums(kinds, "tier_1")
    tier, reasons = enforce_tier("tier_1", kinds)
    assert tier in {"tier_2", "tier_3"}, "an unvalidated dossier cannot be tier_1"
    assert reasons, "the reason is recorded, not averaged away"
    assert any("independent_validation" in reason for reason in reasons), reasons
    # A dossier with case study + vendor story + hiring supports tier_1.
    assert enforce_tier("tier_1", coverage(DOSSIER)) == ("tier_1", [])
    # The reason names the claimed tier and the gap, even when a lower tier is
    # fully supported — that is the case a bare lower number would hide.
    downgraded, why = enforce_tier("tier_1", coverage(FIRST_PARTY_ONLY_WITH_STACK))
    assert downgraded == "tier_2"
    assert why == ["tier_1 needs independent_validation"], why


def test_contradictions_name_the_claim_and_the_missing_counterpart():
    findings = contradictions(FIRST_PARTY_ONLY)
    kinds = {f["kind"] for f in findings}
    assert "certification_unverified" in kinds
    assert "commercial_terms_unverified" in kinds
    cert = next(f for f in findings if f["kind"] == "certification_unverified")
    assert cert["uris"] == ["https://solo.example/services"]
    assert "no vendor-registry" in cert["counterpart"]


def test_contradictions_stay_quiet_when_a_third_party_echoes_the_claim():
    """A stack the vendor story itself names is corroborated, not contradicted."""
    corroborated = DOSSIER.replace(
        "Acme built a streaming platform for Northwind Bank on AWS.",
        "Acme built a Kafka streaming platform for Northwind Bank on AWS and Snowflake.",
    )
    kinds = {f["kind"] for f in contradictions(corroborated)}
    assert "stack_claim_uncorroborated" not in kinds
    assert "hiring_contradicts_claimed_stack" not in kinds, "the ATS board names Kafka too"


def test_contradictions_flag_a_stack_only_the_firm_claims():
    """Kafka is echoed by the job post; Snowflake is claimed by nobody else."""
    findings = {f["kind"]: f for f in contradictions(DOSSIER)}
    assert "stack_claim_uncorroborated" in findings
    claim = findings["stack_claim_uncorroborated"]["claim"]
    assert "Snowflake" in claim
    assert "Kafka" not in claim, "the ATS board independently names Kafka"
    assert "no independent or vendor source" in findings["stack_claim_uncorroborated"]["counterpart"]


def test_certification_claimed_only_in_first_party_material_is_flagged():
    kinds = {f["kind"] for f in contradictions(DOSSIER)}
    assert "certification_unverified" in kinds


def test_diff_reports_added_removed_and_changed_sections():
    old = """# Dossier

=== SECTION: FIRST_PARTY_PRACTICE (URI: https://acme.example/services) ===
We deliver integration work.

=== SECTION: FIRST_PARTY_CASE_STUDY (URI: https://acme.example/case-studies/old) ===
An old case study about a migration.
"""
    new = """# Dossier

=== SECTION: FIRST_PARTY_PRACTICE (URI: https://acme.example/services) ===
We deliver integration and managed services work.

=== SECTION: VENDOR_REGISTRY (URI: https://aws.amazon.com/partners/success/acme/) ===
Acme built a platform.
"""
    result = diff_snapshots(old, new, old_date="2024-01-01", new_date="2026-01-01")
    assert result["evidence_changed"] is True
    assert result["old_date"] == "2024-01-01" and result["new_date"] == "2026-01-01"
    assert [s["uri"] for s in result["sections_removed"]] == ["https://acme.example/case-studies/old"]
    assert [s["uri"] for s in result["sections_added"]] == ["https://aws.amazon.com/partners/success/acme/"]
    changed = result["changed"][0]
    assert changed["uri"] == "https://acme.example/services"
    assert any("managed services" in s for s in changed["sentences_added"])


def test_diff_of_identical_captures_reports_nothing():
    result = diff_snapshots(DOSSIER, DOSSIER, old_date="2025-01-01", new_date="2026-01-01")
    assert result["evidence_changed"] is False
    assert result["changed_count"] == 0


def test_entity_evidence_bundles_it_for_a_record():
    payload = entity_evidence(DOSSIER, tier="tier_1")
    assert payload["tier_supported"] == "tier_1"
    assert payload["tier_capped"] is False
    assert set(payload["kinds"]) == {
        "delivery_proof", "named_clients", "stack_delivery", "independent_validation",
        "delivery_hiring", "commercial_terms", "certification", "engineering_output",
        "growth_signal", "dated_events",
    }


def test_summarize_counts_kinds_across_entities():
    totals = summarize([coverage(DOSSIER), coverage(FIRST_PARTY_ONLY)])
    assert totals["independent_validation"] == 1
    assert totals["delivery_proof"] == 2


def test_the_central_contracts_are_self_consistent():
    """Every cross-reference in the contracts resolves, and they are the source."""
    from harness_fleet import contracts, evidence

    assert contracts.validate_contracts() == []
    # One declaration, read by the reading layer: not a copy that can drift.
    assert evidence.MINIMUM_KINDS is contracts.TIER_MINIMUMS
    assert evidence.INDEPENDENT_CATEGORIES == contracts.INDEPENDENT_CATEGORIES
    assert set(contracts.EVIDENCE_KINDS) >= {
        "delivery_proof", "stack_delivery", "independent_validation", "delivery_hiring",
    }
    # Two levels, both priced; everything else was retired rather than tuned.
    assert set(contracts.CONFIDENCE_LEVELS) == {"reported", "unsupported"}
    for level, weight in contracts.CONFIDENCE_LEVELS.items():
        assert contracts.confidence_weight(level) == weight


def test_the_bar_decides_what_is_evidence():
    """A source either can carry the claim or it is a lead. No middle band."""
    from harness_fleet import contracts

    assert contracts.qualifies("q1_billable_delivery", "VENDOR_REGISTRY") is True
    assert contracts.qualifies("q1_billable_delivery", "GENERAL_WEB") is False
    assert contracts.qualifies("q8_independent_validation", "VENDOR_REGISTRY") is True
    assert contracts.qualifies("q8_independent_validation", "FIRST_PARTY_PRACTICE") is False
def test_a_third_party_requirement_rejects_first_party_material():
    from harness_fleet.contracts import quote_addresses_claim

    ok, why = quote_addresses_claim(
        "q8_independent_validation", "Acme delivered a Kafka migration.", "FIRST_PARTY_CASE_STUDY"
    )
    assert ok is False and "third party" in why

"""The central contracts are the only home for a shared rule.

A rule that two altitudes must agree on cannot be declared twice: the copies
drift, and the drift shows up as a scoring bug far from its cause. These tests
hold the boundary mechanically.
"""
from __future__ import annotations

import re
from pathlib import Path

from harness_fleet import contracts, evidence

ENGINE = Path(__file__).resolve().parents[1] / "harness_fleet"
#: Rules that must exist in exactly one module: the contracts.
CENTRAL_SIGNALS = {
    "OUTCOME_RE": "outcome",
    "DELIVERY_RE": "delivery verb",
    "GROWTH_RE": "growth event",
    "CERT_RE": "certification",
    "COMMERCIAL_RE": "commercial terms",
}


def test_the_contracts_are_self_consistent():
    assert contracts.validate_contracts() == []


def test_every_layer_reads_the_same_rule_objects():
    """Not equal copies — the same objects, so a change lands everywhere."""
    assert evidence.OUTCOME_RE is contracts.OUTCOME_RE
    assert evidence.DELIVERY_VERB_RE is contracts.DELIVERY_RE
    assert evidence.COMMERCIAL_RE is contracts.COMMERCIAL_RE
    assert evidence.CERT_RE is contracts.CERT_RE
    assert evidence.GROWTH_RE is contracts.GROWTH_RE
    assert evidence.MINIMUM_KINDS is contracts.TIER_MINIMUMS


def _central_rule_offenders(name: str, text: str) -> list[str]:
    """Rules that a module re-declares, by the same test the engine files get."""
    offenders: list[str] = []
    for signal_name, signal in CENTRAL_SIGNALS.items():
        if re.search(rf"(?m)^{signal_name}\s*=\s*re\.compile", text):
            offenders.append(f"{name} re-declares {signal_name} ({signal})")
    for match in re.finditer(r"(?m)^([A-Z_]+)\s*=\s*re\.compile", text):
        # A layer may compile its own structural parsers (section markers,
        # headings), but a signal pattern copied from the contract is a second
        # home for a rule that must not have one.
        snippet = text[match.start(): match.start() + 400]
        for signal_name, signal in CENTRAL_SIGNALS.items():
            canonical = contracts.__dict__[signal_name].pattern[:40]
            if canonical and canonical in snippet:
                offenders.append(f"{name}:{match.group(1)} duplicates {signal_name} ({signal})")
    return offenders


def test_no_layer_redeclares_a_central_rule():
    """Only contracts.py may define a central signal pattern."""
    offenders: list[str] = []
    for path in sorted(ENGINE.glob("*.py")):
        if path.name == "contracts.py":
            continue
        offenders.extend(_central_rule_offenders(path.name, path.read_text(encoding="utf-8")))
    assert not offenders, "central rules must have exactly one home:\n  " + "\n  ".join(offenders)


def test_a_second_home_is_actually_caught(tmp_path):
    """Negative control: a genuine copy of a central rule must be flagged."""
    canonical = contracts.GROWTH_RE.pattern
    rogue = (
        "import re\n"
        f"GROWTH_RE = re.compile(r'{canonical}')\n"
    )
    offenders = _central_rule_offenders("rogue.py", rogue)
    assert offenders, "a copied central rule must be reported"
    assert any("GROWTH_RE" in entry for entry in offenders), offenders

    # And a structural parser of its own is not flagged: the boundary is about
    # shared rules, not about forbidding every regex outside the contract.
    structural = "import re\nSECTION_RE = re.compile(r'^==+ (?P<title>.+)$', re.M)\n"
    assert _central_rule_offenders("reader.py", structural) == []


def test_a_source_that_cannot_carry_a_claim_is_not_evidence():
    """A flood of weak sources is not averaged in: it is not evidence at all."""
    from harness_fleet import contracts

    assert contracts.qualifies("q1_billable_delivery", "VENDOR_REGISTRY") is True
    assert contracts.qualifies("q1_billable_delivery", "COMMUNITY_AND_SOCIAL") is False
    assert contracts.support_strength("q1_billable_delivery", ["COMMUNITY_AND_SOCIAL"] * 20) == 0.0
    assert contracts.support_strength("q1_billable_delivery", ["VENDOR_REGISTRY"]) == 1.0
    assert contracts.qualifies("q8_independent_validation", "FIRST_PARTY_PRACTICE") is False
    assert contracts.qualifies("q3_delivery_hiring", "ATS_REQUISITIONS") is True
    assert contracts.qualifies("q3_delivery_hiring", "FIRST_PARTY_PRACTICE") is False


def test_there_is_no_confidence_continuum_to_tune():
    from harness_fleet import contracts

    assert set(contracts.CONFIDENCE_LEVELS) == {"reported", "unsupported"}
    assert contracts.confidence_weight("reported") == 1.0
    assert contracts.confidence_weight("unsupported") == 0.0
    for retired in ("SOURCE_TRUST", "VAGUE_SPECIFICITY", "CORROBORATION_STEP", "UNTRUSTED_CEILING"):
        assert not hasattr(contracts, retired), f"{retired} should be gone, not tuned"


def test_the_bar_decides_whether_a_claim_is_scored():
    from harness_fleet.task import create_task_from_preset

    task = create_task_from_preset("partner-research", preset_name="partner-research")
    first = ("=== SECTION: FIRST_PARTY_CASE_STUDY (URI: https://acme.com/cs) ===\n"
             "Acme implemented a Kafka migration and cut latency 40%.")
    quote = {"text": "Acme implemented a Kafka migration and cut latency 40%.",
             "start": first.index("Acme"), "supports": ["q8_independent_validation"]}
    assert task.support_strengths([quote], "https://acme.com/cs", text=first) == {}

    vendor = first + ("\n\n=== SECTION: VENDOR_REGISTRY (URI: https://aws.amazon.com/partners/success/acme/) ===\n"
                      "Acme built a streaming platform with Kafka. The vendor publishes a detailed customer story describing the migration, the platform it ran on, the team that delivered it and the measured result.")
    vendor_quote = {"text": "Acme built a streaming platform with Kafka. The vendor publishes a detailed customer story describing the migration, the platform it ran on, the team that delivered it and the measured result.",
                    "start": vendor.index("Acme built"), "supports": ["q8_independent_validation"]}
    scored = task.support_strengths([vendor_quote], "https://acme.com/cs", text=vendor)
    assert scored["q8_independent_validation"] == 1.0


def test_the_readout_shows_two_levels():
    from harness_fleet.evidence import entity_evidence

    vendor = ("=== SECTION: VENDOR_REGISTRY (URI: https://aws.amazon.com/partners/success/a/) ===\n"
              "Acme built a Kafka platform for a bank. The vendor publishes a detailed customer story describing the migration, the platform it ran on, the team that delivered it and the measured result.")
    levels = {f["kind"]: f["confidence"] for f in entity_evidence(vendor)["confidences"]}
    assert levels["independent_validation"] == "reported"
    assert all(level in {"reported", "unsupported"} for level in levels.values())


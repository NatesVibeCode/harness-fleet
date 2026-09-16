"""Shrink a world of possibilities: eliminate cheaply, qualify only on evidence."""
from __future__ import annotations

from harness_fleet.gates import (
    FETCHED,
    SNIPPET,
    check_vertical,
    funnel_counts,
    read_size,
    run_funnel,
)

PROFILE = {
    "allows": "services",
    "size_min": 20,
    "size_max": 5000,
    "locations": ("United States", "United Kingdom", "Canada"),
    "verticals": ("fintech", "healthcare"),
}


def test_a_snippet_may_eliminate_but_never_qualify():
    """Somebody else's summary is good enough to rule a firm out, never in.

    A snippet that reads like a delivery firm leaves the gate unknown — the
    candidate has earned a page fetch, not a pass.
    """
    report = run_funnel("acme.com", snippet="Acme is a data consultancy of 200 people in Boston", profile=PROFILE)
    kind = report.results[0]
    assert kind.outcome == "unknown" and kind.evidence == SNIPPET
    assert report.verdict == "lead", "not qualified on a snippet"
    assert report.unresolved == ["kind", "location"], report.unresolved


def test_a_product_company_is_eliminated_at_the_first_gate():
    report = run_funnel(
        "vendor.io",
        snippet="Our platform helps teams ship faster. Book a demo. Pricing plans for every team.",
        profile=PROFILE,
    )
    assert report.verdict == "eliminated"
    assert "product company" in report.because()


def test_size_is_decisive_and_a_wrong_size_never_reaches_the_semantics():
    """Nobody at the wrong-sized company gets approval, however keen they are."""
    small = run_funnel("tiny.io", snippet="A boutique consultancy, team of 6, in Austin", profile=PROFILE)
    assert small.verdict == "eliminated" and "below the profile's floor" in small.because()

    huge = run_funnel("giant.com", snippet="A consultancy with 45,000 employees", profile=PROFILE)
    assert huge.verdict == "eliminated" and "above the profile's ceiling" in huge.because()


def test_out_of_territory_is_eliminated():
    report = run_funnel("acme.de", snippet="A consultancy of 300 people headquartered in Munich, Germany", profile=PROFILE)
    assert report.verdict == "eliminated"
    assert "outside the profile's territory" in report.because()


def test_unknown_is_neither_a_pass_nor_a_failure():
    """A gate we cannot resolve from what we have read is recorded as unknown."""
    report = run_funnel("mystery.com", snippet="We build things for people.", profile=PROFILE)
    assert not report.eliminated and not report.qualified
    assert report.verdict == "lead"
    gate = report.results[1]
    assert gate.gate == "size" and gate.outcome == "unknown"
    assert "no headcount stated" in gate.reason


def test_a_fetched_page_may_qualify_where_a_snippet_may_not():
    from harness_fleet.gates import check_kind
    assert check_kind("We are a consultancy serving banks", allows="services", evidence=SNIPPET).outcome == "unknown"
    assert check_kind("We are a consultancy serving banks", allows="services", evidence=FETCHED).outcome == "pass"


def test_size_parsing_reads_what_companies_write():
    assert read_size("a team of 120 engineers") == 120
    assert read_size("1,200 employees worldwide") == 1200
    assert read_size("no numbers here") is None, "never inferred"


def test_vertical_is_the_expensive_gate_and_says_so():
    """Verticals live in the case studies, so an unresolved vertical is a lead."""
    unresolved = check_vertical((), wanted=("fintech",))
    assert unresolved.outcome == "unknown" and unresolved.evidence == FETCHED
    assert "case studies" in unresolved.reason
    assert check_vertical(("fintech",), wanted=("fintech",)).outcome == "pass"
    assert check_vertical(("retail",), wanted=("fintech",)).outcome == "fail"


def test_the_funnel_reports_where_the_world_shrank():
    """The number a person tunes thresholds by, not a list of companies."""
    reports = [
        run_funnel("a.com", snippet="Our platform is a SaaS product", profile=PROFILE),
        run_funnel("b.com", snippet="A consultancy, team of 4, in Boston", profile=PROFILE),
        run_funnel("c.com", snippet="A consultancy of 300 people in Boston", profile=PROFILE),
    ]
    counts = funnel_counts(reports)
    assert counts["candidates"] == 3
    assert counts["eliminated_at"]["kind"] == 1
    assert counts["eliminated_at"]["size"] == 1
    assert counts["verdicts"]["lead"] == 1
    assert counts["unresolved_at"]["kind"] == 1, "the survivor is unresolved on kind, not passed"


def test_a_report_survives_the_round_trip_a_reader_sees():
    report = run_funnel("acme.com", snippet="A consultancy, team of 4, in Austin", profile=PROFILE)
    payload = report.as_dict()
    assert payload["verdict"] == "eliminated"
    assert payload["because"].startswith("size:")
    assert payload["fetched"] is False, "an eliminated candidate never spends a fetch"
    assert all({"gate", "outcome", "reason", "evidence"} <= set(g) for g in payload["gates"])

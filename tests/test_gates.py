"""Shrink a world of possibilities: eliminate cheaply, qualify only on evidence."""
from __future__ import annotations

from harness_fleet.gates import (
    FETCHED,
    SNIPPET,
    LadderRung,
    check_vertical,
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
    locations = {r.gate: r for r in report.results}
    assert locations["location"].outcome == "pass", "Boston is in the United States"
    # The gates a snippet cannot settle, and the one no snippet could ever
    # settle: the vertical only the case studies name. Both are reported, so a
    # lead says what actually stands between it and a pass.
    assert set(report.unresolved) == {"kind", "vertical"}
    assert "fintech" not in report.because(), "an unresolved vertical is not a failure"


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


def test_the_table_reports_where_the_world_shrank():
    """The number a person tunes thresholds by, counted off the rows."""
    from harness_fleet.rungs import count_rows, gate_rows

    rows, _ = gate_rows(
        [
            _record("a.com", "Our platform is a SaaS product"),
            _record("b.com", "A consultancy, team of 4, in Boston"),
            _record("c.com", "A consultancy of 300 people in Boston"),
        ],
        rung=LadderRung(name="result", evidence=SNIPPET, gates=["kind", "size"]),
        ladder=[LadderRung(name="result", evidence=SNIPPET, gates=["kind", "size"])],
        profile=PROFILE,
        evidence=SNIPPET,
    )
    counts = count_rows(rows)
    assert counts["candidates"] == 3
    assert counts["eliminated_at"]["kind"] == 1
    assert counts["eliminated_at"]["size"] == 1
    assert counts["verdicts"]["lead"] == 1
    assert counts["unresolved_at"]["kind"] == 1, "the survivor is unresolved on kind, not passed"
    eliminated = [row["candidate"] for row in rows if row["outcome"] == "eliminated"]
    assert eliminated == ["a.com", "b.com"], "and the table names them"


def test_a_report_survives_the_round_trip_a_reader_sees():
    report = run_funnel("acme.com", snippet="A consultancy, team of 4, in Austin", profile=PROFILE)
    payload = report.as_dict()
    assert payload["verdict"] == "eliminated"
    assert payload["because"].startswith("size:")
    assert payload["fetched"] is False, "an eliminated candidate never spends a fetch"
    assert all({"gate", "outcome", "reason", "evidence"} <= set(g) for g in payload["gates"])


def test_the_gates_match_substance_not_wording():
    """Companies do not use your words.

    A firm calls itself an "advisory" while the profile says "consultancy", says
    "banking" while the profile says "fintech", says "London" while the profile
    says "United Kingdom". Matching literally makes a gate fire on wording, and
    a real consultancy reads as "nothing here says what kind of company it is".
    """
    from harness_fleet.gates import variants

    assert "consultancy" in variants("advisory")
    assert "fintech" in variants("banking")
    assert "united kingdom" in variants("london")

    firm = "An advisory firm of 200 people in London serving banking and insurance clients"
    report = run_funnel("acme.co.uk", snippet=firm, profile=PROFILE)
    kinds = {r.gate: r for r in report.results}
    assert kinds["kind"].outcome == "unknown", "a snippet still cannot qualify"
    assert kinds["size"].outcome == "pass", "200 people is inside the range"
    assert kinds["location"].outcome == "pass", "London is the United Kingdom"
    assert "London" in kinds["location"].reason, "the report quotes what the page said"

    vertical = check_vertical(("banking", "insurance"), wanted=("fintech",))
    assert vertical.outcome == "pass" and "fintech" in vertical.reason
    assert check_vertical(("retail",), wanted=("fintech",)).outcome == "fail"


def test_synonyms_do_not_widen_a_gate_into_nonsense():
    """A synonym group is a vocabulary, not a licence to match anything."""
    from harness_fleet.gates import matches_any

    assert matches_any("we do data engineering", ("fintech",)) == []
    assert matches_any("headquartered in Berlin", ("germany",)) == ["germany"]
    assert matches_any("", ("fintech",)) == []


def test_a_lane_that_does_not_ask_the_kind_question_does_not_answer_it(monkeypatch):
    """A competitor's customers are buyers, and a buyer may be a product company.

    The account lane carried `allows: "services"`, the partner lane's population
    test: it eliminated software companies and left every other candidate
    unresolved on kind — so no account could ever qualify, and the gate was
    answering a question that lane never asked.
    """
    from harness_fleet.gates import check_kind, run_funnel

    product = (
        "Our platform is a SaaS product, built by our team of 300 people. "
        "Book a demo. Pricing plans for every team."
    )
    assert check_kind(product, allows="services", evidence="fetched").outcome == "fail"
    assert check_kind(product, allows="any", evidence="fetched").outcome == "pass"

    report = run_funnel("buyer.com", snippet=product, profile={"allows": "any", "size_max": 5000})
    kinds = {r.gate: r for r in report.results}
    assert "kind" not in kinds, "a question nobody asked does not appear in the trace"
    assert "kind" not in report.unresolved, "an unasked question is not an unresolved one"
    assert kinds["size"].outcome == "pass", "the gates the lane does ask still run"


def test_the_shipped_account_lane_no_longer_runs_the_partner_test():
    import json
    from pathlib import Path

    lane = json.loads(
        (Path(__file__).resolve().parents[1] / "harness_fleet/resources/lanes/account.json").read_text()
    )
    assert lane["funnel"]["allows"] == "any"
    assert all("kind" not in rung["gates"] for rung in lane["funnel"]["ladder"])
    partner = json.loads(
        (Path(__file__).resolve().parents[1] / "harness_fleet/resources/lanes/partner.json").read_text()
    )
    assert partner["funnel"]["allows"] == "services", "the partner population test stays"
    assert "kind" in partner["funnel"]["ladder"][0]["gates"]


def _record(item_id: str, text: str):
    class _Record:
        pass

    record = _Record()
    record.item_id = item_id
    record.text = text
    record.source_uri = ""
    record.quotes = []
    record.metadata = {}
    return record

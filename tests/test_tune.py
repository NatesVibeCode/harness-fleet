"""Tests for the unified evaluation and tuning tool (harness_fleet.tune and tools/lane_tune.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_fleet.gates import FETCHED, SNIPPET
from harness_fleet.tune import (
    BENCHMARK_CORPUS,
    audit_database,
    diagnose_gates,
    diagnose_gates_batch,
    evaluate_surface,
    format_audit_report,
    format_batch_gate_report,
    format_benchmark_result,
    format_gate_report,
    format_sim_report,
    format_surface_report,
    load_batch_file,
    load_benchmark_corpus,
    run_benchmark,
    simulate_candidate,
)
from tools.lane_tune import main as lane_tune_main


def test_diagnose_gates_for_consultancy():
    text = (
        "Royal Cyber is an IT consulting and systems integrator of 250 people in Chicago. "
        "Our platform engineering team delivers cloud migrations and Kafka platforms for banking clients. "
        "Book a demo of our commerce accelerator."
    )
    report = diagnose_gates(text, evidence=FETCHED, lane_name="partner", profile={
        "allows": "services", "size_min": 50, "size_max": 1000,
        "locations": ["United States"], "verticals": ["fintech"],
    })
    assert report.kind.outcome == "pass"
    assert report.kind.verdict_kind == "services"
    assert "our platform" in report.kind.software_matches
    assert "consulting" in report.kind.services_matches
    assert report.size.outcome == "pass"
    assert report.size.stated_size == 250
    assert report.location.outcome == "pass"
    assert "Chicago" in report.location.stated_location
    assert report.vertical.outcome == "pass"
    assert "fintech" in report.vertical.found_verticals
    assert report.overall_verdict == "qualified"

    rendered = format_gate_report(report)
    assert "KIND GATE: PASS" in rendered
    assert "SIZE GATE: PASS" in rendered


def test_diagnose_gates_for_saas_product():
    text = (
        "Zap Cloud is a SaaS product for marketing automation. "
        "Book a demo of our platform and see pricing plans for every team."
    )
    report = diagnose_gates(text, evidence=FETCHED, lane_name="partner")
    assert report.kind.outcome == "fail"
    assert report.kind.verdict_kind == "software"
    assert "pricing plans" in report.kind.hard_software_matches
    assert report.overall_verdict == "eliminated"


def test_diagnose_gates_for_directory():
    text = "Directory of top IT companies. Search for systems integrators by location. Claim this profile."
    report = diagnose_gates(text, evidence=FETCHED, lane_name="partner")
    assert report.kind.outcome == "fail"
    assert report.kind.verdict_kind == "directory"
    assert report.overall_verdict == "eliminated"


def test_simulate_candidate_advancement():
    sim = simulate_candidate(
        candidate="royalcyber.com",
        snippet="Royal Cyber is an IT consulting firm of 300 in Chicago.",
        page_text="We deliver cloud migrations for our clients. Certified systems integrator.",
        lane_name="partner",
    )
    assert sim.standing is True
    assert len(sim.steps) >= 2
    assert sim.steps[0].rung == "result"
    assert sim.steps[0].advances is True
    assert sim.steps[1].rung == "surface"
    assert sim.final_verdict in ("qualified", "lead")

    rendered = format_sim_report(sim)
    assert "Funnel Simulation" in rendered
    assert "royalcyber.com" in rendered


def test_simulate_candidate_elimination():
    sim = simulate_candidate(
        candidate="badvendor.io",
        snippet="Our platform is a SaaS tool with pricing plans per seat. Book a demo.",
        lane_name="partner",
    )
    assert sim.standing is False
    assert sim.final_verdict == "eliminated"
    assert sim.steps[0].outcome == "eliminated"
    assert sim.steps[0].advances is False


def test_evaluate_surface_url_classification():
    url_surf = evaluate_surface("https://slalom.com/case-studies/finance", lane_name="partner")
    assert url_surf.is_domain is False
    assert url_surf.classified_surface == "case_studies"

    rendered = format_surface_report(url_surf)
    assert "case_studies" in rendered


def test_evaluate_surface_domain_fallback_paths():
    domain_surf = evaluate_surface("slalom.com", lane_name="partner")
    assert domain_surf.is_domain is True
    assert "services" in domain_surf.missing_surfaces
    assert "https://slalom.com/services" in domain_surf.fallback_urls["services"]

    rendered = format_surface_report(domain_surf)
    assert "Domain Surface Evaluation" in rendered
    assert "slalom.com" in rendered


def test_synthetic_benchmark_suite():
    res = run_benchmark()
    # The corpus carries the firms the live audit measured, not ten hand-written
    # items: a suite that cannot fail is not a regression signal.
    assert res.total == len(BENCHMARK_CORPUS) >= 17
    assert res.accuracy == 1.0
    assert res.precision == 1.0
    assert res.recall == 1.0
    assert res.matrix["TP"] == 7
    assert res.matrix["TN"] == 10
    assert res.matrix["FP"] == 0
    assert res.matrix["FN"] == 0
    assert not res.failures

    rendered = format_benchmark_result(res)
    assert "Synthetic Gate Benchmark" in rendered
    assert "Accuracy    : 100.0%" in rendered


def test_audit_database_on_live_probe_db():
    probe_db = Path("/tmp/live-probe/fleet.db")
    if not probe_db.exists():
        pytest.skip("/tmp/live-probe/fleet.db does not exist")
    aud = audit_database(probe_db)
    assert "lanes" in aud
    assert "partner" in aud["lanes"]
    nodes = aud["lanes"]["partner"]["nodes"]
    assert any(n["node"] == "g0-result" for n in nodes)


def test_tools_lane_tune_cli_main(capsys):
    ret = lane_tune_main(["benchmark"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Synthetic Gate Benchmark" in captured.out

    ret_gate = lane_tune_main([
        "gate", "--text", "Thoughtworks is a technology consultancy in London.", "--evidence", "fetched", "--json"
    ])
    assert ret_gate == 0
    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert data["kind"]["outcome"] == "pass"


def test_cli_tune_subcommand(capsys):
    from harness_fleet.cli import main as cli_main
    with pytest.raises(SystemExit) as exc:
        cli_main()
    # verify build_parser includes tune
    from harness_fleet.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["tune", "benchmark"])
    assert args.command == "tune"
    assert args.tune_command == "benchmark"


def test_simulate_candidate_multi_rung_partner():
    """Verify partner lane simulates all 3 rungs (result -> surface -> stories)."""
    sim = simulate_candidate(
        candidate="royalcyber.com",
        snippet="Royal Cyber is an IT consulting firm of 300 in Chicago.",
        page_text="We deliver cloud migrations for our clients. Certified systems integrator.",
        stories_text="Banking client case study: Kafka data pipeline implementation.",
        lane_name="partner",
    )
    assert sim.standing is True
    assert len(sim.steps) == 3
    assert sim.steps[0].rung == "result"
    assert sim.steps[0].evidence == "snippet"
    assert sim.steps[0].advances is True
    assert sim.steps[1].rung == "surface"
    assert sim.steps[1].evidence == "fetched"
    assert sim.steps[1].advances is True
    assert sim.steps[2].rung == "stories"
    assert sim.steps[2].evidence == "fetched"
    assert sim.steps[2].advances is False
    assert sim.final_verdict == "qualified"

    rendered = format_sim_report(sim)
    assert "Step 1 [Rung: result" in rendered
    assert "Step 2 [Rung: surface" in rendered
    assert "Step 3 [Rung: stories" in rendered


def test_simulate_candidate_career_lane():
    """Verify career lane simulates its 2 rungs (result -> posting)."""
    sim = simulate_candidate(
        candidate="techcorp.com",
        snippet="Senior Cloud Engineer opening in London.",
        page_text="Consulting and advisory services delivering cloud migrations for enterprise clients.",
        lane_name="career",
    )
    assert len(sim.steps) == 2
    assert sim.steps[0].rung == "result"
    assert sim.steps[1].rung == "posting"


def test_diagnose_gates_batch():
    candidates = [
        {"entity": "thoughtworks", "text": "Thoughtworks is a software development consultancy in London."},
        {"entity": "saas_tool", "text": "Platform as a service. Free trial, pricing plans per seat. Sign up."},
        {"entity": "biz_dir", "text": "Directory of certified IT companies. Claim your profile."},
    ]
    report = diagnose_gates_batch(candidates, evidence=FETCHED, lane_name="partner")
    assert report.total == 3
    assert report.qualified + report.lead == 1
    assert report.eliminated == 2
    assert round(report.pass_rate, 2) == 0.33

    rendered = format_batch_gate_report(report)
    assert "Batch Gate Diagnostic Report" in rendered
    assert "thoughtworks" in rendered
    assert "saas_tool" in rendered


def test_load_batch_file_and_corpus(tmp_path):
    jsonl_path = tmp_path / "candidates.jsonl"
    jsonl_path.write_text(
        '{"entity": "a", "text": "IT consultancy in London."}\n'
        '{"entity": "b", "text": "SaaS tool with pricing plans."}\n'
    )
    items = load_batch_file(jsonl_path)
    assert len(items) == 2
    assert items[0]["entity"] == "a"

    bench_path = tmp_path / "custom_bench.jsonl"
    bench_path.write_text(
        '{"entity": "good_consultant", "expected": "services", "text": "IT consulting and software engineering firm in London."}\n'
        '{"entity": "bad_saas", "expected": "software", "text": "SaaS app with free trial and pricing plans."}\n'
    )
    bench_res = run_benchmark(corpus_path=bench_path)
    assert bench_res.total == 2
    assert bench_res.accuracy == 1.0
    assert bench_res.matrix["TP"] == 1
    assert bench_res.matrix["TN"] == 1


def test_format_audit_report():
    sample_aud = {
        "database": "/tmp/test.db",
        "exists": True,
        "runs": 1,
        "rung_rows": 28,
        "unique_entities": 14,
        "lanes": {
            "partner": {
                "total_scored": 14,
                "mean_score": 75.0,
                "nodes": [
                    {
                        "node": "g0-result",
                        "rows": 14,
                        "advanced": 14,
                        "eliminated": 0,
                        "outcomes": {"lead": 14},
                        "surfaces": {},
                        "scores_recorded": 0,
                    },
                    {
                        "node": "r1-surface",
                        "rows": 14,
                        "advanced": 14,
                        "eliminated": 0,
                        "outcomes": {"qualified": 14},
                        "surfaces": {"home": 14, "about": 14},
                        "scores_recorded": 14,
                    },
                ],
            }
        },
    }
    rendered = format_audit_report(sample_aud)
    assert "=== Run Database Audit: /tmp/test.db ===" in rendered
    assert "PARTNER" in rendered
    assert "g0-result" in rendered
    assert "Surface Yields:" in rendered
    assert "home" in rendered

    empty_aud = {"database": "/nonexistent.db", "exists": False}
    assert "does not exist" in format_audit_report(empty_aud)


def test_cli_lane_tune_and_lane_eval(capsys):
    from harness_fleet.cli import build_parser, cmd_lane
    parser = build_parser()

    # Verify lane tune benchmark parses
    args_tune = parser.parse_args(["lane", "tune", "benchmark"])
    assert args_tune.command == "lane"
    assert args_tune.lane_command == "tune"
    assert args_tune.tune_command == "benchmark"

    # Verify lane eval sim parses
    args_eval = parser.parse_args(["lane", "eval", "sim", "--candidate", "acme.com"])
    assert args_eval.command == "lane"
    assert args_eval.lane_command == "eval"
    assert args_eval.tune_command == "sim"
    assert args_eval.candidate == "acme.com"

    # Execute lane tune benchmark
    cmd_lane(args_tune)
    out = capsys.readouterr().out
    assert "Synthetic Gate Benchmark" in out
    assert "Accuracy    : 100.0%" in out



def test_audit_database_output_feeds_the_text_report(tmp_path):
    """The report is only reachable if the audit produces what it reads.

    The formatter's contract was exercised against a hand-written dict, so it
    passed while the real path reported "no records" for every database —
    including ones full of rows. This asserts the round trip instead.
    """
    import sqlite3

    db = tmp_path / "audit.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE rung_rows (
            dag_id TEXT, node_id TEXT, lane TEXT, rung TEXT, item_id TEXT,
            outcome TEXT, advances INTEGER, score REAL, surfaces TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO rung_rows VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("d1", "g0-result", "partner", "result", "a.com", "lead", 1, 0, "{}"),
            ("d1", "g1-surface", "partner", "surface", "a.com", "eliminated", 0, 0, "{}"),
            ("d1", "r1-surface", "partner", "surface", "b.com", "read", 1, 0, '{"home": 2}'),
        ],
    )
    con.commit()
    con.close()

    aud = audit_database(db)
    assert aud["exists"] is True
    assert aud["rung_rows"] == 3
    assert aud["unique_entities"] == 2
    assert aud["runs"] == 1  # counted from dag ids when there is no runs table

    rendered = format_audit_report(aud)
    assert "does not exist" not in rendered
    assert "PARTNER" in rendered
    assert "g1-surface" in rendered
    assert "Unique Entities: 2" in rendered


def test_the_simulator_uses_the_lanes_own_gates():
    """A lane that asks no kind question must not be asked the partner's.

    `simulate_candidate` handed the gate engine `lane.model_dump()`, which nests
    the gate fields under `funnel`; `allows` was therefore never read, every lane
    defaulted to `services`, and a career posting was eliminated for "nothing
    reads as a delivery firm" in a lane that asks nothing of the kind.
    """
    product_text = "Our platform. Book a demo. Free trial and pricing plans, per seat."
    for lane_name in ("career", "account"):
        sim = simulate_candidate(
            candidate="vendor.example",
            snippet=product_text,
            page_text=product_text,
            lane_name=lane_name,
        )
        kind_failures = [
            gate
            for step in sim.steps
            for gate in step.gates
            if gate.get("gate") == "kind" and gate.get("outcome") == "fail"
        ]
        assert not kind_failures, f"{lane_name} asks no kind question, so it cannot fail one"
        assert sim.standing is True

    # The partner lane *does* ask it, and must still eliminate the vendor.
    partner_sim = simulate_candidate(
        candidate="vendor.example",
        snippet=product_text,
        page_text=product_text,
        lane_name="partner",
    )
    assert partner_sim.final_verdict == "eliminated"


def test_a_captured_items_file_is_keyed_on_the_entity_not_a_page_slug(tmp_path):
    """A discovery file's item id is a page slug; the checklist is about a firm.

    Live, this was worth ten of eleven candidates. The id was used as the entity,
    so the walk requested `https://acemq.com-apache-kafka-consulting-_-services/
    services` — a host that does not exist — and five rungs all reported
    "nothing". The ladder was reading almost no site and looked merely strict.
    """
    from harness_fleet.dag import carried_from_items

    items = tmp_path / "captured.jsonl"
    items.write_text(
        "\n".join(
            [
                json.dumps({
                    "item_id": "acemq.com-Apache-Kafka-Consulting-_-Services-_-Kafka-Experts",
                    "text": "Kafka consulting. Our clients include banks.",
                    "source_uri": "https://acemq.com/kafka/",
                }),
                # A second page of the same firm is the same candidate.
                json.dumps({
                    "item_id": "acemq.com-Kafka-Experts-Contact",
                    "text": "Contact us for Kafka consulting.",
                    "source_uri": "https://acemq.com/contact",
                }),
                json.dumps({
                    "item_id": "mimacom.com-Apache-Kafka-Consulting-_-Mimacom",
                    "text": "Apache Kafka consulting.",
                    "source_uri": "https://www.mimacom.com/consulting/kafka",
                }),
            ]
        ),
        encoding="utf-8",
    )

    carried = carried_from_items(items, tmp_path)
    assert [c.item_id for c in carried] == ["acemq.com", "mimacom.com"]
    assert [c.candidate for c in carried] == ["acemq.com", "mimacom.com"]
    assert carried[0].source_uri == "https://acemq.com/kafka/"
    # No candidate is a host that cannot resolve.
    assert all(" " not in c.item_id and "-_-" not in c.item_id for c in carried)


def _seed_run_db(path, rows):
    """A run database with the columns an audit reads, and nothing else."""
    import sqlite3

    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE rung_rows (
            dag_id TEXT, node_id TEXT, lane TEXT, rung TEXT, item_id TEXT,
            outcome TEXT, advances INTEGER, score REAL, surfaces TEXT,
            gates TEXT, skipped TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO rung_rows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def test_tune_audit_cli_names_why_candidates_died(tmp_path, capsys):
    """The CLI must answer "why", not just "how many".

    A count of eliminations says a gate is busy. Whether it is *right* is only in
    the reasons, and whether a source is dead or merely empty is only in the skip
    kinds — both of which a reader previously had to pull out of the raw rows.
    """
    from harness_fleet.cli import build_parser, cmd_tune

    db = _seed_run_db(
        tmp_path / "run.db",
        [
            ("d1", "g0-result", "partner", "result", "a.com", "eliminated", 0, 0.0, "{}",
             json.dumps([{"gate": "kind", "outcome": "fail",
                          "reason": "reads as a directory of companies"}]), "[]"),
            ("d1", "r1-surface", "partner", "surface", "b.com", "nothing", 0, 0.0, "{}", "[]",
             json.dumps([{"surface": "about", "reason": "HTTP 404 for https://b.com/about"},
                         {"surface": "about", "reason": "visited, and nothing on this surface was about the entity"}])),
        ],
    )

    args = build_parser().parse_args(["tune", "audit", str(db)])
    cmd_tune(args)
    out = capsys.readouterr().out
    assert "does not exist" not in out
    assert "Why candidates were eliminated" in out
    assert "reads as a directory of companies" in out
    assert "about" in out and "dead path 1" in out and "nothing there 1" in out


def test_tune_sim_cli_reports_declared_and_silent_gates(capsys):
    """A rung that asks a question the profile never makes runnable must say so.

    `stories` declares `vertical`; with no target industries it settles nothing,
    and the report of "the gates that ran" made it look idle rather than unasked.
    """
    from harness_fleet.cli import build_parser, cmd_tune

    args = build_parser().parse_args([
        "tune", "sim", "--lane", "partner", "--candidate", "northwind.example",
        "--page", "We implement Apache Kafka for enterprise clients.",
        "--stories", "Case study: we migrated a bank to Kafka.",
    ])
    cmd_tune(args)
    out = capsys.readouterr().out
    assert "Declared : vertical" in out
    assert "Silent   : vertical" in out
    assert "never runs" in out


def test_tune_compare_cli_shows_the_delta(tmp_path, capsys):
    """Change one thing, look: the delta is the loop, so it is one command."""
    from harness_fleet.cli import build_parser, cmd_tune

    before = _seed_run_db(
        tmp_path / "before.db",
        [("d1", "r1-surface", "partner", "surface", "a.com", "nothing", 11, 0.0, "{}", "[]", "[]")],
    )
    after = _seed_run_db(
        tmp_path / "after.db",
        [("d1", "r1-surface", "partner", "surface", "a.com", "read", 11, 0.0,
          json.dumps({"careers": 18}), "[]", "[]")],
    )

    args = build_parser().parse_args(["tune", "compare", str(before), str(after)])
    cmd_tune(args)
    out = capsys.readouterr().out
    assert "Rung Comparison" in out
    assert "r1-surface" in out
    assert "+18" in out
    assert "careers" in out


def test_the_benchmark_corpus_detects_a_dominance_regression():
    """A corpus that cannot fail is not a signal.

    Ten hand-written items reporting 100% could not detect a regression in the
    dominance rule, which is the rule that decides most of a partner lane's
    population. The corpus now carries the firms the live audit measured, and
    this asserts it actually goes red when the rule is reverted.
    """
    from harness_fleet import gates

    from harness_fleet.tune import BENCHMARK_CORPUS, run_benchmark

    assert len(BENCHMARK_CORPUS) >= 16
    assert run_benchmark().accuracy == 1.0

    def old_precedence(text: str) -> str:
        from harness_fleet.gates import (
            DIRECTORY_TERMS, SERVICES_TERMS, SOFTWARE_TERMS, matches_any,
        )

        if matches_any(text, DIRECTORY_TERMS):
            return "directory"
        if matches_any(text, SOFTWARE_TERMS):
            return "software"
        if matches_any(text, SERVICES_TERMS):
            return "services"
        return ""

    original = gates.read_kind
    gates.read_kind = old_precedence
    try:
        regressed = run_benchmark()
    finally:
        gates.read_kind = original

    assert regressed.accuracy < 1.0, "the corpus must fail when the dominance rule is reverted"
    assert regressed.failures


def test_a_sourceless_record_keeps_its_id_and_an_unattributable_one_is_dropped(tmp_path):
    """The two reasons a record cannot be attributed are not the same fact.

    A record with no URL could not be derived from anything, so the id it
    carries is the best answer. A record *with* a URL that resolves to nobody is
    a job board republishing an employer's posting — and resurrecting its slug is
    how five of seven career candidates stayed boards after the resolver had
    already concluded they were about nobody.
    """
    from harness_fleet.dag import carried_from_items

    items = tmp_path / "captured.jsonl"
    items.write_text(
        "\n".join(
            [
                # No URL: keep the id it came with.
                json.dumps({"item_id": "acme.co.uk", "text": "A data consultancy in London."}),
                # A board with a URL that names no employer: dropped.
                json.dumps({
                    "item_id": "glassdoor.com-Enterprise-Sales-Jobs",
                    "text": "Search jobs. Apply now.",
                    "source_uri": "https://www.glassdoor.com/Job/enterprise-sales-jobs.htm",
                }),
                # A real employer's own posting: kept as the employer.
                json.dumps({
                    "item_id": "pearson.jobs-Enterprise-Sales-Director",
                    "text": "Pearson is hiring an Enterprise Sales Director.",
                    "source_uri": "https://www.pearson.jobs/job/sales-director",
                }),
            ]
        ),
        encoding="utf-8",
    )

    carried = carried_from_items(items, tmp_path)
    assert [c.item_id for c in carried] == ["acme.co.uk", "pearson.jobs"]


def test_a_scored_zero_is_a_score_not_a_missing_measurement(tmp_path):
    """`rung_rows.score` is NOT NULL DEFAULT 0, so truthiness cannot date a row.

    `if row["score"]:` threw every zero away and the audit reported "Scored
    Entities: 0, Mean Score: 0.0" for a run that had scored every candidate and
    found none of them good — the difference between "this lane found nothing"
    and "this lane never looked". A scored row is one a *score node* wrote.
    """
    from harness_fleet.cli import build_parser, cmd_tune

    db = _seed_run_db(
        tmp_path / "zeros.db",
        [
            ("d1", "s-score", "partner", "", "a.com", "", 0, 0.0, "{}", "[]", "[]"),
            ("d1", "s-score", "partner", "", "b.com", "", 0, 0.0, "{}", "[]", "[]"),
            ("d1", "g0-result", "partner", "result", "a.com", "lead", 1, 0.0, "{}", "[]", "[]"),
        ],
    )
    # `_seed_run_db` needs the kind column for the audit to read it.
    import sqlite3

    con = sqlite3.connect(db)
    con.executescript("ALTER TABLE rung_rows ADD COLUMN kind TEXT")
    con.execute("UPDATE rung_rows SET kind='score' WHERE node_id='s-score'")
    con.execute("UPDATE rung_rows SET kind='gate' WHERE node_id='g0-result'")
    con.commit()
    con.close()

    from harness_fleet.tune import audit_database

    aud = audit_database(db)
    partner = aud["lanes"]["partner"]
    assert partner["total_scored"] == 2, "two candidates were scored zero, and that is a result"
    assert partner["mean_score"] == 0.0

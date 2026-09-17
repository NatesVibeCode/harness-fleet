"""Tests for the read-only results board (harness_fleet.board)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from harness_fleet.board import (
    BOARD_HTML_PATH,
    BoardHandler,
    build_board_payload,
    latest_run_id,
    list_runs,
)
from harness_fleet.catalog import PriceState, RouteCatalog
from harness_fleet.engine import Engine
from harness_fleet.models import RoutePolicy
from harness_fleet.store import HarnessStore
from harness_fleet.task import PARTNER_CHECKLIST, create_task_from_preset


def _partner_run(tmp_path: Path, run_id: str = "board-run") -> Path:
    """A real, verified partner run produced by the deterministic demo provider."""
    db = tmp_path / "state.db"
    store = HarnessStore(db)
    # Register the deterministic demo route exactly as `quickstart --demo` does.
    catalog = RouteCatalog(db_path=store.path)
    catalog.add_route(
        route_id="demo/fake",
        provider="demo",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        enabled=True,
        price_state=PriceState.PRICE_OBSERVED_ZERO.value,
        verification_source="board test (deterministic)",
    )
    task = create_task_from_preset("partner_research", preset_name="partner-research")
    store.register_task(task)
    items = [
        {"item_id": "cloud_solutions.io", "text": "Cloud Solutions is a certified Snowflake implementation partner delivering Kafka migrations for fintech clients."},
        {"item_id": "northwind.example", "text": "Northwind resells SaaS licences and offers staff augmentation."},
        {"item_id": "tiny.example", "text": "Two-person design studio."},
    ]
    Engine(
        task=task,
        store=store,
        policy=RoutePolicy(allowed_routes=["demo/fake"], free_only=True),
    ).run_campaign(
        raw_items=items,
        run_id=run_id,
        input_path=str(tmp_path / "partners.csv"),
        concurrency=1,
        max_attempts=5,
        output_packet_path=tmp_path / "packet.json",
    )
    return db


def test_payload_is_schema_driven_and_never_invents_scores(tmp_path):
    db = _partner_run(tmp_path)
    payload = build_board_payload(db)

    assert payload["run"]["run_id"] == "board-run"
    assert payload["stats"]["records"] == 3
    # Labels come from the task's own schema, not a hardcoded partner vocabulary.
    assert {c["item_id"] for c in payload["checklist"]} == set(PARTNER_CHECKLIST)
    assert all(c["label"] and c["points"] > 0 for c in payload["checklist"])
    assert payload["checklist_total"] == 100
    assert {a["key"] for a in payload["attributes"]} >= {"target_stack", "service_model", "vendor_alliances"}

    for partner in payload["partners"]:
        score = partner["score"]
        # A score is either a real number or None; never a fabricated 0.
        assert score is None or (isinstance(score, (int, float)) and 0 <= score <= 100)
        # The score equals the checklist points actually earned.
        assert partner["earned_points"] == sum(
            points for item, points in partner["checklist_points"].items() if partner["checklist"][item]
        )
        if score is not None:
            # A score is the earned points *scaled by how well each claim is
            # supported*: a quote that does not address its claim pays nothing,
            # so the score can never exceed what was earned.
            assert score <= partner["earned_points"] + 1e-6
            if partner["earned_points"]:
                assert score >= 0
        assert partner["provenance"]["route"] == "demo/fake"
        assert partner["provenance"]["cost"] == 0.0


def test_nested_facet_values_read_as_text_not_json():
    from harness_fleet.board import _counter

    counts = _counter([
        {"min_project_size": "", "hourly_rate": "", "employees": "100+ engineers"},
        {"min_project_size": "", "hourly_rate": "", "employees": ""},
        {"min_project_size": "", "hourly_rate": "", "employees": "100+ engineers"},
    ])
    # Stated parts only, no raw JSON, and an all-blank object is not a value.
    assert counts == {"employees: 100+ engineers": 2}


def test_counter_keeps_plain_values_and_flags_booleans():
    from harness_fleet.board import _counter

    # A list value collapses to its stated parts, so it counts with the scalar.
    assert _counter(["Kafka", "Kafka", "", None, ["Kafka"]]) == {"Kafka": 3}
    assert _counter([[], "", None]) == {}
    assert _counter([True, False, True]) == {"yes": 2, "no": 1}


def test_payload_reports_quotes_and_provenance(tmp_path):
    db = _partner_run(tmp_path)
    payload = build_board_payload(db)
    with_quotes = [p for p in payload["partners"] if p["quotes"]]
    assert with_quotes, "the demo provider always returns quotes"
    quote = with_quotes[0]["quotes"][0]
    assert isinstance(quote["text"], str) and quote["text"]
    assert isinstance(quote["supports"], list)
    assert payload["stats"]["quote_count"] == sum(len(p["quotes"]) for p in payload["partners"])
    assert payload["stats"]["routes"] == ["demo/fake"]
    assert payload["stats"]["cost_reported"] == 0.0


def test_unknown_run_and_empty_database_are_explained(tmp_path):
    db = _partner_run(tmp_path)
    with pytest.raises(KeyError):
        build_board_payload(db, "nope")
    empty = HarnessStore(tmp_path / "empty.db")
    with pytest.raises(ValueError, match="no runs yet"):
        latest_run_id(empty)
    assert list_runs(HarnessStore(db))[0]["run_id"] == "board-run"


def test_board_page_ships_with_the_package():
    assert BOARD_HTML_PATH.is_file()
    html = BOARD_HTML_PATH.read_text(encoding="utf-8")
    assert "read-only" in html.lower() or "view only" in html.lower()
    # The page must not imply it writes anything.
    for verb in ("fetch(\"/api/crm/record\"", "method: \"POST\""):
        assert verb not in html


def test_http_endpoints_and_host_guard(tmp_path):
    db = _partner_run(tmp_path)
    BoardHandler.db_path = db
    BoardHandler.run_id = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), BoardHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=20) as response:
            assert response.status == 200
            assert "text/html" in response.headers.get("Content-Type", "")
            assert b"board" in response.read().lower()

        with urlopen(f"http://127.0.0.1:{port}/api/board", timeout=20) as response:
            payload = json.loads(response.read())
        assert payload["run"]["run_id"] == "board-run"
        assert len(payload["partners"]) == 3

        with urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=20) as response:
            runs = json.loads(response.read())
        assert runs["runs"][0]["run_id"] == "board-run"
        assert runs["current"] == "board-run"

        # The page switches runs with ?run=, so the API has to honour it.
        with urlopen(f"http://127.0.0.1:{port}/api/board?run=board-run", timeout=20) as response:
            assert json.loads(response.read())["run"]["run_id"] == "board-run"
        with pytest.raises(urllib.error.HTTPError) as unknown_run:
            urlopen(f"http://127.0.0.1:{port}/api/board?run=does-not-exist", timeout=20)
        assert unknown_run.value.code == 404
        with urlopen(f"http://127.0.0.1:{port}/api/runs?run=does-not-exist", timeout=20) as response:
            assert json.loads(response.read())["current"] == "does-not-exist"

        with pytest.raises(urllib.error.HTTPError) as missing:
            urlopen(f"http://127.0.0.1:{port}/api/nope", timeout=20)
        assert missing.value.code == 404

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/board", headers={"Host": "evil.example"}
        )
        with pytest.raises(urllib.error.HTTPError) as blocked:
            urlopen(request, timeout=20)
        assert blocked.value.code == 403

        # Writes are not part of this surface at all.
        for path in ("/api/crm/record", "/api/board"):
            post = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(urllib.error.HTTPError) as not_allowed:
                urlopen(post, timeout=20)
            assert not_allowed.value.code == 501
    finally:
        server.shutdown()
        server.server_close()


def test_cli_prints_the_payload_with_json(tmp_path, capsys):
    from harness_fleet import cli

    db = _partner_run(tmp_path)
    cli.cmd_board(type("A", (), {"db": str(db), "run_id": "board-run", "json": True})())
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["run_id"] == "board-run"
    assert payload["stats"]["records"] == 3


def test_the_board_shows_the_evidence_read_next_to_the_score(tmp_path):
    """The run's evidence readout is rendered with the score it qualifies."""
    db = _partner_run(tmp_path, run_id="evidence-run")
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "evidence-run"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence.json").write_text(json.dumps({
        "run_id": "evidence-run",
        "items": {
            "cloud_solutions.io": {
                "kinds": {"delivery_proof": True, "independent_validation": False},
                "tier_claimed": "tier_1",
                "tier_supported": "tier_2",
                "tier_capped": True,
                "tier_reasons": ["tier_1 needs independent_validation"],
                "contradictions": [{"kind": "certification_unverified", "claim": "c", "counterpart": "n", "uris": []}],
            }
        },
        "kind_totals": {"delivery_proof": 1, "independent_validation": 0},
        "tier_capped": {"cloud_solutions.io": ["tier_1 needs independent_validation"]},
        "contradictions": {"cloud_solutions.io": [{"kind": "certification_unverified"}]},
    }), encoding="utf-8")

    payload = build_board_payload(db, "evidence-run", runs_dir)
    by_id = {p["id"]: p for p in payload["partners"]}
    assert by_id["cloud_solutions.io"]["evidence"]["tier_supported"] == "tier_2"
    assert payload["stats"]["tier_capped"] == 1
    assert payload["stats"]["lone_claims"] == 1
    assert payload["stats"]["evidence_kinds"]["delivery_proof"] == 1
    # A partner with no readout still renders, with an empty evidence block.
    assert by_id["northwind.example"]["evidence"] == {}


def test_a_run_without_a_readout_still_renders(tmp_path):
    """Evidence reads are additive: an older run must not break the board."""
    db = _partner_run(tmp_path, run_id="no-evidence-run")
    payload = build_board_payload(db, "no-evidence-run", tmp_path / "runs")
    assert all(p["evidence"] == {} for p in payload["partners"])
    assert payload["stats"]["tier_capped"] == 0
    assert payload["stats"]["evidence_kinds"] == {}


def test_a_corrupt_readout_is_ignored_rather_than_fatal(tmp_path):
    db = _partner_run(tmp_path, run_id="corrupt-run")
    run_dir = tmp_path / "runs" / "corrupt-run"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence.json").write_text("{not json", encoding="utf-8")
    payload = build_board_payload(db, "corrupt-run", tmp_path / "runs")
    assert payload["stats"]["records"] == 3
    assert all(p["evidence"] == {} for p in payload["partners"])


def test_a_quote_that_does_not_address_its_claim_pays_nothing(tmp_path):
    """The demo provider's canned quote is generic, so its points are not paid.

    This is the central claim contract in action: an answer only scores when a
    supporting quote actually states what the item claims.
    """
    from harness_fleet.task import PARTNER_CHECKLIST

    db = _partner_run(tmp_path, run_id="unsupported-run")
    payload = build_board_payload(db, "unsupported-run")
    scored = [p for p in payload["partners"] if p["score"] is not None]
    assert scored, "the demo provider still produces scored records"
    assert any(p["score"] < p["earned_points"] for p in scored), (
        "a generic quote must not pay the full checklist points"
    )
    assert set(payload["checklist"][0]) == {"item_id", "label", "points", "description", "passed"}
    assert PARTNER_CHECKLIST, "the checklist contract is unchanged"


def test_the_board_carries_the_running_list(tmp_path):
    """The run detail answers "what happened"; the list answers "who do we know"."""
    from harness_fleet.board import build_board_payload, ledger_view
    from harness_fleet.ledger import Ledger
    from harness_fleet.store import HarnessStore

    db = _partner_run(tmp_path, "board-ledger")
    store = HarnessStore(db)
    Ledger(store).record(
        [{"item_id": "acme.co.uk", "candidate": "acme.co.uk", "outcome": "qualified",
          "because": "", "gates": [], "score": 82.0, "tier": "tier_2",
          "facts": {"service_model": "SI"}, "run_id": "run-1"}],
        dag_id="run-1", node_id="s-score", lane="partner",
    )
    payload = build_board_payload(db)
    ledger = payload["ledger"]
    assert ledger["counts"]["entities"] == 1
    assert ledger["entities"][0]["entity"] == "acme.co.uk"
    assert ledger["entities"][0]["score"] == 82.0
    assert ledger["entities"][0]["facts"]["service_model"] == "SI"
    # And on its own, for a page that wants the list without a run's payload.
    assert ledger_view(store)["counts"]["entities"] == 1


def test_the_ledger_route_answers_on_its_own(tmp_path):
    """A page can poll the running list without rebuilding a run's payload."""
    from harness_fleet.ledger import Ledger

    db = _partner_run(tmp_path, "board-route")
    Ledger(HarnessStore(db)).record(
        [{"item_id": "acme.co.uk", "candidate": "acme.co.uk", "outcome": "qualified",
          "gates": [], "score": 70.0}],
        dag_id="board-route", node_id="s-score",
    )
    BoardHandler.db_path = db
    BoardHandler.run_id = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), BoardHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ledger", timeout=20) as response:
            body = json.loads(response.read())
        assert body["counts"]["entities"] == 1
        assert body["entities"][0]["entity"] == "acme.co.uk"
        assert body["movers"] == [], "one number is not a movement"
    finally:
        server.shutdown()
        server.server_close()


def test_the_board_separates_the_band_from_the_tier_evidence_supports(tmp_path):
    """Two things called a tier: where the score falls, and what was proved."""
    import json as _json
    from pathlib import Path

    from harness_fleet.board import build_board_payload

    db = _partner_run(tmp_path, "board-tiers")
    runs_dir = tmp_path / "runs" / "board-tiers"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "evidence.json").write_text(_json.dumps({
        "items": {
            "cloud_solutions.io": {
                "tier_claimed": "tier_1",
                "tier_supported": "tier_3",
                "tier_capped": True,
                "tier_reasons": ["stack_delivery missing"],
            }
        }
    }), encoding="utf-8")

    payload = build_board_payload(db, runs_dir=tmp_path / "runs")
    row = next(r for r in payload["partners"] if r["id"] == "cloud_solutions.io")

    # The band comes from the row's own claim; the supported tier comes from the
    # run's evidence readout. They are independent facts and both are exposed.
    assert row["tier"] == row["claims"].get("fit_tier")
    assert row["tier"] != row["tier_supported"]
    assert row["tier_supported"] == "tier_3", "and the evidence's answer travels beside it"
    assert row["tier_capped"] is True
    assert row["evidence"]["tier_reasons"] == ["stack_delivery missing"]
    assert Path(runs_dir / "evidence.json").is_file()


def test_the_board_and_the_ledger_agree_about_a_scored_run(tmp_path):
    """The map's last unguarded pairing: two readers of one scored run.

    The board reads the run's evidence readout and its packet; the running list
    reads the scoring node's rows. If those disagree, one of them is describing
    a run that did not happen.
    """
    from harness_fleet.dag import DagSpec, run_dag
    from harness_fleet.lanes import shipped_lanes
    from harness_fleet.ledger import Ledger
    from harness_fleet.models import RoutePolicy
    from harness_fleet.rungs import RungTables, lane_spec

    db = _partner_run(tmp_path, "agree-source")
    store = HarnessStore(db)
    lane = shipped_lanes()["partner"]
    spec = DagSpec.model_validate(lane_spec(
        lane, from_run="agree-source", workspace=tmp_path,
        score=True, score_task="partner-research", score_run_id="agree-scored",
        score_policy=RoutePolicy(allowed_routes=["demo/fake"], free_only=True),
    ))
    state = run_dag(spec, store, workspace_root=tmp_path, dag_id="judged")
    assert state["nodes"]["s-score"]["verified"] > 0

    # The scoring node wrote the readout every reader of a run looks for.
    readout_path = tmp_path / "runs" / "agree-scored" / "evidence.json"
    assert readout_path.is_file(), "a scored run has an evidence readout"
    readout = json.loads(readout_path.read_text(encoding="utf-8"))
    node_rows = {row["item_id"]: row for row in RungTables(store).rows("judged", "s-score")}
    listed = {row["entity"]: row for row in Ledger(store).entities() if row.get("scored_at")}

    payload = build_board_payload(db, "agree-scored", runs_dir=tmp_path / "runs")
    board = {row["id"]: row for row in payload["partners"]}

    # Every firm the node wrote a row for, whatever it scored: the invariant is
    # that the three readers agree, not that the number is large.
    assert node_rows, "the node wrote rows"
    for item in node_rows:
        assert board[item]["score"] == node_rows[item]["score"], (
            f"{item}: the board and the node disagree about the score"
        )
        assert listed[item]["score"] == node_rows[item]["score"]
        assert listed[item]["tier"] == node_rows[item]["tier"]
        # And the readout carries the supported tier for the same firm.
        assert readout["items"][item]["tier_supported"] == node_rows[item]["tier"] or not node_rows[item]["tier"]
        assert board[item]["tier_supported"] == readout["items"][item]["tier_supported"]


def test_a_reader_skips_a_record_the_rules_no_longer_accept(tmp_path):
    """One stale record must not hide a hundred good ones.

    A stored run whose score disagrees with its checklist — written before the
    score was derived from the answer — made the board refuse to open at all,
    which is the wrong trade in the wrong direction: strictness belongs to the
    deliverable, and a reader should count what it left out.
    """
    from harness_fleet.export import verified_records_from_snapshot
    from harness_fleet.models import TaskSpec

    task = TaskSpec(
        name="tiny",
        instructions="answer the checklist",
        checklist={"q1": 10, "q2": 20},
        claims_schema={
            "type": "object",
            "properties": {"checklist": {"type": "object"}, "score": {"type": "number"}},
            "required": ["checklist"],
            "additionalProperties": False,
        },
    )
    record = {
        "item_id": "acme.example",
        "content_type": "text/plain",
        "source_digest": "a" * 64,
        # The checklist says 10; the stored record claims 75.
        "claims": {"checklist": {"q1": True, "q2": False}, "score": 75},
        "quotes": [{"slice_id": "s", "start": 0, "end": 4, "text": "Acme", "supports": ["q1"]}],
    }
    def _snapshot():
        return {
            "task": task.model_dump(mode="json", by_alias=True),
            "batches": {"b1": {"status": "verified", "result": [record]}},
        }

    with pytest.raises(ValueError, match="inconsistent with the checklist"):
        verified_records_from_snapshot(_snapshot())

    reading = _snapshot()
    records, _task = verified_records_from_snapshot(reading, tolerate_rejected=True)
    assert records == [], "the record it cannot vouch for is left out"
    assert reading["records_rejected"] == 1, "and counted, so a reader can say so"


def test_the_board_names_a_lane_by_its_lane_not_its_preset(tmp_path):
    """A lane and a preset are independent, and the noun has to come from the lane.

    The board inferred it from the task's name, which worked for partners by
    luck ("partner-research" starts with "partner") and called a career run's
    roles "Records" — because the career lane's preset is `triage`. The visit
    log records the lane a node ran for, which is the fact itself.
    """
    import sqlite3

    from harness_fleet.board import lane_for_run
    from harness_fleet.ledger import Ledger
    from harness_fleet.store import HarnessStore

    db = tmp_path / "t.db"
    store = HarnessStore(db)
    Ledger(store).record(
        [{"item_id": "acme.com", "candidate": "acme.com", "outcome": "read", "gates": [],
          "score": 0.0, "run_id": "run-career"}],
        dag_id="ui-career", node_id="s-score", lane="career",
    )
    assert lane_for_run(store, "run-career") == "career"
    assert lane_for_run(store, "no-such-run") == "", "and an unknown run says nothing"

    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE entity_events")
    assert lane_for_run(store, "run-career") == "", (
        "a database without the log is a database without a lane, not an error"
    )

"""The running list, and the attempts that made it.

Two questions, two shapes. A rung table answers "what happened in this run, at
this node", and it is now versioned because re-running a rung is how a lane gets
tuned — the earlier answer has to still be there to compare against. The running
list answers "who have we looked at and where did each one stop", and it has to
survive the run, because the point of running again is to learn more about the
same firms.
"""

import json

from harness_fleet.ledger import Ledger
from harness_fleet.rungs import RungTables, read_csv
from harness_fleet.store import HarnessStore


def _row(entity: str, outcome: str, **extra):
    row = {
        "item_id": entity,
        "candidate": entity,
        "rung": "result",
        "evidence": "snippet",
        "outcome": outcome,
        "because": extra.pop("because", ""),
        "gates": extra.pop("gates", []),
        "surfaces": {},
        "pages": extra.pop("pages", []),
        "source_uri": "",
        "advances": extra.pop("advances", True),
    }
    row.update(extra)
    return row


def _store(tmp_path):
    return HarnessStore(tmp_path / "t.db")


# --------------------------------------------------------------------------
# Attempts: a rung table is never overwritten


def test_rerunning_a_rung_keeps_the_earlier_answer(tmp_path):
    store = _store(tmp_path)
    tables = RungTables(store)
    first = tables.write("dag", "g0-result", [_row("vendor.io", "eliminated")],
                         texts={"vendor.io": "a platform"})
    # The lane is tuned, and the same rung runs again.
    second = tables.write("dag", "g0-result", [_row("vendor.io", "lead")])

    assert (first, second) == (1, 2)
    assert tables.attempts("dag", "g0-result") == [1, 2]
    # Reading gets the current answer; the old one is still addressable.
    assert tables.rows("dag", "g0-result")[0]["outcome"] == "lead"
    assert tables.rows("dag", "g0-result", 1)[0]["outcome"] == "eliminated"
    assert tables.text("dag", "g0-result", "vendor.io", 1) == "a platform"
    assert tables.export_csv("dag", "g0-result", tmp_path / "old.csv", 1) == 1
    assert read_csv(tmp_path / "old.csv")[0]["outcome"] == "eliminated"


# --------------------------------------------------------------------------
# The running list


def test_the_running_list_survives_the_run_and_accumulates(tmp_path):
    store = _store(tmp_path)
    ledger = Ledger(store)
    ledger.record(
        [_row("acme.co.uk", "lead"), _row("vendor.io", "eliminated",
                                          because="kind: a product vendor")],
        dag_id="run-1", node_id="g0-result", lane="partner", at="2026-01-01T00:00:00+00:00",
    )
    # A second run sees the same firm again, and one that is new.
    ledger.record(
        [_row("acme.co.uk", "qualified"), _row("newfirm.example", "lead")],
        dag_id="run-2", node_id="g1-surface", lane="partner",
        at="2026-02-01T00:00:00+00:00",
    )

    listed = {row["entity"]: row for row in ledger.entities()}
    assert set(listed) == {"acme.co.uk", "vendor.io", "newfirm.example"}
    assert listed["acme.co.uk"]["sightings"] == 2
    assert listed["acme.co.uk"]["outcome"] == "qualified", "the newest belief"
    assert listed["acme.co.uk"]["first_seen"].startswith("2026-01-01")
    assert listed["acme.co.uk"]["last_dag"] == "run-2"
    assert listed["vendor.io"]["outcome"] == "eliminated"

    counts = ledger.counts()
    assert counts["entities"] == 3 and counts["lead"] == 1 and counts["qualified"] == 1
    assert {row["entity"] for row in ledger.entities(standing_only=True)} == {
        "newfirm.example", "acme.co.uk",
    }, "the list is ordered by when each was last seen, so it is the set that matters"


def test_an_elimination_is_not_quietly_undone_by_a_later_rung(tmp_path):
    """Worst news stands: a rung that never saw the entity cannot revive it."""
    ledger = Ledger(_store(tmp_path))
    ledger.record([_row("vendor.io", "eliminated", because="kind")],
                  dag_id="d", node_id="g0-result")
    ledger.record([_row("vendor.io", "")], dag_id="d", node_id="x0-result")

    row = ledger.entities()[0]
    assert row["outcome"] == "eliminated" and row["because"] == "kind"
    assert row["last_node"] == "x0-result", "but we still know when we last saw it"


def test_every_visit_is_logged_even_when_the_state_moves_on(tmp_path):
    ledger = Ledger(_store(tmp_path))
    ledger.record([_row("acme.co.uk", "lead", gates=[{"gate": "kind", "outcome": "unknown"}])],
                  dag_id="run-1", node_id="g0-result", run_seq=1, at="2026-01-01T00:00:00+00:00")
    ledger.record([_row("acme.co.uk", "qualified")],
                  dag_id="run-1", node_id="g1-surface", run_seq=1, at="2026-01-02T00:00:00+00:00")

    events = ledger.history("acme.co.uk")
    assert [event["node_id"] for event in events] == ["g0-result", "g1-surface"]
    assert events[0]["gates_open"] == ["kind"], "and what was open when it was"


def test_the_running_list_is_written_only_when_asked_for(tmp_path):
    ledger = Ledger(_store(tmp_path))
    ledger.record([_row("acme.co.uk", "lead")], dag_id="d", node_id="g")
    path = tmp_path / "ledger.csv"
    assert ledger.export_csv(str(path)) == 1
    assert read_csv(path)[0]["entity"] == "acme.co.uk"


# --------------------------------------------------------------------------
# The command


def test_the_ledger_command_reads_the_running_list(tmp_path, capsys):
    from argparse import Namespace

    from harness_fleet import cli

    store = _store(tmp_path)
    Ledger(store).record([_row("acme.co.uk", "lead"), _row("vendor.io", "eliminated")],
                         dag_id="d", node_id="g0-result", lane="partner")
    cli.cmd_ledger(Namespace(
        db=str(tmp_path / "t.db"), json=True, limit=10, outcome=None, lane=None,
        standing=False, entity=None, csv=None,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["entities"] == 2
    assert {row["entity"] for row in payload["entities"]} == {"acme.co.uk", "vendor.io"}


def test_the_ledger_command_answers_for_one_entity(tmp_path, capsys):
    from argparse import Namespace

    from harness_fleet import cli

    store = _store(tmp_path)
    Ledger(store).record([_row("acme.co.uk", "lead")], dag_id="run-1", node_id="g0-result")
    cli.cmd_ledger(Namespace(
        db=str(tmp_path / "t.db"), json=True, limit=10, outcome=None, lane=None,
        standing=False, entity="acme.co.uk", csv=None,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert payload["entity"] == "acme.co.uk"
    assert payload["events"][0]["dag_id"] == "run-1"


# --------------------------------------------------------------------------
# The score node writes both halves of the answer


def test_a_score_node_records_the_number_and_the_facts(tmp_path, monkeypatch):
    """The stage that produces the deliverable is a node, and it feeds the list."""
    from harness_fleet.dag import DagSpec, run_dag
    from harness_fleet.gates import LadderRung
    from harness_fleet.lanes import shipped_lanes
    from harness_fleet.rungs import lane_spec

    lane = shipped_lanes()["partner"]
    lane.funnel.ladder = [LadderRung(name="result", evidence="snippet", gates=["kind"])]
    captured = tmp_path / "captured.jsonl"
    captured.write_text(
        json.dumps({"item_id": "acme.co.uk", "text": "Acme is a systems integrator.",
                    "content_type": "text/plain"}) + "\n",
        encoding="utf-8",
    )
    store = _store(tmp_path)

    def fake_records(snapshot):
        from harness_fleet.models import ExtractedItem

        return [
            ExtractedItem(
                item_id="acme.co.uk",
                source_uri="https://acme.co.uk",
                source_digest="a" * 64,
                content_type="text/plain",
                claims={
                    "score": 87.5,
                    "fit_tier": "tier_2",
                    "reason": "delivers what the lane buys",
                    "answers": {"service_model": "SI", "industry_verticals": "fintech"},
                },
                quotes=[{"slice_id": "s", "start": 0, "end": 4, "text": "Acme"}],
            )
        ], None

    monkeypatch.setattr("harness_fleet.dag.verified_records_from_snapshot", fake_records)
    monkeypatch.setattr(
        "harness_fleet.dag._resolve_dag_task", lambda name, store, root: _FakeTask(name)
    )

    class _FakeEngine:
        def __init__(self, **kwargs):
            pass

        def run_campaign(self, **kwargs):
            return {"total_verified_records": 1}

    monkeypatch.setattr("harness_fleet.dag.Engine", _FakeEngine)
    # The campaign is faked, so the snapshot it would have written is too; the
    # extraction above is what the node actually reads.
    monkeypatch.setattr(
        "harness_fleet.store.HarnessStore.run_snapshot",
        lambda self, run_id: {"run_id": run_id, "task": {}, "batches": {}},
    )
    spec = DagSpec.model_validate(lane_spec(
        lane, from_items=str(captured), workspace=tmp_path,
        score=True, score_task="partner-research", score_run_id="run-1",
    ))
    assert spec.topo_order()[-1] == "s-score"
    state = run_dag(spec, store, workspace_root=tmp_path, dag_id="f1")

    scored = state["nodes"]["s-score"]
    assert scored["run_id"] == "run-1"
    assert scored["verified"] == 1 and scored["mean_score"] == 87.5
    # The node's own table carries the number and the facts.
    row = RungTables(store).rows("f1", "s-score")[0]
    assert row["score"] == 87.5
    assert row["facts"]["service_model"] == "SI"
    assert row["facts"]["industry_verticals"] == "fintech"
    # And so does the running list, which is the point of recording it.
    listed = Ledger(store).entities()[0]
    assert listed["entity"] == "acme.co.uk"
    assert listed["score"] == 87.5
    # The tier is capped at what the gathered pages actually support, not at what
    # the model claimed: one short line of evidence does not carry tier_2.
    assert listed["tier"] == "tier_3"
    assert listed["facts"]["service_model"] == "SI"
    assert listed["scored_at"] and listed["run_id"] == "run-1"
    # Both stages logged: the gate that let it through, then the score.
    events = Ledger(store).history("acme.co.uk")
    assert [event["node_id"] for event in events] == ["g0-result", "s-score"]
    assert events[1]["score"] == 87.5 and events[1]["facts"]["service_model"] == "SI"


class _FakeTask:
    def __init__(self, name: str) -> None:
        self.name = name


def test_a_firm_scored_twice_shows_its_movement(tmp_path):
    """"How good is this firm" is a column; "is it getting better" is a series."""
    ledger = Ledger(_store(tmp_path))
    ledger.record([_row("acme.co.uk", "qualified", score=61.0, facts={"service_model": "SI"})],
                  dag_id="run-1", node_id="s-score", at="2026-01-01T00:00:00+00:00")
    ledger.record([_row("acme.co.uk", "qualified", score=87.5, facts={"service_model": "SI"})],
                  dag_id="run-2", node_id="s-score", at="2026-02-01T00:00:00+00:00")
    ledger.record([_row("other.example", "qualified", score=40.0)],
                  dag_id="run-2", node_id="s-score", at="2026-02-01T00:00:00+00:00")

    history = ledger.scores("acme.co.uk")
    assert [row["score"] for row in history] == [61.0, 87.5]
    assert [row["run_id"] for row in history] == ["run-1", "run-2"]

    movers = ledger.movers()
    assert len(movers) == 1, "only a firm with two numbers has moved"
    assert movers[0]["entity"] == "acme.co.uk"
    assert movers[0]["previous"] == 61.0 and movers[0]["score"] == 87.5
    assert movers[0]["delta"] == 26.5
    # The current belief is still the newest number.
    assert ledger.entities()[0]["score"] == 87.5


def test_the_trend_command_reads_the_movement(tmp_path, capsys):
    from argparse import Namespace

    from harness_fleet import cli

    store = _store(tmp_path)
    ledger = Ledger(store)
    ledger.record([_row("acme.co.uk", "qualified", score=61.0)],
                  dag_id="run-1", node_id="s-score", at="2026-01-01T00:00:00+00:00")
    ledger.record([_row("acme.co.uk", "qualified", score=87.5)],
                  dag_id="run-2", node_id="s-score", at="2026-02-01T00:00:00+00:00")
    cli.cmd_ledger(Namespace(
        db=str(tmp_path / "t.db"), json=True, limit=10, outcome=None, lane=None,
        standing=False, entity=None, csv=None, trend=True,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert payload["movers"][0]["delta"] == 26.5


def test_a_walk_only_run_is_still_scored(tmp_path):
    """Turning the gates off is not turning the run off: someone still judges it."""
    from harness_fleet.dag import DagSpec, run_dag
    from harness_fleet.lanes import shipped_lanes
    from harness_fleet.rungs import lane_spec

    captured = tmp_path / "captured.jsonl"
    captured.write_text(
        json.dumps({"item_id": "acme.co.uk", "text": "Acme is a systems integrator.",
                    "content_type": "text/plain"}) + "\n",
        encoding="utf-8",
    )
    spec = DagSpec.model_validate(lane_spec(
        shipped_lanes()["partner"], from_items=str(captured), workspace=tmp_path,
        gates=False, score=True, score_task="partner-research",
    ))
    assert spec.topo_order() == ["r0-surface", "s-score"]
    # No gate to name, so the score node holds the population itself.
    assert spec.nodes[-1].from_gate == ""
    assert spec.nodes[-1].from_nodes == ["r0-surface"]
    assert run_dag is not None


def test_a_walk_does_not_erase_the_verdict_that_decided_the_firm(tmp_path):
    """A visit is not a verdict: 'read' must not overwrite 'lead, open on kind'."""
    ledger = Ledger(_store(tmp_path))
    ledger.record(
        [_row("acme.co.uk", "lead", because="unresolved on kind",
              gates=[{"gate": "kind", "outcome": "unknown"}])],
        dag_id="run-1", node_id="g0-result", lane="partner",
    )
    # The walk goes and reads their site. It reports a visit, not a judgement.
    ledger.record([_row("acme.co.uk", "read", pages=["https://acme.co.uk/about"])],
                  dag_id="run-1", node_id="r1-surface", lane="partner")
    row = ledger.entities()[0]
    assert row["outcome"] == "lead", "the gate's verdict is still where this firm stands"
    assert row["because"] == "unresolved on kind"
    assert row["gates_open"] == ["kind"], "and what is open is still open"
    assert row["last_node"] == "r1-surface", "while the visit is still recorded"
    assert row["pages_seen"] == 1

    # A judgement does move it, and so does a later elimination.
    ledger.record([_row("acme.co.uk", "qualified", gates=[])],
                  dag_id="run-1", node_id="g1-surface", lane="partner")
    assert ledger.entities()[0]["outcome"] == "qualified"
    assert ledger.entities()[0]["gates_open"] == []
    ledger.record([_row("acme.co.uk", "eliminated", because="vertical")],
                  dag_id="run-1", node_id="g2-stories", lane="partner")
    listed = ledger.entities()[0]
    assert listed["outcome"] == "eliminated" and listed["because"] == "vertical"


def test_scoring_nobody_says_so_instead_of_running_a_campaign(tmp_path, monkeypatch):
    """An empty population is an answer, not a failed model call."""
    from harness_fleet.dag import DagSpec, run_dag
    from harness_fleet.gates import LadderRung
    from harness_fleet.lanes import shipped_lanes
    from harness_fleet.rungs import lane_spec

    calls: list[str] = []

    class _Engine:
        def __init__(self, **kwargs):
            calls.append("built")

        def run_campaign(self, **kwargs):
            calls.append("ran")
            return {"total_verified_records": 0}

    monkeypatch.setattr("harness_fleet.dag.Engine", _Engine)
    captured = tmp_path / "captured.jsonl"
    captured.write_text(
        json.dumps({"item_id": "vendor.io", "text": "Book a demo of our platform.",
                    "content_type": "text/plain"}) + "\n",
        encoding="utf-8",
    )
    lane = shipped_lanes()["partner"]
    lane.funnel.ladder = [LadderRung(name="result", evidence="snippet", gates=["kind"])]
    spec = DagSpec.model_validate(lane_spec(
        lane, from_items=str(captured), workspace=tmp_path, score=True,
        score_task="partner-research",
    ))
    state = run_dag(spec, _store(tmp_path), workspace_root=tmp_path, dag_id="f1")

    scored = state["nodes"]["s-score"]
    assert scored["judged"] == 0 and scored["skipped"]
    assert "eliminated every candidate" in scored["skipped"]
    assert calls == [], "no campaign is built for a population of nobody"
    assert RungTables(_store(tmp_path)).rows("f1", "s-score") == []


def test_one_trajectory_from_both_records(tmp_path):
    """The engine prices campaigns; the ledger records nodes. One reader, both."""
    from argparse import Namespace

    from harness_fleet import cli

    store = _store(tmp_path)
    ledger = Ledger(store)
    # A node's judgement, long ago.
    ledger.record([_row("acme.co.uk", "qualified", score=87.5, tier="tier_2")],
                  dag_id="run-2", node_id="s-score", at="2020-01-01T00:00:00+00:00")
    # And a campaign run directly, which only the engine records — its own clock,
    # so the trajectory is ordered by when each number was produced.
    store.record_score_history("direct-run", "acme.co.uk", "acme.co.uk", 55.0)

    scores = ledger.scores("acme.co.uk")
    assert [row["score"] for row in scores] == [87.5, 55.0], "both doors are in the trajectory"
    assert scores[0]["tier"] == "tier_2" and scores[0]["node_id"] == "s-score"
    assert scores[1]["run_id"] == "direct-run"

    # And both commands report the same numbers, because they read the same rows.
    cli.cmd_history(Namespace(db=str(tmp_path / "t.db"), json=True, entity="acme.co.uk"))
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli.cmd_history(Namespace(db=str(tmp_path / "t.db"), json=False, entity="acme.co.uk"))
    assert "87.5" in buffer.getvalue() and "55.0" in buffer.getvalue()


def test_movers_counts_a_movement_the_engine_recorded(tmp_path):
    store = _store(tmp_path)
    store.record_score_history("run-a", "acme.co.uk", "acme.co.uk", 40.0)
    store.record_score_history("run-b", "acme.co.uk", "acme.co.uk", 70.0)
    movers = Ledger(store).movers()
    assert movers and movers[0]["delta"] == 30.0


# --------------------------------------------------------------------------
# The store is state somebody has to be able to see and trim


def test_a_walks_items_are_keyed_not_named_after_ids(tmp_path):
    """Two nodes can concatenate to the same name; a key cannot collide."""
    store = _store(tmp_path)
    tables = RungTables(store)
    # The pair that used to collide: "a-b"+"c" and "a"+"b-c" both became
    # rung_items_a_b_c, and each run wrote into the other's table.
    tables.write("a-b", "c", [_row("first.example", "read")], texts={"first.example": "one"})
    tables.write_items("a-b", "c", [{"item_id": "first.example", "text": "one"}], run_seq=1)
    tables.write("a", "b-c", [_row("second.example", "read")], texts={"second.example": "two"})
    tables.write_items("a", "b-c", [{"item_id": "second.example", "text": "two"}], run_seq=1)

    assert [item["item_id"] for item in tables.items("a-b", "c")] == ["first.example"]
    assert [item["item_id"] for item in tables.items("a", "b-c")] == ["second.example"]
    assert tables.text("a", "b-c", "second.example") == "two"


def test_prune_keeps_the_answer_and_drops_the_history(tmp_path):
    store = _store(tmp_path)
    tables = RungTables(store)
    tables.write("dag", "g0-result", [_row("acme.co.uk", "lead")], texts={"acme.co.uk": "old"})
    tables.write("dag", "g0-result", [_row("acme.co.uk", "eliminated")],
                 texts={"acme.co.uk": "new"})
    tables.write_items("dag", "g0-result", [{"item_id": "acme.co.uk"}], run_seq=1)
    assert tables.stats()["attempts"] == 2

    removed = tables.prune(keep_attempts=1)
    assert removed["rows"] == 1 and removed["text"] == 1
    # The newest attempt is untouched: pruning never changes the answer.
    assert tables.attempts("dag", "g0-result") == [2]
    assert tables.rows("dag", "g0-result")[0]["outcome"] == "eliminated"
    assert tables.text("dag", "g0-result", "acme.co.uk") == "new"
    # And it can be aimed at one graph.
    assert tables.prune(keep_attempts=1, dag_id="dag") == {"rows": 0, "text": 0, "items": 0}


def test_db_stats_and_prune_are_commands(tmp_path, capsys):
    from argparse import Namespace

    from harness_fleet import cli

    store = _store(tmp_path)
    tables = RungTables(store)
    tables.write("dag", "g", [_row("a.example", "lead")])
    tables.write("dag", "g", [_row("a.example", "lead")])
    args = dict(db=str(tmp_path / "t.db"), json=True)
    cli.cmd_db_stats(Namespace(**args))
    stats = json.loads(capsys.readouterr().out)
    assert stats["rung_rows"] == 2 and stats["attempts"] == 2 and stats["bytes"] > 0

    cli.cmd_db_prune(Namespace(**args, keep_attempts=1, dag_id=None))
    removed = json.loads(capsys.readouterr().out)
    assert removed["rows"] == 1
    cli.cmd_db_stats(Namespace(**args))
    assert json.loads(capsys.readouterr().out)["attempts"] == 1

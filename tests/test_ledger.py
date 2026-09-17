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

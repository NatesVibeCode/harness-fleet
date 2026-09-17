"""Each rung is a node, each node is a table, and nobody walks a dead candidate.

The ladder already said what each rung reads and what it may fetch. Compiled
into a DAG those rungs are stages, and these tests hold the two properties that
make the shape worth having: the table at each step is the whole population the
step was given (so a reader can see where the world shrank, not just who was
left), and a candidate eliminated on a search result is never fetched, never
re-gated, and never appears in a table above the rung that killed it.
"""

import csv
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness_fleet.dag import DagSpec, GateNode, RetrieveNode
from harness_fleet.gates import FETCHED, SNIPPET, FunnelReport, GateResult, LadderRung
from harness_fleet.lanes import shipped_lanes
from harness_fleet.rungs import (
    advances_to,
    count_rows,
    gate_rows,
    lane_spec,
    next_rung,
    read_table,
    write_table,
)

#: A delivery firm the shallow gates cannot finish judging, so the ladder owes
#: it a page, and a product company they can.
INTEGRATOR = "Northwind Consulting is a systems integrator in London."
VENDOR = "Zap Cloud. Book a demo of our platform and see pricing plans."


def _lane(name: str = "partner"):
    return shipped_lanes()[name]


def _record(item_id: str, text: str, uri: str = "", entity: str = ""):
    """A record as a run hands one over: an id, text, and what it is about."""

    class _Quote:
        def __init__(self, text):
            self.text = text

    class _Record:
        pass

    record = _Record()
    record.item_id = item_id
    record.text = text
    record.source_uri = uri
    record.quotes = [_Quote(text)]
    record.metadata = {"entity": entity} if entity else {}
    return record


def _rung_of(lane, name):
    ladder = lane.funnel.rungs()
    for rung in ladder:
        if rung.name == name:
            return rung, ladder
    raise AssertionError(f"no rung {name}")


def _verdict(gate: str, outcome: str):
    return GateResult(gate=gate, outcome=outcome, reason="because", evidence=SNIPPET)


# --------------------------------------------------------------------------
# The compile: the ladder is the graph


def test_every_rung_becomes_a_gate_and_the_walk_between_them_is_a_retrieve():
    spec = DagSpec.model_validate(lane_spec(_lane("partner"), from_run="discovery"))

    assert spec.topo_order() == [
        "g0-result",
        "r1-surface",
        "g1-surface",
        "r2-stories",
        "g2-stories",
    ]
    by_id = {node.id: node for node in spec.nodes}
    # The leading rung reads the run; nothing is fetched for it.
    assert isinstance(by_id["g0-result"], GateNode)
    assert by_id["g0-result"].from_run == "discovery"
    # The fetched rung is earned by a walk that reads exactly its surfaces.
    assert isinstance(by_id["r1-surface"], RetrieveNode)
    assert by_id["r1-surface"].from_gate == "g0-result"
    rung, _ladder = _rung_of(_lane("partner"), "surface")
    assert rung.surfaces == ["home", "about", "services", "partners", "careers"]
    assert by_id["r1-surface"].rung == "surface"
    # And the gate above the walk reads what the walk brought back.
    assert by_id["g1-surface"].from_retrieve == "r1-surface"


def test_a_fetched_rung_with_no_surfaces_gets_a_gate_but_no_walk():
    """A ladder can tighten the gates without spending a page visit."""
    lane = _lane("partner")
    lane.funnel.ladder = [
        LadderRung(name="result", evidence=SNIPPET, gates=["kind"]),
        LadderRung(name="again", evidence=FETCHED, gates=["size"], surfaces=[]),
    ]
    spec = DagSpec.model_validate(lane_spec(lane, from_run="discovery"))

    assert spec.topo_order() == ["g0-result", "g1-again"]
    assert not any(isinstance(node, RetrieveNode) for node in spec.nodes)
    by_id = {node.id: node for node in spec.nodes}
    assert by_id["g1-again"].from_gate == "g0-result"


def test_a_gate_needs_exactly_one_source():
    with pytest.raises(ValidationError, match="exactly one"):
        DagSpec.model_validate({
            "name": "x",
            "nodes": [
                {"kind": "run", "id": "d", "task": "t", "input": "i.csv"},
                {"kind": "gate", "id": "g", "lane": "partner", "rung": "result",
                 "from_run": "d", "from_gate": "g"},
            ],
        })


def test_a_retrieve_cannot_hang_off_a_run():
    with pytest.raises(ValidationError, match="not a gate node"):
        DagSpec.model_validate({
            "name": "x",
            "nodes": [
                {"kind": "run", "id": "d", "task": "t", "input": "i.csv"},
                {"kind": "retrieve", "id": "r", "lane": "partner", "rung": "surface",
                 "from_gate": "d"},
            ],
        })


def test_the_graph_admits_a_run_node_in_place_of_an_existing_run():
    """A ladder runs over a discovery run, or over the run node that makes one."""
    spec = DagSpec.model_validate({
        "name": "x",
        "nodes": [
            {"kind": "run", "id": "discover", "task": "t", "input": "i.csv"},
            {"kind": "gate", "id": "g", "lane": "partner", "rung": "result",
             "from_run": "discover"},
        ],
    })
    assert spec.topo_order() == ["discover", "g"]


def test_a_gate_naming_a_rung_its_lane_does_not_have_is_refused(tmp_path):
    from harness_fleet import dag as dag_module

    spec = DagSpec.model_validate({
        "name": "x",
        "nodes": [{"kind": "gate", "id": "g", "lane": "partner", "rung": "invented",
                   "from_run": "any"}],
    })
    with pytest.raises(dag_module.DagError, match="has no rung 'invented'"):
        dag_module.run_dag(spec, _store(tmp_path), workspace_root=tmp_path, dag_id="r")


# --------------------------------------------------------------------------
# The data rungs through


def test_a_rung_table_holds_every_candidate_it_was_given():
    """Not just the survivors: a table of winners cannot show where the world shrank."""
    lane = _lane("partner")
    rung, ladder = _rung_of(lane, "result")
    rows, reports = gate_rows(
        [_record("a", INTEGRATOR), _record("b", VENDOR)],
        rung=rung,
        ladder=ladder,
        profile=None,
    )

    assert [row["item_id"] for row in rows] == ["a", "b"]
    assert len(reports) == 2
    # Every row carries the verdicts that produced it, gate by gate.
    assert all(isinstance(row["gates"], list) for row in rows)


def test_an_eliminated_candidate_is_not_owed_the_next_rung():
    killed = FunnelReport(candidate="vendor.com", results=[_verdict("kind", "fail")])
    lead = FunnelReport(
        candidate="integrator.com",
        results=[_verdict("kind", "pass"), _verdict("vertical", "unknown")],
    )
    settled = FunnelReport(candidate="qualified.com", results=[_verdict("kind", "pass")])

    rung = LadderRung(name="stories", evidence=FETCHED, gates=["vertical"])
    assert not advances_to(killed, rung)
    # A qualified candidate has nothing left to buy: walking it spends a visit
    # on a question that is already answered.
    assert not advances_to(settled, rung)
    # The lead is owed the rung whose gates settle what is still open.
    assert advances_to(lead, rung)
    assert not advances_to(lead, None)


def test_a_lead_earns_the_rung_the_ladder_named_for_it():
    rung = LadderRung(name="surface", evidence=FETCHED, gates=["kind"], surfaces=["about"])
    assert advances_to(FunnelReport(candidate="x", earned="surface"), rung)


def test_the_next_rung_is_the_one_above_it():
    ladder = _lane("partner").funnel.rungs()
    assert next_rung(ladder, ladder[0]).name == "surface"
    assert next_rung(ladder, ladder[-1]) is None


def test_counts_say_where_the_world_shrank():
    rows = [
        {"item_id": "a", "outcome": "eliminated",
         "gates": [{"gate": "kind", "outcome": "fail"}]},
        {"item_id": "b", "outcome": "eliminated",
         "gates": [{"gate": "kind", "outcome": "pass"}, {"gate": "size", "outcome": "fail"}]},
        {"item_id": "c", "outcome": "lead", "gates": []},
    ]
    counts = count_rows(rows)
    assert counts["candidates"] == 3
    assert counts["verdicts"] == {"eliminated": 2, "lead": 1}
    assert counts["eliminated_at"] == {"kind": 1, "size": 1}


def test_a_table_round_trips_with_its_verdicts_intact(tmp_path):
    rows = [{
        "item_id": "a", "candidate": "acme.com", "rung": "result", "evidence": SNIPPET,
        "outcome": "eliminated", "earned": "", "because": "kind: a product vendor",
        "gates": [{"gate": "kind", "outcome": "fail", "reason": "a product vendor"}],
        "surfaces": {}, "pages": [], "text_path": "/tmp/a.txt", "source_uri": "https://x",
    }]
    path = tmp_path / "table.csv"
    assert write_table(path, rows) == 1

    back = read_table(path)
    assert back[0]["candidate"] == "acme.com"
    assert json.loads(back[0]["gates"])[0]["gate"] == "kind"
    assert back[0]["outcome"] == "eliminated"


def test_a_gate_appends_the_columns_it_adds(tmp_path):
    path = tmp_path / "table.csv"
    write_table(path, [{"item_id": "a", "advances": True, "next_rung": "surface"}])
    with path.open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header[:4] == ["item_id", "candidate", "rung", "evidence"]
    assert "advances" in header and "next_rung" in header


# --------------------------------------------------------------------------
# The chain end to end: a dead candidate never reaches the walk


def _store(tmp_path, *, routes: bool = True):
    """A real store (so a DAG run can resolve routes) with one seeded run."""
    from harness_fleet.catalog import PriceState, RouteCatalog
    from harness_fleet.store import HarnessStore

    store = HarnessStore(tmp_path / "t.db")
    if routes:
        RouteCatalog(db_path=store.path).add_route(
            route_id="demo/fake", provider="demo",
            cost_per_1k_input=0.0, cost_per_1k_output=0.0, enabled=True,
            price_state=PriceState.PRICE_OBSERVED_ZERO.value,
            verification_source="test",
        )
    return store


def _profile(tmp_path):
    """An authored profile, so the shallow gates have something to gate on.

    Without one the lane's own gates are all there is, and a partner lane ships
    none: the only live gate would be the kind question, which a search result
    answers, so the ladder would be exhausted on the first rung and never read a
    page. That is the honest behaviour and it is the profile that changes it.
    """
    (tmp_path / "ideal_partner_profile.json").write_text(json.dumps({
        "profile_name": "Test IPP",
        "partner_kind": "services",
        "partner_size_min": 20,
        "partner_size_max": 5000,
        "target_territories": ["United Kingdom"],
        "target_industries": ["fintech"],
    }), encoding="utf-8")


def _seed_run(monkeypatch, store, records):
    from harness_fleet import dag as dag_module

    monkeypatch.setattr(
        store, "run_snapshot",
        lambda run_id: {"run_id": run_id, "input_path": "", "task": {}, "batches": {}},
    )
    monkeypatch.setattr(
        dag_module, "verified_records_from_snapshot", lambda snapshot: (records, None)
    )


def test_the_walk_reads_only_what_the_rung_below_it_passed_on(tmp_path, monkeypatch):
    """The whole point, in one assertion: the vendor is never fetched."""
    from harness_fleet import dag as dag_module
    from harness_fleet.enrich import EnrichReport

    fetched: list[str] = []

    def fake_enrich(entity, **kwargs):
        fetched.append(entity)
        report = EnrichReport(entity=entity, domain=entity,
                              surfaces=list(kwargs.get("surface_order") or []))
        report.visited = 1
        report.kept = 1
        report.by_surface = {"about": 1}
        return [_record(f"{entity}-about", INTEGRATOR, uri=f"https://{entity}/about")], report

    monkeypatch.setattr(dag_module, "enrich_entity", fake_enrich, raising=False)
    _profile(tmp_path)
    store = _store(tmp_path)
    _seed_run(monkeypatch, store, [
        _record("vendor", VENDOR, uri="https://zapcloud.example", entity="zapcloud.example"),
        _record("integrator", INTEGRATOR, uri="https://northwind.example",
                entity="northwind.example"),
    ])
    spec = DagSpec.model_validate(lane_spec(_lane("partner"), from_run="discovery"))

    state = dag_module.run_dag(spec, store, workspace_root=tmp_path, dag_id="rungs")

    # The rung that reads search results ran over both candidates and killed one…
    first = read_table(state["nodes"]["g0-result"]["table"])
    assert [row["candidate"] for row in first] == ["zapcloud.example", "northwind.example"]
    assert [row["outcome"] for row in first] == ["eliminated", "lead"]
    assert state["nodes"]["g0-result"]["counts"]["eliminated_at"] == {"kind": 1}
    # …and only what it left alive was walked. The integrator is walked once per
    # rung it earned — the same firm, read for what that rung asked (its pages,
    # then its case studies) — and the vendor is walked by neither.
    assert fetched == ["northwind.example", "northwind.example"]
    assert "zapcloud.example" not in fetched
    walked = read_table(state["nodes"]["r1-surface"]["table"])
    assert [row["candidate"] for row in walked] == ["northwind.example"]
    assert state["nodes"]["r2-stories"]["surfaces"] == [
        "case_studies", "partners", "blog", "news",
    ]
    # The dead candidate is in no table above the rung that killed it.
    for node in ("r1-surface", "g1-surface"):
        assert "zapcloud.example" not in [
            row["candidate"] for row in read_table(state["nodes"][node]["table"])
        ]


def test_the_fetched_rung_above_a_walk_gates_on_what_was_read(tmp_path, monkeypatch):
    """The verdict changes grade: the same gates, now standing on a page."""
    from harness_fleet import dag as dag_module
    from harness_fleet.enrich import EnrichReport

    def fake_enrich(entity, **kwargs):
        report = EnrichReport(entity=entity, domain=entity,
                              surfaces=list(kwargs.get("surface_order") or []))
        report.visited = 3
        report.kept = 3
        report.by_surface = {"about": 1, "services": 1, "careers": 2}
        page = (
            f"{entity} is a systems integrator with 400 employees in London, "
            "serving fintech clients."
        )
        return [_record(f"{entity}-about", page, uri=f"https://{entity}/about")], report

    monkeypatch.setattr(dag_module, "enrich_entity", fake_enrich, raising=False)
    _profile(tmp_path)
    store = _store(tmp_path)
    _seed_run(monkeypatch, store, [
        _record("integrator", INTEGRATOR, uri="https://northwind.example",
                entity="northwind.example"),
    ])
    spec = DagSpec.model_validate(lane_spec(_lane("partner"), from_run="discovery"))

    state = dag_module.run_dag(spec, store, workspace_root=tmp_path, dag_id="rungs")

    assert state["nodes"]["r1-surface"]["surfaces"] == [
        "home", "about", "services", "partners", "careers",
    ]
    assert state["nodes"]["r1-surface"]["read"] == 1
    second = read_table(state["nodes"]["g1-surface"]["table"])[0]
    # Read, so this rung gates as a page: the headcount and the place are on the
    # page, the gates pass, and nothing is left to settle.
    assert second["evidence"] == FETCHED
    assert second["outcome"] == "qualified"
    assert second["because"] == ""
    assert second["advances"] == "False"
    assert Path(second["text_path"]).read_text(encoding="utf-8").startswith("northwind")


def test_a_rung_reports_the_gates_it_ran_and_where_its_table_lives(tmp_path, monkeypatch):
    from harness_fleet import dag as dag_module

    _profile(tmp_path)
    store = _store(tmp_path)
    _seed_run(monkeypatch, store, [
        _record("a", VENDOR, uri="https://zapcloud.example", entity="zapcloud.example"),
    ])
    spec = DagSpec.model_validate(lane_spec(_lane("partner"), from_run="discovery"))

    state = dag_module.run_dag(spec, store, workspace_root=tmp_path, dag_id="rungs")
    node = state["nodes"]["g0-result"]

    assert node["gates"] == ["kind", "size", "location"]
    assert node["counts"]["candidates"] == 1
    assert node["next_rung"] == "surface"
    assert Path(node["table"]).is_file()
    row = read_table(node["table"])[0]
    assert Path(row["text_path"]).read_text(encoding="utf-8").startswith("Zap Cloud")


def test_a_run_of_the_chain_writes_the_sidecar_with_every_table(tmp_path, monkeypatch):
    from harness_fleet import dag as dag_module

    _profile(tmp_path)
    store = _store(tmp_path)
    _seed_run(monkeypatch, store, [
        _record("a", VENDOR, entity="zapcloud.example"),
    ])
    spec = DagSpec.model_validate(lane_spec(_lane("partner"), from_run="discovery"))
    dag_module.run_dag(spec, store, workspace_root=tmp_path, dag_id="rungs")

    sidecar = json.loads((tmp_path / "runs" / "rungs" / "dag.json").read_text())
    assert sidecar["nodes"]["g0-result"]["kind"] == "gate"
    assert sidecar["nodes"]["g0-result"]["table"].endswith("table.csv")


def test_the_cli_compiles_a_lane_ladder_into_a_spec(tmp_path, capsys):
    """The lane a person names is the ladder the engine runs, same order."""
    from argparse import Namespace

    from harness_fleet import cli

    cli.cmd_dag(Namespace(
        spec=None, lane="partner", from_run="discovery", emit_spec=True,
        dag_id=None, dry_run=False, no_resume=False, profile=None,
        workspace_root=str(tmp_path), db=str(tmp_path / "t.db"), json=True,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert [node["id"] for node in payload["nodes"]] == [
        "g0-result", "r1-surface", "g1-surface", "r2-stories", "g2-stories",
    ]


def test_the_cli_refuses_a_lane_with_no_run_to_gate(tmp_path):
    from argparse import Namespace

    from harness_fleet import cli
    from harness_fleet.dag import DagError

    with pytest.raises(DagError, match="--from-run"):
        cli.cmd_dag(Namespace(
            spec=None, lane="partner", from_run=None, emit_spec=False,
            dag_id=None, dry_run=False, no_resume=False, profile=None,
            workspace_root=str(tmp_path), db=str(tmp_path / "t.db"), json=True,
        ))

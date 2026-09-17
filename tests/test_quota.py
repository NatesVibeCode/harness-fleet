"""Deliver to a quota: how many records, at or above what score.

The unit of work is the delivery, not the run. An operator asks for two hundred
firms at seventy and gets two hundred firms at seventy — or gets told, in the
report, exactly what the lane would have to change for that to be possible.
"""

import json

from harness_fleet.lanes import shipped_lanes
from harness_fleet.ledger import Ledger
from harness_fleet.quota import (
    Quota,
    Round,
    delivered,
    outstanding,
    query_space,
    rounds,
    verdict,
)
from harness_fleet.store import HarnessStore


def _store(tmp_path):
    return HarnessStore(tmp_path / "t.db")


def _scored(store, entity: str, score: float, **extra):
    row = {
        "item_id": entity, "candidate": entity, "outcome": "", "gates": [],
        "score": score, "tier": extra.pop("tier", "tier_2"), "run_id": "r1",
    }
    row.update(extra)
    Ledger(store).record([row], dag_id="r1", node_id="s-score", lane="partner")


def test_the_query_space_is_what_a_quota_can_draw_from():
    """One round searches a lane's cap; the space is everything it could ask."""
    lane = shipped_lanes()["partner"]
    space = query_space(lane)
    assert len(space) > lane.max_queries, "widening must have somewhere to go"
    assert all("{" not in query for query in space), "every template is filled in"
    assert len(set(space)) == len(space), "and the space is deduplicated"


def test_each_round_takes_the_next_slice_never_the_same_one():
    """A loop that repeats its queries pays to rediscover the same firms."""
    lane = shipped_lanes()["partner"]
    plan = rounds(lane, Quota(want=500, min_score=70))

    assert len(plan) > 1, "a quota bigger than one round widens"
    assert plan[0] == query_space(lane)[: lane.max_queries]
    assert not set(plan[0]) & set(plan[1]), "no query is searched twice"
    flattened = [query for entry in plan for query in entry]
    assert len(flattened) == len(set(flattened))
    # And the whole space is covered: exhaustion is a fact, not a guess.
    assert set(flattened) == set(query_space(lane))


def test_the_loop_is_bounded_by_the_operator_not_by_the_tool():
    lane = shipped_lanes()["partner"]
    assert len(rounds(lane, Quota(want=500, min_score=70, max_rounds=2))) == 2
    every = rounds(lane, Quota(want=500, min_score=70))
    assert len(rounds(lane, Quota(want=500, min_score=70, max_rounds=len(every) + 5))) == len(every), (
        "asking for more rounds than the space holds gives the space, not padding"
    )


def test_delivery_is_counted_across_runs_and_never_twice(tmp_path):
    store = _store(tmp_path)
    _scored(store, "good.example", 82.0)
    _scored(store, "also.example", 71.0)
    _scored(store, "nearly.example", 68.0)
    Ledger(store).record(
        [{"item_id": "walked.example", "candidate": "walked.example", "outcome": "read", "gates": []}],
        dag_id="r2", node_id="r1-surface",
    )

    assert [row["entity"] for row in delivered(store, 70)] == ["good.example", "also.example"]
    assert {row["entity"] for row in delivered(store, 60)} == {
        "good.example", "also.example", "nearly.example",
    }, "a lower bar delivers more of the same firms"
    assert outstanding(store, Quota(want=5, min_score=70)) == 3
    assert outstanding(store, Quota(want=2, min_score=70)) == 0, "met is met"
    assert outstanding(store, Quota(want=2, min_score=90)) == 2, "the bar is the bar"


def test_stopping_short_says_what_to_change(tmp_path):
    """A shortfall is a fact about the lane, not a request for patience."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_board import _partner_run

    store = HarnessStore(_partner_run(tmp_path, "quota-run"))
    _scored(store, "one.example", 74.0)

    short = verdict(
        store, Quota(want=200, min_score=70),
        [Round(number=1, queries=["a", "b"], delivered_total=1)],
    )
    assert short["met"] is False and short["owed"] == 199
    assert "delivered 1 of 200 at or above 70" in short["summary"]
    assert "after 2 queries" in short["summary"]

    met = verdict(store, Quota(want=1, min_score=70), [Round(number=1, queries=["a"])])
    assert met["met"] is True and met["owed"] == 0
    assert met["summary"] == "delivered 1 at or above 70"

    nothing = verdict(_store(tmp_path / "empty"), Quota(want=10, min_score=70), [])
    assert nothing["delivered"] == 0 and nothing["met"] is False
    assert json.dumps(nothing)  # and the whole thing is reportable


# --------------------------------------------------------------------------
# The loop itself


def test_the_loop_stops_the_moment_the_quota_is_met(tmp_path):
    """The round ceiling bounds spending; it is not a target to reach."""
    from harness_fleet.quota import run_to_quota

    store = _store(tmp_path)
    lane = shipped_lanes()["partner"]
    ran: list[list[str]] = []

    def run_one(queries, index):
        ran.append(list(queries))
        # Two firms per round, so a quota of two is met in one.
        for offset in range(2):
            _scored(store, f"r{index}-{offset}.example", 80.0)
        return {"items": 7, "note": "40 candidates before the gates"}

    result = run_to_quota(store, lane, Quota(want=2, min_score=70, max_rounds=9), run_one)

    assert len(ran) == 1, "met in round one, so round two never happens"
    # What the round saw is part of the report: a round that finds plenty and
    # delivers none is a different problem from one that finds nothing.
    assert result["rounds"][0]["candidates"] == 7
    assert result["rounds"][0]["note"] == "40 candidates before the gates"
    assert result["met"] is True and result["delivered"] == 2 and result["owed"] == 0


def test_the_loop_widens_and_never_repeats_a_query(tmp_path):
    from harness_fleet.quota import run_to_quota

    store = _store(tmp_path)
    lane = shipped_lanes()["partner"]
    ran: list[list[str]] = []

    def run_one(queries, index):
        ran.append(list(queries))
        _scored(store, f"round{index}.example", 75.0)   # one new firm per round

    plan = rounds(lane, Quota(want=3, min_score=70))
    result = run_to_quota(store, lane, Quota(want=3, min_score=70), run_one)

    # The lane's space is finite, so the loop runs it out rather than inventing
    # queries — and the report says whether that was enough.
    assert len(ran) == len(plan), "one round per slice of the lane's query space"
    searched = [query for entry in ran for query in entry]
    assert len(searched) == len(set(searched)), "widening, never repeating"
    assert result["added"] == len(plan), "one new firm per round"
    assert result["met"] is (result["delivered"] >= 3)


def test_a_quota_run_never_delivers_a_firm_twice(tmp_path):
    """The fleet's own record is the ledger, so last week's firms count."""
    from harness_fleet.quota import run_to_quota

    store = _store(tmp_path)
    _scored(store, "already.example", 88.0)          # delivered before this run
    lane = shipped_lanes()["partner"]
    ran: list[int] = []

    def run_one(queries, index):
        ran.append(index)
        _scored(store, f"new{index}.example", 72.0)

    result = run_to_quota(store, lane, Quota(want=2, min_score=70), run_one)

    assert result["delivered"] == 2, "the firm from before counts toward the quota"
    assert len(ran) == 1, "so only one more was needed"
    assert outstanding(store, Quota(want=2, min_score=70)) == 0


def test_running_out_of_lane_says_so_and_writes_what_it_has(tmp_path):
    from harness_fleet.quota import run_to_quota

    store = _store(tmp_path)
    lane = shipped_lanes()["partner"]

    def run_one(queries, index):
        _scored(store, f"only{index}.example", 90.0)

    deliverable = tmp_path / "delivered.csv"
    result = run_to_quota(
        store, lane, Quota(want=10_000, min_score=70, max_rounds=2), run_one,
        deliverable=deliverable,
    )

    assert result["met"] is False
    assert result["rounds"] and len(result["rounds"]) == 2, "the ceiling was the bound"
    assert result["delivered"] == 2 and result["owed"] == 9_998
    assert "delivered 2 of 10000 at or above 70" in result["summary"]
    # And the file holds the firms that were delivered, not the ones that were not.
    from harness_fleet.rungs import read_csv

    rows = read_csv(deliverable)
    assert [row["entity"] for row in rows] == ["only1.example", "only2.example"]
    assert all(float(row["score"]) >= 70 for row in rows)


def test_the_quota_command_wires_the_loop_up(tmp_path, capsys):
    """`research --want N --min-score S` runs rounds and reports what it got."""
    from argparse import Namespace

    from harness_fleet import cli

    store = HarnessStore(tmp_path / "t.db")
    calls: list[Namespace] = []

    def run_one(queries, index):
        calls.append(Namespace(queries=list(queries), index=index))
        Ledger(store).record(
            [{"item_id": f"firm{index}.example", "candidate": f"firm{index}.example",
              "outcome": "", "gates": [], "score": 80.0}],
            dag_id=f"quota-{index}", node_id="s-score", lane="partner",
        )

    args = Namespace(want=2, min_score=70.0, rounds=0, json=True, output="delivered.csv")
    result = cli._quota_run(
        args, store, shipped_lanes()["partner"], tmp_path / "delivered.csv", "q1", run_one,
    )

    # Progress lines go to stdout as the loop runs; the payload is the last one.
    printed = capsys.readouterr().out
    # Each round emits its own payload, and the quota's is the last one.
    start = printed.rindex("\n{\n") + 1 if "\n{\n" in printed else printed.index("{\n")
    payload = json.loads(printed[start:])   # past the progress lines
    assert payload["met"] is True and payload["delivered"] == 2
    assert len(calls) == 2, "one firm a round means two rounds for two firms"
    assert calls[0].queries != calls[1].queries, "and the second round widened"
    assert result["rows"] == 2


# --------------------------------------------------------------------------
# Through the real command, not around it


def _lane_workspace(tmp_path, **overrides):
    (tmp_path / "lanes").mkdir(exist_ok=True)
    (tmp_path / "lanes" / "partner.json").write_text(json.dumps({
        "name": "partner",
        "description": "systems integrators",
        "queries": ["\"{tech}\" systems integrator"],
        "query_terms": {"tech": ["Kafka", "dbt", "Airflow", "Terraform", "Flink", "Spark"]},
        "max_queries": 2,
        "backends": ["ddgs"],
        "preset": "partner-research",
        "top": 5,
        **overrides,
    }), encoding="utf-8")
    return tmp_path


def _research_args(tmp_path, **overrides):
    from argparse import Namespace

    base = dict(
        lane="partner", query=None, backend=None, preset=None, top=None, output=None,
        workspace_root=str(tmp_path), db=str(tmp_path / "s.db"), json=True, run_id=None,
        max_results=3, timeout=5, delay=0, ignore_robots=True, min_chars=None,
        min_source_coverage=None, sessions=1, max_attempts=5, route=["demo/fake"],
        no_funnel=False, no_enrich=True, profile=None, enrich_pages=1, enrich_entities=0,
        want=0, min_score=0.0, rounds=0,
    )
    base.update(overrides)
    return Namespace(**base)


def test_research_delivers_a_quota_through_the_command(tmp_path, monkeypatch, capsys):
    """Rounds of the real command: discovery, the graph, the score, the count.

    The loop is only as good as the run it repeats, so this drives
    `cmd_research` itself: a stubbed search that finds one new firm per round,
    the deterministic demo route for scoring, and a quota of two.
    """
    from harness_fleet import cli
    from harness_fleet.catalog import PriceState, RouteCatalog
    from harness_fleet.models import InputItem
    from harness_fleet.store import HarnessStore

    _lane_workspace(tmp_path)
    # The demo route is registered before the command runs: without a verified
    # free route, the engine refreshes prices over the network, which is minutes
    # of waiting per round and nothing to do with the loop.
    store = HarnessStore(tmp_path / "s.db")
    RouteCatalog(db_path=store.path).add_route(
        route_id="demo/fake", provider="demo",
        cost_per_1k_input=0.0, cost_per_1k_output=0.0, enabled=True,
        price_state=PriceState.PRICE_OBSERVED_ZERO.value, verification_source="test",
    )
    rounds_seen: list[list[str]] = []

    def fake_discovery(**kwargs):
        rounds_seen.append(list(kwargs.get("queries") or []))
        index = len(rounds_seen)
        return (
            [InputItem(item_id=f"firm{index}.example", text=f"Firm {index} is a systems integrator.",
                       source_uri=f"https://firm{index}.example")],
            {"hits": 1, "skipped": [], "source_quality": {"captured": 1, "attempted": 1,
                                                          "coverage": 1.0, "meets_threshold": True}},
        )

    monkeypatch.setattr(cli, "run_discovery", fake_discovery)
    monkeypatch.setattr(cli, "iter_input_items", lambda *a, **k: iter([
        InputItem(item_id="firm1.example", text="Firm 1 is a systems integrator.",
                  source_uri="https://firm1.example"),
    ]))
    monkeypatch.setattr(cli, "_check_routes_for_run", lambda *a, **k: None)
    # The resolve node asks a search engine for the address; there is no network
    # in a test, and a real timeout per round is what makes this slow.
    monkeypatch.setattr("harness_fleet.discover.web_search", lambda *a, **k: [])
    # Deliberately not stubbing the evidence readout: it reads the deliverable's
    # input through the same loader everything else uses, and stubbing it hid a
    # file whose content did not match its own name.


    args = _research_args(tmp_path, want=2, min_score=0.0, rounds=3, output="delivered.csv")
    cli.cmd_research(args)

    printed = capsys.readouterr().out
    # Each round emits its own payload, and the quota's is the last one.
    start = printed.rindex("\n{\n") + 1 if "\n{\n" in printed else printed.index("{\n")
    payload = json.loads(printed[start:])
    assert payload["met"] is True
    assert payload["delivered"] == 2
    assert len(rounds_seen) == 2, "one firm a round, two firms wanted"
    assert rounds_seen[0] != rounds_seen[1], "and the second round widened"

    # The deliverable is in the workspace, says who and how good up front, and
    # holds exactly the firms the quota was met with.
    from harness_fleet.rungs import read_csv

    deliverable = tmp_path / "delivered_quota.csv"
    assert deliverable.is_file(), "a quota run files its delivery in the workspace"
    assert payload["deliverable"] == str(deliverable)
    rows = read_csv(deliverable)
    assert [row["entity"] for row in rows] == ["firm1.example", "firm2.example"]
    assert list(rows[0])[:3] == ["entity", "score", "tier"]

    # And the run's own input file is readable by the loader that reads it.
    from harness_fleet.input_data import load_input_items

    shipped = load_input_items(
        tmp_path / "delivered.csv", id_column="item_id", text_column="text",
        uri_column="source_uri",
    )
    assert shipped, "the dossiers the campaign judged are in the file the run names"


# --------------------------------------------------------------------------
# Why it came up short


def _visit(store, entity, node, outcome, dag, score=0.0):
    Ledger(store).record(
        [{"item_id": entity, "candidate": entity, "outcome": outcome, "gates": [],
          "score": score}],
        dag_id=dag, node_id=node, lane="partner",
    )


def test_a_shortfall_names_the_stage_that_cost_the_volume(tmp_path):
    """Three stages can leave an operator short, and each has a different fix."""
    from harness_fleet.quota import BAR, FUNNEL, SEARCH, diagnose

    # The gates are the constraint: plenty surfaced, few stood.
    store = _store(tmp_path / "funnel")
    for index in range(40):
        _visit(store, f"firm{index}.example", "g0-result",
               "eliminated" if index > 5 else "lead", "d1")
    found = diagnose(store, Quota(want=100, min_score=70),
                     [Round(number=1, queries=["q"], candidates=40)], ["d1"])
    assert found["stage"] == FUNNEL
    assert "40 candidate(s) surfaced, 6 stood" in found["findings"][0]["detail"]
    assert found["findings"][0]["eliminated_at"]["g0-result"] == 34
    # The numbers partition: what surfaced is what stood plus what was thrown
    # out, counted per firm — a firm that dies at the second gate is not also
    # counted as standing at the first.
    assert found["surfaced"] == found["standing"] + found["eliminated"] == 40

    # The bar is the constraint: firms standing and scoring, none high enough.
    store = _store(tmp_path / "bar")
    _visit(store, "close.example", "g0-result", "lead", "d1")
    _scored(store, "close.example", 64.0)
    near = diagnose(store, Quota(want=100, min_score=70),
                    [Round(number=1, queries=["q"], candidates=1)], ["d1"])
    assert near["stage"] == BAR
    assert "the best of them 64" in near["findings"][0]["detail"]
    assert "lower --min-score to 64" in near["findings"][0]["lever"]

    # Neither: the search itself is thin, and the fix is the lane's terms.
    store = _store(tmp_path / "search")
    _visit(store, "one.example", "g0-result", "lead", "d1")
    thin = diagnose(store, Quota(want=100, min_score=70),
                    [Round(number=1, queries=["q"], candidates=1)], ["d1"])
    assert thin["stage"] == SEARCH
    assert "add terms to query_terms" in thin["findings"][0]["lever"]


def test_the_loop_reports_the_diagnosis_when_it_stops_short(tmp_path):
    from harness_fleet.quota import run_to_quota

    store = _store(tmp_path)
    lane = shipped_lanes()["partner"]

    def run_one(queries, index):
        for offset in range(30):
            _visit(store, f"r{index}-{offset}.example", "g0-result",
                   "eliminated" if offset else "lead", f"r{index}-funnel")
        return {"items": 30}

    result = run_to_quota(
        store, lane, Quota(want=500, min_score=70, max_rounds=1), run_one,
        dag_for=lambda index: f"r{index}-funnel",
    )
    assert result["met"] is False
    assert result["diagnosis"]["stage"] == "funnel"
    assert result["diagnosis"]["surfaced"] == 30
    assert result["diagnosis"]["standing"] == 1
    assert result["diagnosis"]["eliminated"] == 29
    assert (
        result["diagnosis"]["surfaced"]
        == result["diagnosis"]["standing"] + result["diagnosis"]["eliminated"]
    ), "and the three numbers still add up"


def test_two_queries_finding_the_same_firm_is_one_candidate(tmp_path):
    """Search returns the same company for several queries. That is not an error.

    The graph writes the candidates down and reads them back through a loader
    that rejects a repeated id, and the old funnel grouped by entity instead — so
    wiring the funnel into the graph made the most ordinary thing search does
    fail a live run: `duplicate item_id: gpsolutions.com`.
    """
    from harness_fleet import cli
    from harness_fleet.models import InputItem

    first = InputItem(item_id="gpsolutions.com", text="GPS is a systems integrator.",
                      source_uri="https://gpsolutions.com")
    second = InputItem(item_id="gpsolutions.com", text="They deliver Kafka pipelines.",
                       source_uri="https://gpsolutions.com/about")

    merged, extra = cli.merge_candidates([first, second, InputItem(
        item_id="other.example", text="Another firm.", source_uri="https://other.example")])

    assert extra == 1
    assert [item.item_id for item in merged] == ["gpsolutions.com", "other.example"]
    assert "systems integrator" in merged[0].text and "Kafka pipelines" in merged[0].text, (
        "both sources are evidence about the one firm, so both are kept"
    )
    assert cli.merge_candidates([first])[1] == 0, "and a run with no overlap says nothing"


def test_a_directory_is_not_a_candidate(tmp_path):
    """A catalogue of companies is evidence about none of them.

    A live run scored `integratorguide.com` and every checklist item came back
    false — and the model was right: the page lists *other* firms, so the row
    described nobody. A directory profile is different: its key is the company
    it is about, and it stays.
    """
    from harness_fleet import cli

    assert cli.is_directory_host("clutch.co") is True, "a review platform is not a firm"
    assert cli.is_directory_host("g2.com") is True
    assert cli.is_directory_host("integratorguide.com") is False, (
        "an unrecognised host is a company until the taxonomy says otherwise — "
        "the registry is how a directory gets named"
    )
    assert cli.is_directory_host("northwind.example") is False
    assert cli.is_directory_host("") is False

    # And the ones nobody has named yet: the page's own words say what it is.
    live = ("Search for automation and control system integrators by name, location, "
            "industries served, areas served")
    assert cli.reads_as_directory(live) is True, "the page a live run scored zero on"
    assert cli.reads_as_directory("Northwind Consulting is a systems integrator.") is False
    assert cli.reads_as_directory("") is False


def test_the_score_comes_from_the_checklist_not_the_workers_arithmetic():
    """A live model answered one checklist item true and wrote `score: 0`.

    The checklist is the scoring model — its points are what calibration fits —
    so the number is computed from it, and a supplied one never overrides it.
    Trusting the worker's arithmetic made every scored record of a live run look
    like a zero, including records whose own checklist said otherwise.
    """
    from harness_fleet.task import create_task_from_preset

    task = create_task_from_preset("partner-research", preset_name="partner-research")
    checklist = {name: name == "q1_billable_delivery" for name in task.checklist}

    derived = task.with_derived_claims({"checklist": checklist, "score": 0})
    assert derived["score"] == task.checklist["q1_billable_delivery"] == 15, (
        "the checklist decides the number, and the worker's zero is ignored"
    )

    # And the rest of the checklist still moves it.
    all_true = {name: True for name in task.checklist}
    assert task.with_derived_claims({"checklist": all_true})["score"] == sum(
        task.checklist.values()
    )

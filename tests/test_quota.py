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

    result = run_to_quota(store, lane, Quota(want=2, min_score=70, max_rounds=9), run_one)

    assert len(ran) == 1, "met in round one, so round two never happens"
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
    payload = json.loads(printed[printed.index("{"):])   # past the progress lines
    assert payload["met"] is True and payload["delivered"] == 2
    assert len(calls) == 2, "one firm a round means two rounds for two firms"
    assert calls[0].queries != calls[1].queries, "and the second round widened"
    assert result["rows"] == 2

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

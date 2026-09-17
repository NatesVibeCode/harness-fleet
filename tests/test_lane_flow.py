"""A lane drives a real run: its queries, its sources, its filters, its preset."""
import json
from argparse import Namespace

import pytest

from harness_fleet import cli
from harness_fleet.models import InputItem


def _workspace(tmp_path, **lane_fields):
    (tmp_path / "lanes").mkdir()
    (tmp_path / "lanes" / "career.json").write_text(json.dumps({
        "name": "career",
        "description": "enterprise sales/ops, remote",
        "seeds": ["enterprise software"],
        "title_include": ["enterprise sales", "sales ops"],
        "remote": True,
        "preset": "triage",
        "funnel": {"allows": "any"},
        "top": 5,
        **lane_fields,
    }), encoding="utf-8")
    return tmp_path


def _args(tmp_path, **overrides):
    base = dict(
        lane="career", query=None, backend=None, preset=None, top=None, output=None,
        workspace_root=str(tmp_path), db=str(tmp_path / "s.db"), json=True, run_id=None,
        max_results=5, timeout=5, delay=0, ignore_robots=True, min_chars=None,
        min_source_coverage=None, sessions=1, max_attempts=5, route=["demo/fake"],
    )
    base.update(overrides)
    return Namespace(**base)


def test_the_lane_supplies_queries_sources_filters_and_preset(tmp_path, monkeypatch, capsys):
    _workspace(tmp_path)
    seen = {}

    def fake_discovery(**kwargs):
        seen.update(kwargs)
        return (
            [
                InputItem(item_id="a", title="Enterprise Sales Director", text="Remote role",
                          source_uri="https://acme.com/jobs/1"),
                InputItem(item_id="b", title="Warehouse Associate", text="On-site in Ohio",
                          source_uri="https://acme.com/jobs/2"),
            ],
            {"hits": 2, "skipped": [], "source_quality": {"captured": 2, "attempted": 2, "coverage": 1.0,
                                                          "meets_threshold": True}},
        )

    monkeypatch.setattr(cli, "run_discovery", fake_discovery)
    monkeypatch.setattr(cli, "iter_input_items", lambda *a, **k: iter([
        InputItem(item_id="acme.com", title="Enterprise Sales Director", text="Remote role",
                  source_uri="https://acme.com/jobs/1"),
    ]))
    monkeypatch.setattr(cli, "_check_routes_for_run", lambda *a, **k: None)

    class FakeEngine:
        def __init__(self, **kwargs):
            seen["task"] = kwargs["task"].name

        def run_campaign(self, **kwargs):
            return {"total_verified_records": 1}

    # The scoring stage is a node in the graph now, so the engine it builds is
    # the DAG's — which is the point: the command no longer runs a campaign of
    # its own.
    monkeypatch.setattr("harness_fleet.dag.Engine", FakeEngine)
    monkeypatch.setattr(
        "harness_fleet.dag.verified_records_from_snapshot", lambda snapshot: ([], None)
    )
    monkeypatch.setattr(cli.HarnessStore, "run_snapshot", lambda self, run_id: {"status": "completed"})
    # cmd_research imports the exporter inside the call, so patch it where it lives.
    monkeypatch.setattr("harness_fleet.export.export_clean_packet", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_evidence_readout", lambda *a, **k: {"items": {}, "kind_totals": {},
                                                                   "tier_capped": {}, "contradictions": {},
                                                                   "text_missing": []})

    cli.cmd_research(_args(tmp_path))
    out = capsys.readouterr().out
    assert "Lane 'career'" in out
    assert seen["queries"] == ["enterprise software"], "the lane supplied the query"
    assert seen["task"] == "triage", "the lane chose the checklist"


def test_a_lane_this_workspace_does_not_have_is_reported(tmp_path):
    with pytest.raises(ValueError, match="no lane 'nope'"):
        cli.cmd_research(_args(tmp_path, lane="nope"))


def test_the_lane_filter_is_applied_not_assumed(tmp_path):
    from harness_fleet.lanes import Lane

    lane = Lane(name="career", title_include=["enterprise sales"], remote=True)
    items = [
        InputItem(item_id="a", title="Enterprise Sales Director", text="Remote", source_uri="https://x/1"),
        InputItem(item_id="b", title="Enterprise Sales Director", text="On-site", source_uri="https://x/2"),
        InputItem(item_id="c", title="Warehouse", text="Remote", source_uri="https://x/3"),
    ]
    kept, dropped = cli._lane_items(items, lane)
    assert [item.item_id for item in kept] == ["a"]
    assert dropped == 2


def test_without_a_lane_nothing_is_filtered(tmp_path):
    from harness_fleet.models import InputItem as Item

    items = [Item(item_id="a", title="anything", text="at all", source_uri="https://x/1")]
    assert cli._lane_items(items, None) == (items, 0)


def test_routes_are_checked_before_anything_is_searched(tmp_path, monkeypatch):
    """A run that cannot be scored must not spend minutes on the web first.

    When scoring moved into the graph, the route check moved out of the command
    with it: a live probe discovered it had no usable route *after* discovery and
    sixty-five page fetches, and failed on a check that costs nothing.
    """
    _workspace(tmp_path)
    order: list[str] = []

    def route_check(store, policy):
        order.append("routes")
        raise RuntimeError("no usable route")

    monkeypatch.setattr(cli, "_check_routes_for_run", route_check)
    monkeypatch.setattr(cli, "run_discovery", lambda **kwargs: order.append("discovery"))

    with pytest.raises(RuntimeError, match="no usable route"):
        cli.cmd_research(_args(tmp_path))
    assert order == ["routes"], "the search never started"

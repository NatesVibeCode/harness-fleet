"""Deterministic mechanisms: tier derivation, stack/title gates, IN/AND-OR filters, cost fail-closed."""
import pytest

from harness_fleet.discover import (
    RawRecord,
    fetch_ashby_org,
    fetch_greenhouse_board,
    stack_signal,
    to_input_items,
)
from harness_fleet.export import _evaluate_filter
from harness_fleet.models import (
    ClaimFilter,
    FilterClause,
    FilterOp,
    TaskSpec,
    score_to_fit_tier,
)
from harness_fleet.scoring import rank_with_scores
from harness_fleet.store import HarnessStore


def _account_task():
    return TaskSpec(
        name="triage-check",
        claims_schema={
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "fit_tier": {"enum": ["tier_1", "tier_2", "tier_3", "unfit"]},
            },
            "required": ["score", "fit_tier"],
            "additionalProperties": False,
        },
    )


# --- 1. score -> tier -------------------------------------------------------

@pytest.mark.parametrize(("score", "tier"), [
    (100, "tier_1"), (85, "tier_1"), (84, "tier_2"), (70, "tier_2"),
    (69, "tier_3"), (50, "tier_3"), (49, "unfit"), (0, "unfit"),
])
def test_score_to_fit_tier_boundaries(score, tier):
    assert score_to_fit_tier(score) == tier


def test_validate_claims_rejects_inconsistent_tier():
    task = _account_task()
    task.validate_claims({"score": 92, "fit_tier": "tier_1"})
    with pytest.raises(ValueError, match="inconsistent"):
        task.validate_claims({"score": 92, "fit_tier": "unfit"})
    with pytest.raises(ValueError, match="inconsistent"):
        task.validate_claims({"score": 40, "fit_tier": "tier_2"})


# --- 3. stack / title / evidence gates --------------------------------------

def test_stack_signal_verdicts():
    assert stack_signal("we run Kafka on k8s", ["kafka"], ["mainframe"])["verdict"] == "matched"
    assert stack_signal("cobol mainframe shop", ["kafka"], ["mainframe"])["verdict"] == "excluded"
    assert stack_signal("generic retail corp", ["kafka"], [])["verdict"] == "missing"
    assert stack_signal("anything", [], [])["verdict"] == "unconstrained"


def test_to_input_items_min_chars_and_evidence_gate():
    records = [
        RawRecord(text="ok " * 50, source_uri="https://a.example/1", metadata={"evidence": "fetched"}),
        RawRecord(text="stub", source_uri="https://a.example/2", metadata={"evidence": "fetched"}),
        RawRecord(text="snippet " * 50, source_uri="https://a.example/3", metadata={"evidence": "indicator"}),
    ]
    items = to_input_items(records, min_chars=50, allowed_evidence=("fetched", "profile"))
    assert [i.item_id for i in items] == [items[0].item_id] and len(items) == 1
    assert items[0].source_uri == "https://a.example/1"


def test_to_input_items_stack_veto_annotates():
    records = [RawRecord(text="runs on mainframe cobol", source_uri="https://a.example/1")]
    (item,) = to_input_items(records, excluded_stack=["mainframe"])
    assert item.metadata["stack_veto"] is True
    assert item.metadata["stack_signal"]["verdict"] == "excluded"


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, **kwargs):
        return _FakeResp(self._payload)


def test_ats_title_filter_gates_before_llm():
    payload = {"jobs": [
        {"id": 1, "title": "Senior Kafka Engineer", "absolute_url": "https://x/1",
         "content": "<p>Kafka work</p>"},
        {"id": 2, "title": "Office Manager", "absolute_url": "https://x/2",
         "content": "<p>office work</p>"},
    ]}
    recs = fetch_greenhouse_board("acme", client=_FakeClient(payload), title_include=["engineer"])
    assert len(recs) == 1 and "Kafka" in recs[0].title
    recs = fetch_greenhouse_board(
        "acme", client=_FakeClient(payload), title_exclude=["engineer", "manager"])
    assert recs == []


def test_ats_stack_signal_marks_veto():
    payload = {"jobs": [{"id": "a", "title": "E", "jobUrl": "https://x/1",
                         "isListed": True, "descriptionPlain": "cobol mainframe role"}]}
    (rec,) = fetch_ashby_org("acme", client=_FakeClient(payload), excluded_stack=["mainframe"])
    assert rec.metadata["stack_veto"] is True


# --- 4. typed claim filters -------------------------------------------------

def _clause(field, op, value):
    return FilterClause(field=field, op=FilterOp(op), value=value)


def test_filter_in_operator():
    assert _evaluate_filter({"fit_tier": "tier_2"}, ClaimFilter(all=[_clause("fit_tier", "in", ["tier_1", "tier_2"])])) is True
    assert _evaluate_filter({"fit_tier": "tier_3"}, ClaimFilter(all=[_clause("fit_tier", "in", ["tier_1", "tier_2"])])) is False
    assert _evaluate_filter({"fit_tier": "tier_3"}, ClaimFilter(all=[_clause("fit_tier", "not_in", ["tier_1", "tier_2"])])) is True
    assert _evaluate_filter({}, ClaimFilter(all=[_clause("fit_tier", "not_in", ["tier_1"])])) is True
    assert _evaluate_filter({}, ClaimFilter(all=[_clause("fit_tier", "in", ["tier_1"])])) is False


def test_filter_and_or_clauses():
    claims = {"score": 90, "passed": True, "fit_tier": "tier_1"}
    assert _evaluate_filter(claims, ClaimFilter(all=[_clause("score", ">=", 70), _clause("passed", "==", True)])) is True
    assert _evaluate_filter(claims, ClaimFilter(all=[_clause("score", ">=", 95), _clause("passed", "==", True)])) is False
    assert _evaluate_filter(claims, ClaimFilter(any=[
        ClaimFilter(all=[_clause("score", ">=", 95)]),
        ClaimFilter(all=[_clause("fit_tier", "==", "tier_1")]),
    ])) is True
    assert _evaluate_filter(claims, ClaimFilter(any=[
        ClaimFilter(all=[_clause("score", ">=", 95)]),
        ClaimFilter(all=[_clause("fit_tier", "==", "tier_3")]),
    ])) is False
    # AND-group plus OR branches conjoin: top-level all must pass too
    assert _evaluate_filter(
        {"a": 1, "c": 1},
        ClaimFilter(all=[_clause("a", "==", 1)], any=[ClaimFilter(all=[_clause("c", "==", 1)])]),
    ) is True


def test_filter_rejects_bad_shape():
    with pytest.raises(ValueError):
        FilterClause(field="score", op=">=", value=[80])
    with pytest.raises(ValueError):
        FilterClause(field="score", op="in", value=[])
    with pytest.raises(ValueError):
        FilterClause(field="no spaces", op="==", value=1)
    with pytest.raises(ValueError):
        ClaimFilter.model_validate({"all": [{"field": "score", "op": "like", "value": 1}]})


# --- 5. scoring hardening ----------------------------------------------------

def test_cost_ceiling_is_fail_closed_on_unknown_pricing(tmp_path):
    store = HarnessStore(tmp_path / "t.db")
    from harness_fleet.models import RoutePolicy

    routes = [
        {"id": "r/priced", "provider": "x", "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
        {"id": "r/unknown", "provider": "x", "cost_per_1k_input": None, "cost_per_1k_output": None},
    ]
    policy = RoutePolicy(max_cost_per_1k_input=1.0, max_cost_per_1k_output=1.0)
    ranked, _ = rank_with_scores(routes, store, policy=policy)
    assert ranked == ["r/priced"]


def test_latest_eval_wins_by_created_at_not_row_order(tmp_path):
    store = HarnessStore(tmp_path / "t.db")
    store.record_route_eval({
        "task_name": "t", "route_id": "r/a", "provider": "x",
        "total_samples": 4, "schema_pass_count": 4, "grounding_pass_count": 4,
        "correct_count": None, "rate_limit_count": 0, "error_count": 0,
        "avg_latency_seconds": 1.0, "composite_score": 0.95,
        "created_at": "2026-09-14T00:00:02Z",
    })
    store.record_route_eval({
        "task_name": "t", "route_id": "r/a", "provider": "x",
        "total_samples": 4, "schema_pass_count": 0, "grounding_pass_count": 0,
        "correct_count": None, "rate_limit_count": 0, "error_count": 4,
        "avg_latency_seconds": 1.0, "composite_score": 0.05,
        "created_at": "2026-09-14T00:00:01Z",
    })
    from harness_fleet.scoring import RouteScorer

    scores = RouteScorer(store).score_routes([{"id": "r/a", "provider": "x"}], task_name="t")
    assert scores["r/a"] > 0.5  # the newer 0.95 eval dominates, not the older 0.05


def test_legacy_complete_with_error_text_is_not_verified(tmp_path):
    from harness_fleet.store import HarnessStore

    store = HarnessStore(tmp_path / "t.db")
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO model_runs(
                receipt_id, run_id, batch_id, provider, requested_route, status,
                cost, cost_status, error, duration_seconds, receipt_json, created_at
            ) VALUES('r1', 'run', 'b', 'x', 'r/a', 'complete', 0, 'reported_zero', NULL, 1.0, '{}', '2026-09-09')"""
        )
        conn.execute(
            """INSERT INTO model_runs(
                receipt_id, run_id, batch_id, provider, requested_route, status,
                cost, cost_status, error, duration_seconds, receipt_json, created_at
            ) VALUES('r2', 'run', 'b', 'x', 'r/a', 'complete', 0, 'reported_zero', 'stale error text', 1.0, '{}', '2026-09-09')"""
        )
    stats = store.get_route_history_stats()
    assert stats["r/a"]["total"] > 0
    # Same age => same decay weight: exactly half the weight verifies.
    assert stats["r/a"]["completed"] == stats["r/a"]["total"] / 2


def test_backoff_delay_progression_and_clamp():
    from harness_fleet.store import HarnessStore

    backoff = HarnessStore.backoff_delay
    assert backoff(1) == 30.0
    assert backoff(2) == 60.0
    assert backoff(3) == 180.0
    assert backoff(4) == 600.0
    assert backoff(99) == 600.0
    assert backoff(0) == 30.0
    assert backoff(5, base_delays=[10.0, 20.0], max_delay=15.0) == 15.0


def test_empty_filter_matches_all():
    from harness_fleet.export import _evaluate_filter

    assert _evaluate_filter({"score": 5}, ClaimFilter()) is True
    assert _evaluate_filter({}, ClaimFilter()) is True


def test_route_evals_are_retention_bounded(tmp_path):
    from harness_fleet.store import HarnessStore

    store = HarnessStore(tmp_path / "t.db")

    def _eval(score, ts):
        return {
            "task_name": "t", "route_id": "r/a", "provider": "x",
            "total_samples": 1, "schema_pass_count": 1, "grounding_pass_count": 1,
            "correct_count": None, "rate_limit_count": 0, "error_count": 0,
            "avg_latency_seconds": 1.0, "composite_score": score,
            "created_at": ts,
        }

    for i in range(5):
        store.record_route_eval(_eval(0.1 * i, f"2026-09-14T00:00:0{i}Z"), keep_latest=2)
    rows = store.get_route_evals("t")
    assert len(rows) == 2
    assert sorted(r["composite_score"] for r in rows)[-1] > 0.39
    assert all(r["composite_score"] >= 0.2 for r in rows)


def test_tiebreak_is_deterministic_without_seed(tmp_path):
    store = HarnessStore(tmp_path / "t.db")
    routes = [{"id": f"r/{name}", "provider": "x"} for name in ("b", "a", "c")]
    first, _ = rank_with_scores(routes, store, seed="")
    second, _ = rank_with_scores(list(reversed(routes)), store, seed="")
    assert first == second


# --- provider payload extraction -------------------------------------------

def _task_prompt(prefix: str = "") -> str:
    from harness_fleet.models import TaskSpec

    task = TaskSpec(name="t", instructions="Do it.")
    payload = task.render_prompt([{
        "item_id": "i1",
        "title": "T",
        "sections": [{"slice_id": "full", "start": 0, "end": 10, "text": "0123456789"}],
    }])
    return f"{prefix}{payload}"


def test_extract_output_schema_ignores_profile_braces():
    from harness_fleet.providers.base import extract_task_payload
    from harness_fleet.providers.openai_compatible import (
        _extract_output_schema as oc_schema,
    )
    from harness_fleet.providers.openrouter import _extract_output_schema as or_schema

    profile = (
        'IDEAL COMPANY PROFILE: X (v1)\nScoring rubric: {"weights": {"a": 1}}'
        "\nApply this profile as the qualification context.\n\n"
    )
    prompt = _task_prompt(prefix=f"Instructions here.\n{profile}")
    assert extract_task_payload(prompt)["output_schema"]["required"] == ["items"]
    assert or_schema(prompt)["required"] == ["items"]
    assert oc_schema(prompt)["required"] == ["items"]


def test_blank_text_is_rejected_at_input_boundary(tmp_path):
    from harness_fleet.input_data import InputDataError, load_input_items

    blank = tmp_path / "blank.jsonl"
    blank.write_text('{"item_id": "b1", "text": "   "}\n')
    with pytest.raises(InputDataError, match="blank"):
        load_input_items(blank)


# --- worker guide ------------------------------------------------------------

def test_worker_guide_states_verifier_numbers():
    from harness_fleet.models import TaskSpec

    task = TaskSpec(
        name="g",
        min_quote_chars=21,
        claims_schema={
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "fit_tier": {"enum": ["tier_1", "tier_2", "tier_3", "unfit"]},
                "reason": {"type": "string", "description": "Short grounded reason"},
            },
            "required": ["score", "fit_tier", "reason"],
            "additionalProperties": False,
        },
    )
    guide = task.render_worker_guide()
    assert "at least 21 characters" in guide
    assert "more than once" in guide and "REQUIRED" in guide
    assert "tier_1 for 85-100" in guide and "unfit below 50" in guide
    assert "reason - Short grounded reason" in guide
    assert "Return JSON only." in guide
    assert "{" not in guide and "}" not in guide


def test_worker_guide_omits_tiers_without_score_fields():
    from harness_fleet.models import TaskSpec

    task = TaskSpec(
        name="plain",
        claims_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )
    assert "tier_1" not in task.render_worker_guide()


def test_every_preset_property_carries_a_worker_description():
    from harness_fleet.task import PRESETS, create_task_from_preset

    for preset_name in PRESETS:
        task = create_task_from_preset(f"t-{preset_name}", preset_name=preset_name)
        props = task.claims_schema["properties"]
        missing = [name for name, spec in props.items() if not spec.get("description")]
        assert not missing, f"preset '{preset_name}' lacks descriptions: {missing}"
        guide = task.render_worker_guide()
        for name in props:
            assert name in guide, f"preset '{preset_name}' field '{name}' missing from guide"


def test_bundled_example_tasks_carry_worker_descriptions():
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    # Examples are product-specific, so check the ones this distribution ships
    # rather than a fixed list copied from another product's repo.
    paths = sorted(repo.glob("examples/*/task.json")) + sorted(
        repo.glob("harness_fleet/resources/examples/*/task.json")
    )
    assert paths, "a distribution with example tasks must ship at least one"
    for path in paths:
        props = json.loads(path.read_text())["claims_schema"]["properties"]
        missing = [name for name, spec in props.items() if not spec.get("description")]
        assert not missing, f"{path} lacks descriptions: {missing}"


def test_account_research_preset_guide_names_tiers_and_gap():
    from harness_fleet.task import create_task_from_preset

    task = create_task_from_preset("research", preset_name="account-research")
    guide = task.render_worker_guide()
    assert "tier_1 for 85-100" in guide
    assert "identified_gap - " in guide


def test_render_prompt_embeds_guide_before_payload():
    from harness_fleet.models import TaskSpec
    from harness_fleet.providers.base import extract_task_payload

    task = TaskSpec(name="t", instructions="Do it.", min_quote_chars=15)
    prompt = task.render_prompt([{
        "item_id": "i1", "title": "T",
        "sections": [{"slice_id": "full", "start": 0, "end": 10, "text": "0123456789"}],
    }])
    assert "FIELD RULES" in prompt
    assert "Return JSON only." in prompt
    payload = extract_task_payload(prompt)
    assert payload is not None and payload["input_items"][0]["item_id"] == "i1"

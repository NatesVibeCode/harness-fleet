"""Deterministic calibration fitting: points, weights, and half-lives.

Fits refine the task's current calibration against labeled samples with a
fixed-order coordinate descent (no random init), so identical observations
always yield identical tables. Points stay on the simplex so tier bands
keep their meaning.
"""
import pytest

from harness_fleet.calibrate import (
    _project_simplex,
    collect_observations,
    fit_calibration,
    round_points_to_simplex,
)
from harness_fleet.task import create_task_from_preset


def _task():
    return create_task_from_preset("fit-demo", preset_name="score")


def _obs(answers, expected, uri=None, captured=None, supports=None):
    keys = [key for key, value in answers.items() if value is True]
    tags = supports if supports is not None else keys
    return {
        "item_id": "s",
        "answers": dict(answers),
        "quotes": [{"supports": list(tags), "text": "x" * 30}],
        "source_uri": uri,
        "captured_at": captured,
        "expected": float(expected),
    }


def _answersets():
    return [
        {"initiative_named": True, "criteria_evidence": False, "supporting_signals": False},
        {"initiative_named": False, "criteria_evidence": True, "supporting_signals": False},
        {"initiative_named": False, "criteria_evidence": False, "supporting_signals": True},
        {"initiative_named": True, "criteria_evidence": True, "supporting_signals": False},
        {"initiative_named": True, "criteria_evidence": False, "supporting_signals": True},
        {"initiative_named": False, "criteria_evidence": True, "supporting_signals": True},
        {"initiative_named": True, "criteria_evidence": True, "supporting_signals": True},
        {"initiative_named": False, "criteria_evidence": False, "supporting_signals": False},
        {"initiative_named": True, "criteria_evidence": True, "supporting_signals": True},
        {"initiative_named": True, "criteria_evidence": False, "supporting_signals": False},
    ]


# --- simplex machinery -------------------------------------------------------

def test_project_simplex_sums_and_clips():
    assert sum(_project_simplex([40.0, 35.0, 25.0])) == pytest.approx(100.0)
    projected = _project_simplex([120.0, -10.0, 5.0])
    assert sum(projected) == pytest.approx(100.0)
    assert all(value >= 0 for value in projected)
    assert _project_simplex([]) == []
    assert _project_simplex([40.0, 35.0, 25.0]) == _project_simplex([40.0, 35.0, 25.0])


def test_round_points_largest_remainder():
    rounded = round_points_to_simplex({"a": 33.33, "b": 33.33, "c": 33.34})
    assert sum(rounded.values()) == 100
    assert all(isinstance(value, int) for value in rounded.values())
    assert round_points_to_simplex({}) == {}
    assert sum(round_points_to_simplex({"a": 70.0, "b": 30.0}).values()) == 100


# --- points recovery ---------------------------------------------------------

def test_fit_recovers_known_points():
    task = _task()
    truth = {"initiative_named": 70.0, "criteria_evidence": 20.0, "supporting_signals": 10.0}
    observations = [
        _obs(answers, sum(truth[key] for key, value in answers.items() if value))
        for answers in _answersets()
    ]
    scored_at = "2026-09-01T00:00:00+00:00"
    fit = fit_calibration(task, observations, scored_at, kinds=("points",))
    assert fit["fitted_train"]["mse"] < 1e-6
    assert fit["fitted_train"]["mse"] < fit["baseline"]["mse"]
    for key, value in truth.items():
        assert abs(fit["points_float"][key] - value) < 2.0
    assert sum(fit["points"].values()) == 100
    assert fit["n_train"] == 8 and fit["n_holdout"] == 2


def test_fit_is_deterministic():
    task = _task()
    observations = [_obs(answers, 50.0) for answers in _answersets()]
    scored_at = "2026-09-01T00:00:00+00:00"
    first = fit_calibration(task, observations, scored_at)
    second = fit_calibration(task, observations, scored_at)
    assert first == second


def test_fit_respects_bounds_and_kinds():
    task = _task()
    task.source_weights = {"good.example": 0.9, "bad.example": 0.1}
    task.recency_half_lives = {"supporting_signals": 30.0}
    observations = [_obs(answers, 60.0) for answers in _answersets()]
    scored_at = "2026-09-01T00:00:00+00:00"
    fit = fit_calibration(task, observations, scored_at)
    assert all(0 <= value <= 100 for value in fit["points"].values())
    assert all(0.0 <= value <= 1.0 for value in fit["weights"].values())
    assert 0.0 <= fit["default_source_weight"] <= 1.0
    assert all(0.5 <= value <= 7300.0 for value in fit["halves"].values())
    points_only = fit_calibration(task, observations, scored_at, kinds=("points",))
    assert points_only["weights"] == {"good.example": 0.9, "bad.example": 0.1}
    with pytest.raises(ValueError, match="unknown param kind"):
        fit_calibration(task, observations, scored_at, kinds=("nope",))


# --- weights and halves recovery ---------------------------------------------

def test_fit_recovers_source_weights():
    import math as _math

    task = _task()
    # Both sources must be ones the evidence bar allows, or no weight can be
    # recovered: a disallowed domain is not evidence at any weight.
    task.source_weights = {"boards.greenhouse.io": 0.5, "jobs.lever.co": 0.5}
    observations = []
    for index, answers in enumerate(_answersets()):
        uri = "https://boards.greenhouse.io/good/x" if index % 2 == 0 else "https://jobs.lever.co/bad/y"
        weight = 1.0 if index % 2 == 0 else 0.3
        # Labels live on the rubric scale: derive rounds halves up, caps at 100.
        expected = min(100, _math.floor(sum(
            task.checklist[key] * weight for key, value in answers.items() if value
        ) + 0.5))
        observations.append(_obs(answers, expected, uri=uri))
    scored_at = "2026-09-01T00:00:00+00:00"
    fit = fit_calibration(task, observations, scored_at, kinds=("weights",))
    assert fit["fitted_train"]["mse"] < 1e-4
    assert fit["weights"]["jobs.lever.co"] < fit["weights"]["boards.greenhouse.io"]
    assert abs(fit["weights"]["jobs.lever.co"] - 0.3) < 0.1


def test_fit_recovers_half_life_direction():
    task = _task()
    task.recency_half_lives = {"supporting_signals": 200.0}
    scored_at = "2026-06-01T00:00:00+00:00"
    observations = []
    for answers in _answersets():
        decay = 0.5 ** (60.0 / 20.0) if answers["supporting_signals"] else 1.0
        expected = (
            (40.0 if answers["initiative_named"] else 0.0)
            + (35.0 if answers["criteria_evidence"] else 0.0)
            + (25.0 * decay if answers["supporting_signals"] else 0.0)
        )
        observations.append(_obs(
            answers, expected, captured="2026-04-02T00:00:00+00:00",
        ))
    fit = fit_calibration(task, observations, scored_at, kinds=("halves",))
    assert fit["fitted_train"]["mse"] < fit["baseline"]["mse"] / 10
    assert fit["halves"]["supporting_signals"] < 100.0


# --- errors ------------------------------------------------------------------

def test_fit_rejects_bad_inputs():
    task = _task()
    with pytest.raises(ValueError, match="at least 2"):
        fit_calibration(task, [_obs(_answersets()[0], 10.0)], "2026-09-01T00:00:00+00:00")
    from harness_fleet.models import TaskSpec

    with pytest.raises(ValueError, match="needs a task with a checklist"):
        fit_calibration(TaskSpec(name="plain"), [], "2026-09-01T00:00:00+00:00")


# --- observation collection --------------------------------------------------

def test_collect_observations_counts_skips(tmp_path):
    import json as _json

    from harness_fleet.models import InputItem

    task = _task()

    def run_prompt(route_id, prompt, system_prompt=None, policy=None):
        quote = "Acme is migrating its platform to Kubernetes this quarter."
        payload = {"items": [{
            "item_id": "s1",
            "claims": {
                "checklist": {"initiative_named": True, "criteria_evidence": False,
                              "supporting_signals": False},
                "reason": "migration named",
            },
            "quotes": [{"slice_id": "full", "supports": ["initiative_named"], "text": quote}],
        }]}
        receipt = {"id": "r", "provider": "stub", "requested_route": route_id,
                   "status": "complete", "cost": 0.0, "cost_status": "reported_zero"}
        return True, _json.dumps(payload), receipt

    text = "Acme is migrating its platform to Kubernetes this quarter. Padding to reach length."
    samples = [
        InputItem(item_id="s1", text=text, metadata={"score": 40}),
        InputItem(item_id="s2", text=text, metadata={"score": "high"}),
        InputItem(item_id="s3", text=text, metadata={}),
    ]
    observations, skipped, scored_at = collect_observations(task, samples, "score", run_prompt, "stub/r")
    assert len(observations) == 1
    assert observations[0]["answers"]["initiative_named"] is True
    assert observations[0]["expected"] == 40.0
    assert skipped.get("non_numeric_label", 0) == 2
    assert scored_at


def test_cli_calibrate_dry_run_and_apply(tmp_path, monkeypatch, capsys):
    """CLI wiring: the demo rater always answers all-true, so rubric-scale
    labels (100) fit exactly and idempotently; error paths fail closed."""
    import json as _json
    from argparse import Namespace

    import pytest

    from harness_fleet import cli
    from harness_fleet.catalog import RouteCatalog
    from harness_fleet.store import HarnessStore

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(
        name="cal-demo", preset="score", batch_size=4,
        source_weight=[], half_life=[],
        sample=str(tmp_path / "sample.jsonl"), db=str(db), json=True,
    ))
    capsys.readouterr()
    RouteCatalog(db_path=db).add_route(
        "demo/fake", provider="demo",
        cost_per_1k_input=0.0, cost_per_1k_output=0.0,
        enabled=True, price_state="price_observed_zero",
    )
    labeled = tmp_path / "labeled.jsonl"
    with open(labeled, "w", encoding="utf-8") as fh:
        for index in range(6):
            fh.write(_json.dumps({
                "item_id": f"s{index}",
                "text": f"Acme initiative {index} migrates billing to Kafka with verified stack evidence present.",
                "metadata": {"score": 100},
            }) + "\n")
    base_ns = dict(
        task="cal-demo", input=str(labeled), expected="score", route="demo/fake",
        params="points", max_sweeps=20, apply=False,
        id_column=None, text_column=None, title_column=None, uri_column=None,
        only_ids=None, only_ids_fuzzy=False, workspace_root=".", db=str(db), json=True,
    )
    store = HarnessStore(db)
    before = store.current_task_revision("cal-demo")
    cli.cmd_calibrate(Namespace(**base_ns))
    dry = _json.loads(capsys.readouterr().out)
    assert dry["applied"] is False and dry["new_revision"] is None
    assert dry["fitted_train"]["mae"] == 0.0 and dry["baseline"]["mae"] == 0.0
    assert dry["n_train"] + dry["n_holdout"] == 6
    assert store.current_task_revision("cal-demo") == before, "dry run must not mutate"
    cli.cmd_calibrate(Namespace(**dict(base_ns, apply=True)))
    applied = _json.loads(capsys.readouterr().out)
    assert applied["applied"] is True and applied["new_revision"] == before
    assert sum(store.get_task("cal-demo").checklist.values()) == 100
    with pytest.raises(ValueError, match="single rater route"):
        cli.cmd_calibrate(Namespace(**dict(base_ns, route=None)))
    with pytest.raises(ValueError, match="unknown route"):
        cli.cmd_calibrate(Namespace(**dict(base_ns, route="nope/missing")))
    with pytest.raises(ValueError, match="no checklist"):
        cli.cmd_init(Namespace(
            name="plain-demo", preset="summarize", batch_size=4,
            source_weight=[], half_life=[],
            sample=str(tmp_path / "plain.jsonl"), db=str(db), json=True,
        ))
        capsys.readouterr()
        cli.cmd_calibrate(Namespace(**dict(base_ns, task="plain-demo")))

"""Which live-ping failures are the machine's, and which are our wiring.

The probe skips conditions a machine explains and fails the rest, so a recipe
that names a model must never inherit the "stale machine config" skip.
"""
from types import SimpleNamespace

import pytest

from tests import test_harness_live as live_probe
from tests.test_harness_live import _explained_by_the_machine


class _RecipeWithoutModel:
    def build_argv(self, *, model, prompt, prompt_file):
        return ["-p", "--output-format", "json"]


class _RecipeWithModel:
    def build_argv(self, *, model, prompt, prompt_file):
        return ["-p", "--model", model, "--output-format", "json"]


def _receipt(error_type, error):
    return SimpleNamespace(error_type=error_type, error=error)


def test_a_stale_configured_model_is_the_machines_problem():
    """Our recipe named no model, so the CLI's own config chose the rejected one."""
    receipt = _receipt(
        "inference_error",
        '[claude-code:unrecognized_model] {"model":"z-ai/glm-5.3-flash"}',
    )
    assert _explained_by_the_machine(_RecipeWithoutModel, receipt) is True


def test_a_model_we_named_is_still_our_bug():
    """The same error, but this recipe passes a model: our wiring, so it fails."""
    receipt = _receipt(
        "inference_error",
        '[claude-code:unrecognized_model] {"model":"harness-fleet-probe-model"}',
    )
    assert _explained_by_the_machine(_RecipeWithModel, receipt) is False


def test_ordinary_environment_failures_still_skip():
    for kind in ("auth_error", "rate_limit", "timeout", "transient_http"):
        assert _explained_by_the_machine(_RecipeWithModel, _receipt(kind, "boom")) is True


def test_a_real_inference_regression_never_skips():
    """A bad answer with a healthy model stays a failure in either recipe."""
    receipt = _receipt("inference_error", "model returned an unparseable receipt")
    assert _explained_by_the_machine(_RecipeWithoutModel, receipt) is False
    assert _explained_by_the_machine(_RecipeWithModel, receipt) is False


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_live_probe_requires_explicit_authorization(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("HARNESS_FLEET_LIVE_TESTS", raising=False)
    else:
        monkeypatch.setenv("HARNESS_FLEET_LIVE_TESTS", value)

    def unexpected_lookup(binary):
        pytest.fail("Unauthorized probe attempted binary discovery")

    monkeypatch.setattr(live_probe.shutil, "which", unexpected_lookup)
    with pytest.raises(pytest.skip.Exception, match="authorize live inference"):
        live_probe.test_live_ping("stub", "stub", None, "stub/ping")


def test_live_probe_runs_when_explicitly_authorized(monkeypatch):
    monkeypatch.setenv("HARNESS_FLEET_LIVE_TESTS", "1")
    monkeypatch.setattr(live_probe.shutil, "which", lambda binary: "/stub")
    calls = []

    def run_prompt(route_id, prompt, timeout_sec):
        calls.append((route_id, prompt, timeout_sec))
        return True, "PING", SimpleNamespace(provider="stub")

    provider = SimpleNamespace(run_prompt=run_prompt)
    live_probe.test_live_ping("stub", "stub", lambda: provider, "stub/ping")
    assert calls == [("stub/ping", "Reply with exactly: PING", 120)]

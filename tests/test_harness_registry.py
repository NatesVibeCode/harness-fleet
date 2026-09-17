"""Explicit-only provider resolution, availability matrix, doctor, refresh shape."""
import json

import pytest

from harness_fleet.catalog import RouteCatalog
from harness_fleet.providers.registry import (
    HARNESS_SPECS,
    ProviderRegistry,
    ProviderResolutionError,
    configured_routes,
)


def test_seven_equal_harness_providers_registered():
    registry = ProviderRegistry()
    assert [spec.name for spec in HARNESS_SPECS] == [
        "opencode", "claude", "codex", "cursor", "grok", "muse", "antigravity",
    ]
    for spec in HARNESS_SPECS:
        assert registry.get(spec.name) is not None


def test_explicit_resolve_all_seven():
    registry = ProviderRegistry()
    for spec in HARNESS_SPECS:
        assert registry.resolve(spec.name) is registry.get(spec.name)


def test_unknown_provider_fails_closed_with_available_names():
    registry = ProviderRegistry()
    for bad in (None, "", "nope", "opencode/anthropic/claude"):
        with pytest.raises(ProviderResolutionError) as exc_info:
            registry.resolve(bad)
        for spec in HARNESS_SPECS:
            assert spec.name in str(exc_info.value)


def test_resolve_accepts_route_id_by_provider():
    from harness_fleet.models import RouteId

    registry = ProviderRegistry()
    assert registry.resolve(RouteId.parse("codex/some-model")) is registry.get("codex")
    with pytest.raises(ProviderResolutionError):
        registry.resolve(RouteId(provider="nope", model="x"))


def test_no_prefix_substring_or_default_resolution():
    registry = ProviderRegistry()
    # Route ids never dispatch: only the explicit provider field resolves.
    with pytest.raises(ProviderResolutionError):
        registry.resolve(None)
    with pytest.raises(ProviderResolutionError):
        registry.resolve("openrouter-free-zone")


def test_configured_routes_matrix_is_binary_on_path_for_harnesses(monkeypatch):
    import harness_fleet.providers.harness as harness_module

    present = {"opencode", "cursor-agent"}
    monkeypatch.setattr(harness_module.shutil, "which", lambda name: f"/bin/{name}" if name in present else None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    routes = [
        {"id": "opencode/m", "provider": "opencode"},
        {"id": "claude/m", "provider": "claude"},
        {"id": "cursor/m", "provider": "cursor"},
        {"id": "openrouter/m", "provider": "openrouter"},
        {"id": "demo/fake", "provider": "demo"},
        {"id": "x/y", "provider": "unknown"},
    ]
    ids = {route["id"] for route in configured_routes(routes)}
    assert ids == {"opencode/m", "cursor/m"}


def test_doctor_reports_per_harness_matrix(tmp_path, monkeypatch, capsys):
    import shutil as _shutil

    from harness_fleet.cli import build_parser, cmd_doctor

    monkeypatch.setattr(_shutil, "which", lambda name: f"/bin/{name}" if name == "opencode" else None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    args = build_parser().parse_args(["doctor", "--workspace-root", str(tmp_path), "--json"])
    with pytest.raises(SystemExit):
        cmd_doctor(args)
    report = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in report["checks"]}
    for spec in HARNESS_SPECS:
        assert spec.name in names
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["opencode"]["ok"] is True
    assert by_name["claude"]["ok"] is False
    assert "not found in PATH" in by_name["claude"]["detail"]


def test_refresh_all_shape_with_discovery_zeros(tmp_path, monkeypatch):
    import harness_fleet.catalog as catalog_module

    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: None)

    class NoNet:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(catalog_module.httpx, "Client", NoNet)
    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")
    results = catalog.refresh_all()
    for spec in HARNESS_SPECS:
        assert spec.name in results or f"{spec.name}_error" in results
    # Harnesses without a models command always contribute zero.
    for name in ("claude", "codex", "muse"):
        assert results[name] == 0
    # Harnesses with one either discover or report why they could not.
    for name in ("opencode", "cursor", "grok", "antigravity"):
        assert name in results or f"{name}_error" in results
    assert "opencode_error" in results


def test_refresh_from_harness_cursor_discovers_candidates(tmp_path, monkeypatch):
    import subprocess as _sp

    import harness_fleet.catalog as catalog_module
    from harness_fleet.providers.cursor import CURSOR_SPEC

    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/bin/cursor-agent")

    def fake_run(argv, capture_output, text, timeout, env=None):
        assert argv[:2] == ["cursor-agent", "models"]
        return _sp.CompletedProcess(argv, 0, stdout=json.dumps([{"id": "pro-1"}]), stderr="")

    monkeypatch.setattr(catalog_module.subprocess, "run", fake_run)
    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")
    assert catalog.refresh_from_harness(CURSOR_SPEC) == 1
    routes = {route["id"]: route for route in catalog.data["routes"] if route["provider"] == "cursor"}
    assert routes["cursor/pro-1"]["price_state"] == "candidate"
    assert routes["cursor/pro-1"]["enabled"] is False


def test_refresh_from_harness_discovery_less_is_zero():
    from harness_fleet.providers.claude import CLAUDE_SPEC

    catalog = RouteCatalog.__new__(RouteCatalog)
    assert catalog.refresh_from_harness(CLAUDE_SPEC) == 0


def test_harness_refresh_records_zero_cost_for_observed_zero_routes(tmp_path, monkeypatch):
    """An admitted harness route must carry the pricing evidence it was admitted on.

    The harness refresh used to set price_state=price_observed_zero without
    writing cost_per_1k_*, leaving routes advertised as verified-free whose cost
    fields read None -- indistinguishable from unpriced.
    """
    import subprocess as _sp

    import harness_fleet.catalog as catalog_module
    from harness_fleet.providers.opencode import OPENCODE_SPEC

    monkeypatch.setattr(catalog_module.shutil, "which", lambda name: "/bin/opencode")

    # Shape taken from `opencode models --verbose`: an explicit all-zero cost
    # block, which is what earns the observed-zero state.
    payload = "\n".join([
        "opencode/ling-3.0-flash-fin-free",
        json.dumps({
            "id": "ling-3.0-flash-fin-free", "status": "active",
            "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}},
        }),
        "opencode/paid-model",
        json.dumps({
            "id": "paid-model", "status": "active",
            "cost": {"input": 0.0000002, "output": 0.0000011},
        }),
    ])

    def fake_run(argv, capture_output, text, timeout, env=None):
        return _sp.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(catalog_module.subprocess, "run", fake_run)
    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")
    catalog.refresh_from_harness(OPENCODE_SPEC)

    routes = {r["id"]: r for r in catalog.data["routes"] if r["provider"] == "opencode"}
    admitted = routes["opencode/ling-3.0-flash-fin-free"]
    assert admitted["price_state"] == "price_observed_zero"
    assert admitted["enabled"] is True
    assert admitted["cost_per_1k_input"] == 0.0
    assert admitted["cost_per_1k_output"] == 0.0

    # A paid model is never admitted from discovery, so it cannot inherit a
    # zero cost figure either.
    assert "opencode/paid-model" not in routes

    # The invariant: a recorded 0.0 cost IS verified zero pricing, nothing else.
    zero_cost = {
        r["id"]: r for r in catalog.data["routes"] if r.get("cost_per_1k_input") == 0.0
    }
    assert set(zero_cost) == {"opencode/ling-3.0-flash-fin-free"}
    assert all(r["price_state"] == "price_observed_zero" for r in zero_cost.values())

    # An unverified packaged hint keeps its CANDIDATE label but no price claim:
    # the hint decides candidate-vs-unknown, it is not evidence. (An openrouter
    # hint, since the opencode refresh above legitimately retires opencode
    # routes missing from its own response.)
    hint = next(r for r in catalog.data["routes"] if r["id"] == "openrouter/minimax/minimax-01:free")
    assert hint["price_state"] == "candidate"
    assert hint["enabled"] is False
    assert hint.get("cost_per_1k_input") is None
    assert hint.get("cost_per_1k_output") is None


def test_retired_routes_lose_their_stale_zero_cost(tmp_path, monkeypatch):
    """A ':free' variant the provider retired must not keep advertising 0.0.

    Exercised through the real refresh branch: the response no longer lists the
    model, so its zero-pricing evidence is stale and must be dropped.
    """
    import harness_fleet.catalog as catalog_module

    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")
    catalog.data["routes"].append({
        "id": "openrouter/vendor/retired-model:free",
        "provider": "openrouter",
        "enabled": True,
        "price_state": "price_observed_zero",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
    })
    catalog.save()

    class _Response:
        status_code = 200
        def json(self):
            # The provider still serves other free models, but not this one.
            return {"data": [{
                "id": "vendor/live-model:free",
                "pricing": {"prompt": "0", "completion": "0"},
            }]}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def get(self, url, **k): return _Response()

    monkeypatch.setattr(catalog_module.httpx, "Client", _Client)
    catalog.refresh_from_openrouter()

    routes = {r["id"]: r for r in catalog.data["routes"]}
    retired = routes["openrouter/vendor/retired-model:free"]
    assert retired["price_state"] == "unknown"
    assert retired["enabled"] is False
    assert "not present" in retired["disabled_reason"]
    # The stale zero is gone, so nothing can read it as still verified-free.
    assert retired.get("cost_per_1k_input") is None
    assert retired.get("cost_per_1k_output") is None

    # The model that IS still listed keeps its earned zero-pricing evidence.
    live = routes["openrouter/vendor/live-model:free"]
    assert live["price_state"] == "price_observed_zero"
    assert live["enabled"] is True
    assert live["cost_per_1k_input"] == 0.0


def test_unverified_hint_with_zero_costs_is_not_treated_as_free():
    """A ':free' name plus packaged 0.0 is a hint, never pricing evidence.

    The predicate is the safety net behind the paid-route re-approval guard and
    the circuit breaker, so a candidate must not read as verified-free just
    because a seed declared zero costs for it.
    """
    from harness_fleet.catalog import PriceState, is_observed_zero_price_route

    hint = {
        "id": "openrouter/vendor/model:free",
        "price_state": PriceState.CANDIDATE.value,
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
    }
    assert is_observed_zero_price_route(hint) is False

    for state in (PriceState.UNKNOWN.value, PriceState.DISABLED.value):
        assert is_observed_zero_price_route({**hint, "price_state": state}) is False

    # Only the observation marker counts.
    assert is_observed_zero_price_route(
        {**hint, "price_state": PriceState.PRICE_OBSERVED_ZERO.value}
    ) is True

    # Any non-zero figure disqualifies a route regardless of its label.
    assert is_observed_zero_price_route({
        "price_state": PriceState.PRICE_OBSERVED_ZERO.value,
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.5,
    }) is False

    # Legacy rows written before price_state existed still resolve via costs.
    assert is_observed_zero_price_route(
        {"cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0}
    ) is True
    assert is_observed_zero_price_route(
        {"cost_per_1k_input": None, "cost_per_1k_output": None}
    ) is False


def test_zero_priced_audio_model_is_never_admitted(tmp_path, monkeypatch):
    """Genuinely free but non-text output: cheap is not the same as usable.

    OpenRouter reports 0/0 for two Lyria preview models whose output is audio,
    so price evidence alone would put them in the ladder a text batch routes
    through.
    """
    import harness_fleet.catalog as catalog_module
    from harness_fleet.catalog import supports_text_completion

    assert supports_text_completion({"architecture": {"output_modalities": ["text"]}}) is True
    assert supports_text_completion({"architecture": {"output_modalities": ["text", "audio"]}}) is False
    # Missing modality metadata must fail open, not drop routes.
    assert supports_text_completion({}) is True
    assert supports_text_completion({"architecture": {}}) is True
    assert supports_text_completion({"architecture": {"output_modalities": []}}) is True

    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")

    class _Response:
        status_code = 200
        def json(self):
            return {"data": [
                {"id": "google/lyria-3-pro-preview",
                 "architecture": {"output_modalities": ["text", "audio"]},
                 "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "vendor/real-text-model:free",
                 "architecture": {"output_modalities": ["text"]},
                 "pricing": {"prompt": "0", "completion": "0"}},
            ]}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def get(self, url, **k): return _Response()

    monkeypatch.setattr(catalog_module.httpx, "Client", _Client)
    catalog.refresh_from_openrouter()

    routes = {r["id"]: r for r in catalog.data["routes"]}
    assert "openrouter/google/lyria-3-pro-preview" not in routes
    assert "openrouter/google/lyria-3-pro-preview" not in {r["id"] for r in catalog.get_routes(free_only=True)}

    text_model = routes["openrouter/vendor/real-text-model:free"]
    assert text_model["price_state"] == "price_observed_zero"
    assert text_model["enabled"] is True


def test_an_admitted_audio_route_is_disabled_by_the_next_refresh(tmp_path, monkeypatch):
    """A database seeded before the guard existed must be repaired, not left usable."""
    import harness_fleet.catalog as catalog_module

    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")
    catalog.data["routes"].append({
        "id": "openrouter/google/lyria-3-clip-preview",
        "provider": "openrouter",
        "enabled": True,
        "price_state": "price_observed_zero",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
    })
    catalog.save()
    assert "openrouter/google/lyria-3-clip-preview" in {
        r["id"] for r in catalog.get_routes(free_only=True)
    }

    class _Response:
        status_code = 200
        def json(self):
            return {"data": [{
                "id": "google/lyria-3-clip-preview",
                "architecture": {"output_modalities": ["text", "audio"]},
                "pricing": {"prompt": "0", "completion": "0"},
            }]}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def get(self, url, **k): return _Response()

    monkeypatch.setattr(catalog_module.httpx, "Client", _Client)
    catalog.refresh_from_openrouter()

    route = {r["id"]: r for r in catalog.data["routes"]}["openrouter/google/lyria-3-clip-preview"]
    assert route["enabled"] is False
    assert route["price_state"] == "unknown"
    assert "not text" in route["disabled_reason"]
    assert route.get("cost_per_1k_input") is None
    assert route["id"] not in {r["id"] for r in catalog.get_routes(free_only=True)}

"""A route that will not answer is parked, so the ladder moves on."""
from __future__ import annotations

from harness_fleet.catalog import RouteCatalog
from harness_fleet.engine import Engine
from harness_fleet.models import ProviderReceipt, RoutePolicy
from harness_fleet.packer import pack_items
from harness_fleet.store import HarnessStore
from harness_fleet.task import create_task_from_preset


class _HangingProvider:
    """Answers the way a hung CLI does: the prompt timeout, every time."""

    def __init__(self, error_type: str = "timeout"):
        self.error_type = error_type
        self.calls = 0

    def run_prompt(self, route_id, prompt, system_prompt=None, timeout_sec=120, session_id=None, policy=None):
        self.calls += 1
        return False, None, ProviderReceipt(
            id="receipt-1", provider="stub", requested_route=route_id,
            status="failed", cost=0.0, cost_status="reported_zero", usage={"total_tokens": 0},
            error="Command timed out after 180s", error_type=self.error_type,  # type: ignore[arg-type]
            duration_seconds=180.0,
        )


def _engine(tmp_path, provider, route_id="stub/slow"):
    # In production the control plane and the catalog are one database; a cooldown
    # written by the engine must be visible to the store that ranks routes.
    store = HarnessStore(tmp_path / "state.db")
    catalog = RouteCatalog(db_path=store.path)
    catalog.add_route(
        route_id=route_id, provider="stub", cost_per_1k_input=0.0, cost_per_1k_output=0.0,
        enabled=True, price_state="price_observed_zero", verification_source="test",
    )
    task = create_task_from_preset("t", preset_name="classify")
    engine = Engine(task, catalog=catalog, store=store, policy=RoutePolicy(allowed_routes=[route_id]))
    engine.registry.register("stub", provider)
    return engine, catalog, store


def test_a_route_that_times_out_is_parked(tmp_path):
    provider = _HangingProvider("timeout")
    engine, _catalog, store = _engine(tmp_path, provider)
    engine.execute_batch(pack_items([{"item_id": "i1", "text": "text"}])[0], run_id="r1")

    cooldowns = store.get_active_cooldowns()
    assert "stub/slow" in cooldowns, "a hanging route must not be retried at once"
    assert cooldowns["stub/slow"] > 0


def test_a_refused_route_is_parked_too(tmp_path):
    provider = _HangingProvider("auth_error")
    engine, _catalog, store = _engine(tmp_path, provider)
    engine.execute_batch(pack_items([{"item_id": "i1", "text": "text"}])[0], run_id="r1")
    assert "stub/slow" in store.get_active_cooldowns()


def test_an_ordinary_inference_error_does_not_park_the_route(tmp_path):
    """Only the failures that retrying cannot fix park a route."""
    provider = _HangingProvider("inference_error")
    engine, _catalog, store = _engine(tmp_path, provider)
    engine.execute_batch(pack_items([{"item_id": "i1", "text": "text"}])[0], run_id="r1")
    assert "stub/slow" not in store.get_active_cooldowns()

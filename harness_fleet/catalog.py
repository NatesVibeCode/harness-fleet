"""Route catalog and dynamic circuit-breaker management with automated free-schema discovery."""
import json
import re
import shutil
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from .models import RouteInfo
from .store import HarnessStore

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "routes.seed.json"

class RouteCircuitBreaker(Exception):  # noqa: N818
    pass

class PriceState(str, Enum):
    CANDIDATE = "candidate"
    PRICE_OBSERVED_ZERO = "price_observed_zero"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


def is_observed_zero_price_route(route: dict[str, Any]) -> bool:
    """Return whether a route has explicit zero-price evidence.

    ``price_state`` is the evidence marker, so an unverified hint is not
    evidence: a ``candidate``/``unknown``/``disabled`` route stays unpaid-for
    even when a packaged seed declared ``0.0`` costs for it, because that
    declaration comes from the model *name*, not from an observation. Treating
    it as evidence let a ':free' name defeat the paid-route re-approval guard
    and the circuit breaker. The cost fallback is only for legacy rows written
    before ``price_state`` existed.
    """
    declared_costs = (route.get("cost_per_1k_input"), route.get("cost_per_1k_output"))
    if any(cost is not None and cost != 0.0 for cost in declared_costs):
        return False
    state = route.get("price_state")
    if state is not None:
        return state == PriceState.PRICE_OBSERVED_ZERO.value
    return all(cost is not None and cost == 0.0 for cost in declared_costs)


def _observed_prices(model_data: dict) -> list[float] | None:
    pricing = model_data.get("pricing") or model_data.get("cost")
    if isinstance(pricing, dict):
        input_value = pricing.get("prompt", pricing.get("input"))
        output_value = pricing.get("completion", pricing.get("output"))
        if input_value is None or output_value is None:
            return None
        raw_values: list[Any] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    collect(nested)
            elif value is not None:
                raw_values.append(value)

        collect(pricing)
        try:
            return [float(value) for value in raw_values]
        except (TypeError, ValueError):
            return None
    return None


def classify_price_state(model_data: dict) -> PriceState:
    """Classify evidence without treating marketing text as observed pricing."""
    prices = _observed_prices(model_data)
    if prices is not None:
        return PriceState.PRICE_OBSERVED_ZERO if all(value == 0 for value in prices) else PriceState.UNKNOWN
    schema_dump = json.dumps(model_data).lower()
    if re.search(r"(\bfree\b|:free|-free|_free)", schema_dump):
        return PriceState.CANDIDATE
    return PriceState.UNKNOWN


def is_free_in_schema(model_data: dict) -> bool:
    """Compatibility predicate: true only for explicit observed zero pricing."""
    return classify_price_state(model_data) is PriceState.PRICE_OBSERVED_ZERO


def supports_text_completion(model_data: dict) -> bool:
    """False only when the model's *output* is known to include a non-text modality.

    OpenRouter genuinely reports zero pricing for two Lyria preview models whose
    output is audio. They can never satisfy a text-inference task contract, so
    price evidence alone must not put them in the free-route ladder. Fails open:
    with no modality metadata (CLI harness model listings carry none) the model
    is kept, because missing evidence is not evidence of unsuitability.
    """
    architecture = model_data.get("architecture")
    if not isinstance(architecture, dict):
        return True
    modalities = architecture.get("output_modalities")
    if not isinstance(modalities, list) or not modalities:
        return True
    return set(modalities) <= {"text"}


def _apply_price_evidence(route: dict, price_state: PriceState) -> None:
    """Keep the stored cost fields consistent with the route's price state.

    ``add_route`` refuses ``price_observed_zero`` alongside non-zero declared
    costs and only derives that state from explicit ``0.0``/``0.0``, so the rest
    of the catalog expects the two to agree. The harness refresh path used to
    set the state without recording any cost, leaving a route advertised as
    verified-free while its cost fields read ``None`` (indistinguishable from
    unpriced). An observed price of zero is zero in any unit, so the per-1k
    figures are exactly ``0.0``.
    """
    if price_state is PriceState.PRICE_OBSERVED_ZERO:
        route["cost_per_1k_input"] = 0.0
        route["cost_per_1k_output"] = 0.0
    else:
        route.pop("cost_per_1k_input", None)
        route.pop("cost_per_1k_output", None)


def _admissible_opencode_model(header: str, spec_name: str) -> bool:
    """Which of opencode's models become fleet routes: the free ones.

    opencode's registry lists its own models and the OpenRouter models it
    mediates. The ones we run are the ones that say free in the name — the
    default install needs no key for either family. Everything else in that
    registry (paid models, other vendors' endpoints) is added deliberately with
    `routes add`, never discovered implicitly.
    """
    if "/" not in header or " " in header:
        return False
    return "free" in header.lower()


class RouteCatalog:
    def __init__(self, config_path: Path | str | None = None, db_path: Path | str | None = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        resolved_db = db_path or (self.config_path.with_suffix(".db") if config_path else None)
        self.store = HarnessStore(resolved_db)
        if self.store.route_count() == 0:
            self._seed_from_json()
        self.data = self._load()

    def _seed_from_json(self) -> None:
        """Import packaged route hints without treating bundled history as local evidence."""
        if not self.config_path.exists():
            return
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        for raw_route in data.get("routes", []):
            route = dict(raw_route)
            hinted_zero = (
                route.get("price_state") == PriceState.PRICE_OBSERVED_ZERO.value
                or (route.get("cost_per_1k_input") == 0 and route.get("cost_per_1k_output") == 0)
            )
            route["enabled"] = False
            route["price_state"] = PriceState.CANDIDATE.value if hinted_zero else PriceState.UNKNOWN.value
            route["last_verified"] = None
            route["verification_source"] = "packaged route hint; refresh required"
            route.pop("zero_price_verified", None)
            # The hint only decides CANDIDATE vs UNKNOWN. It is not pricing
            # evidence, so the hinted zeros must not be stored as though they
            # had been observed -- otherwise the row claims 0.0 while its own
            # state says "refresh required".
            _apply_price_evidence(route, PriceState(route["price_state"]))
            self.store.upsert_route(RouteInfo.model_validate(route))

    def _load(self) -> dict:
        routes = self.store.list_routes(observed_zero_only=False, include_disabled=True)
        return {"revision": 2, "routes": [route.model_dump(mode="json") for route in routes]}

    def save(self):
        with self._lock:
            for raw_route in self.data.get("routes", []):
                self.store.upsert_route(RouteInfo.model_validate(raw_route))

    def get_routes(
        self,
        provider: str | None = None,
        free_only: bool = True,
        include_disabled: bool = False,
    ) -> list[dict]:
        routes = self.data.get("routes", [])
        matched = []
        for r in routes:
            if not include_disabled and not r.get("enabled", False):
                continue
            if provider and r.get("provider") != provider:
                continue
            if free_only and not is_observed_zero_price_route(r):
                continue
            matched.append(r)
        return matched

    def add_route(
        self,
        route_id: str,
        provider: str,
        cost_per_1k_input: float | None = None,
        cost_per_1k_output: float | None = None,
        enabled: bool = True,
        price_state: str | None = None,
        verification_source: str = "manual_registration",
    ) -> RouteInfo:
        """Register or update a route in the catalog."""
        if price_state == PriceState.PRICE_OBSERVED_ZERO.value and any(
            cost is not None and cost != 0.0
            for cost in (cost_per_1k_input, cost_per_1k_output)
        ):
            raise ValueError("price_observed_zero routes cannot declare non-zero token costs")
        if price_state is None:
            if (
                cost_per_1k_input == 0
                and cost_per_1k_output == 0
                and cost_per_1k_input is not None
                and cost_per_1k_output is not None
            ):
                price_state = PriceState.PRICE_OBSERVED_ZERO.value
            else:
                price_state = PriceState.UNKNOWN.value
        route = RouteInfo(
            id=route_id,
            provider=provider,
            enabled=enabled,
            price_state=price_state,  # type: ignore[arg-type]
            cost_per_1k_input=cost_per_1k_input,
            cost_per_1k_output=cost_per_1k_output,
            last_verified=time.strftime("%Y-%m-%d"),
            verification_source=verification_source,
        )
        self.store.upsert_route(route)
        self.data = self._load()
        return route

    def set_cooldown(self, route_id: str, duration_sec: float | None = None, reason: str = "") -> float:
        """Temporarily cool down a route after rate limits or transient errors.
        
        If duration_sec is None, applies adaptive exponential backoff based on consecutive rate limits.
        Returns the cooldown expiry timestamp.
        """
        if duration_sec is None:
            return self.store.record_rate_limit_with_adaptive_backoff(route_id, reason)
        expiry = time.time() + duration_sec
        self.store.set_cooldown(route_id, expiry, reason)
        return expiry

    def is_cooled_down(self, route_id: str) -> bool:
        """Check whether a route is currently in cooldown."""
        return self.store.is_route_cooled_down(route_id)

    def get_earliest_cooldown_retry(self, route_ids: list[str] | None = None) -> float:
        """Return the number of seconds until the earliest cooled-down route is available again."""
        exp = self.store.get_earliest_cooldown_expiry(route_ids)
        if exp:
            return max(0.0, exp - time.time())
        return 0.0

    def get_route_summary(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Return comprehensive route catalog summary with Bayesian scores and attempt stats."""
        routes = self.get_routes(free_only=False, include_disabled=include_disabled)
        active_cooldowns = self.store.get_active_cooldowns()
        now = time.time()

        from .scoring import RouteScorer
        scorer = RouteScorer(self.store)
        history_stats = self.store.get_route_history_stats()
        route_dicts = [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in routes]
        scores = scorer.score_routes(route_dicts)

        summary = []
        for r in routes:
            rid = r["id"] if isinstance(r, dict) else r.id
            provider = r["provider"] if isinstance(r, dict) else r.provider
            enabled = r["enabled"] if isinstance(r, dict) else r.enabled
            last_verified = r.get("last_verified") if isinstance(r, dict) else getattr(r, "last_verified", None)
            stat = history_stats.get(rid, {})
            cd_until = active_cooldowns.get(rid)
            is_cooling = cd_until is not None and cd_until > now

            status_str = "COOLING" if is_cooling else ("ACTIVE" if enabled else "DISABLED")
            # History totals are decayed effective sample sizes (floats); round
            # for display while keeping full precision for scoring.
            total = round(float(stat.get("total", 0)), 2)
            completed = round(float(stat.get("completed", 0)), 2)
            success_rate = (completed / total * 100.0) if total > 0 else None

            summary.append({
                "id": rid,
                "provider": provider,
                "enabled": enabled,
                "status": status_str,
                "is_cooling": is_cooling,
                "cooling_seconds": round(max(0.0, cd_until - now), 1) if is_cooling else 0.0,  # type: ignore[operator]
                "bayesian_score": scores.get(rid, 0.5) if enabled and not is_cooling else (0.0 if is_cooling else 0.5),
                "total_attempts": total,
                "completed": completed,
                "success_rate": success_rate,
                "rate_limits": round(float(stat.get("rate_limits", 0)), 2),
                "avg_duration": round(float(stat.get("avg_duration", 0.0)), 2),
                "cooldown_until": cd_until,
                "cooldown_remaining_sec": round(max(0.0, cd_until - now), 1) if is_cooling else 0.0,  # type: ignore[operator]
                "last_verified": last_verified,
            })

        def sort_key(x):
            status_order = {"ACTIVE": 0, "COOLING": 1, "DISABLED": 2}.get(x["status"], 3)
            return (status_order, -x["bayesian_score"])

        return sorted(summary, key=sort_key)

    def refresh_from_openai_compatible(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        provider_name: str = "openai_compatible",
    ) -> int:
        """Discover routes from an OpenAI-compatible /models endpoint (e.g. Ollama, LM Studio, vLLM)."""
        from .providers.openai_compatible import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider(base_url=base_url, api_key=api_key, provider_name=provider_name)
        headers = {}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{provider.base_url}/models", headers=headers)
                if resp.status_code != 200:
                    return 0
                data = resp.json()
                models = data.get("data", [])
                count = 0
                is_local = provider.is_local
                price_state = PriceState.PRICE_OBSERVED_ZERO.value if is_local else PriceState.UNKNOWN.value
                cost = 0.0 if is_local else None
                seen_route_ids: set[str] = set()
                for m in models:
                    mid = m.get("id")
                    if not mid:
                        continue
                    rid = f"{provider_name}/{mid}"
                    seen_route_ids.add(rid)
                    self.add_route(
                        route_id=rid,
                        provider=provider_name,
                        cost_per_1k_input=cost,
                        cost_per_1k_output=cost,
                        enabled=True,
                        price_state=price_state,
                        verification_source=f"{provider_name} /models discovery (local={is_local})",
                    )
                    count += 1
                for route in self.data.get("routes", []):
                    if route.get("provider") == provider_name and route.get("id") not in seen_route_ids:
                        route["enabled"] = False
                        route["price_state"] = PriceState.UNKNOWN.value
                        route["disabled_reason"] = f"not present in latest {provider_name} /models response"
                        route["disabled_at"] = time.time()
                self.save()
                return count
        except Exception:
            return 0

    def get_ladder(
        self,
        task_seed: str = "",
        provider: str | None = None,
        free_only: bool = True,
        task_name: str | None = None,
        policy: Any | None = None,
    ) -> list[str]:
        """Returns an intelligently prioritized list of route IDs based on historical performance and eval scores."""
        ranked, _ = self.get_ladder_with_scores(
            task_seed=task_seed,
            provider=provider,
            free_only=free_only,
            task_name=task_name,
            policy=policy,
        )
        return ranked

    def get_ladder_with_scores(
        self,
        task_seed: str = "",
        provider: str | None = None,
        free_only: bool = True,
        task_name: str | None = None,
        policy: Any | None = None,
    ) -> tuple[list[str], dict[str, float]]:
        """Like get_ladder but also returns the score map from the same scoring pass."""
        effective_free_only = free_only
        if policy is not None:
            # Paid lanes require explicit route approval. Cost ceilings constrain
            # an approved route; they do not grant permission to use paid routes.
            if getattr(policy, "free_only", False):
                effective_free_only = True
            elif getattr(policy, "allowed_routes", None):
                effective_free_only = False
        routes = self.get_routes(provider=provider, free_only=effective_free_only)
        # Demo output is synthetic and must never enter a real campaign implicitly.
        routes = [r for r in routes if r.get("provider") != "demo" or (
            policy is not None and (
                r["id"] in (getattr(policy, "allowed_routes", None) or [])
                or "demo" in (getattr(policy, "allowed_transports", None) or [])
                or "demo" in (getattr(policy, "allowed_providers", None) or [])
            )
        )]
        if not routes:
            return [], {}
        # A route whose transport cannot run here is not a candidate: ranking them
        # in made a keyless install spend every attempt on providers that answer
        # `auth_error` (two of three attempts in a live run), scoring nothing.
        # A policy that names routes explicitly is honoured as written, because a
        # pin is a deliberate instruction rather than a preference.
        if not (policy is not None and getattr(policy, "allowed_routes", None)):
            from .providers.registry import configured_routes

            usable = configured_routes(routes)
            if usable:
                routes = usable
        from .scoring import rank_with_scores
        return rank_with_scores(
            routes=routes,
            store=self.store,
            task_name=task_name,
            policy=policy,
            seed=task_seed,
        )

    def record_cost(
        self,
        route_id: str,
        reported_cost: float | None,
        policy: Any | None = None,
    ) -> None:
        """Update state from provider-reported cost and trip circuit breaker only when appropriate."""
        with self._lock:
            routes = self.data.get("routes", [])
            target_route = next((r for r in routes if r["id"] == route_id), None)

            is_zero_price_route = False
            if target_route:
                if is_observed_zero_price_route(target_route):
                    is_zero_price_route = True

            run_is_free_only = True
            if policy:
                if getattr(policy, "free_only", False):
                    run_is_free_only = True
                elif getattr(policy, "allowed_routes", None):
                    run_is_free_only = False

            if reported_cost == 0:
                # A single zero-cost response is not proof that an unknown or
                # paid route is permanently free. Keep explicit approval and
                # pricing evidence separate from per-request observations.
                if target_route and is_observed_zero_price_route(target_route):
                    if target_route.get("price_state") != PriceState.DISABLED.value:
                        target_route["price_state"] = PriceState.PRICE_OBSERVED_ZERO.value
                        target_route["last_price_observation"] = time.time()
                        self.save()
                return

            if reported_cost is not None and reported_cost > 0:
                # If this was admitted as a free route OR the run is strictly free-only:
                if is_zero_price_route or run_is_free_only:
                    if target_route:
                        target_route["enabled"] = False
                        target_route["price_state"] = PriceState.DISABLED.value
                        target_route["disabled_reason"] = (
                            f"Circuit breaker tripped: reported cost {reported_cost} > 0 on free route."
                        )
                        target_route["disabled_at"] = time.time()
                        self.save()
                    raise RouteCircuitBreaker(
                        f"Non-zero cost {reported_cost} reported on free route '{route_id}'! Route disabled."
                    )

                # For paid routes: check per-request spend ceiling if configured in policy
                max_request_cost = getattr(policy, "max_request_cost", None) if policy else None
                if max_request_cost is not None and reported_cost > max_request_cost:
                    if target_route:
                        target_route["enabled"] = False
                        target_route["price_state"] = PriceState.DISABLED.value
                        target_route["disabled_reason"] = (
                            f"Circuit breaker tripped: reported cost {reported_cost} exceeded policy max_request_cost {max_request_cost}."
                        )
                        target_route["disabled_at"] = time.time()
                        self.save()
                    raise RouteCircuitBreaker(
                        f"Request cost {reported_cost} exceeded policy max_request_cost ceiling of {max_request_cost} on '{route_id}'!"
                    )

                # Record last price observation for the paid route without disabling it
                if target_route:
                    target_route["last_price_observation"] = time.time()
                    self.save()

    def _parse_harness_discovery(self, spec, text: str) -> dict:
        """Parse one harness models transcript into {route_id: model_data}."""
        if spec.name == "opencode":
            models: dict = {}
            decoder = json.JSONDecoder()
            while text.strip():
                text = text.lstrip()
                header, sep, rest = text.partition("\n")
                if not sep:
                    break
                if _admissible_opencode_model(header, spec.name):
                    route_id = header if header.startswith(f"{spec.name}/") else f"{spec.name}/{header}"
                    try:
                        obj, end = decoder.raw_decode(rest.lstrip())
                        models[route_id] = obj
                        text = rest.lstrip()[end:]
                    except Exception:
                        text = rest
                else:
                    text = rest
            return models
        # antigravity (`agy models`) prints a tab-separated table:
        #   "Fetching available models..." then  id<TAB>Description
        if spec.name == "antigravity":
            models = {}
            for line in text.splitlines():
                line = line.strip()
                if "\t" not in line:
                    continue  # banner lines, section headers, blank
                model_id, _, description = line.partition("\t")
                model_id = model_id.strip()
                if not model_id or " " in model_id:
                    continue
                # A bare id from a text table carries no pricing evidence: it
                # enters as a candidate, never as an observed-zero route.
                models[f"{spec.name}/{model_id}"] = {
                    "id": model_id,
                    "description": description.strip(),
                }
            return models
        # grok models prints "Available models:" then "  * id (default)" bullets.
        if spec.name == "grok":
            models = {}
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("*"):
                    continue
                model_id = stripped.lstrip("* ").strip()
                model_id = re.sub(r"\s*\(default\)\s*$", "", model_id).strip()
                if not model_id:
                    continue
                models[model_id if "/" in model_id else f"{spec.name}/{model_id}"] = {"id": model_id}
            return models
        # cursor-agent models: conservative JSON read; bare ids carry no
        # pricing evidence, so they enter as disabled candidates, never as
        # observed-zero routes.
        try:
            payload = json.loads(text.strip())
        except (json.JSONDecodeError, AttributeError):
            return {}
        entries = payload
        if isinstance(payload, dict):
            for key in ("models", "data"):
                if isinstance(payload.get(key), list):
                    entries = payload[key]
                    break
            else:
                return {}
        if not isinstance(entries, list):
            return {}
        models = {}
        for entry in entries:
            if isinstance(entry, str) and entry.strip():
                mid = entry.strip()
                models[mid if "/" in mid else f"cursor/{mid}"] = {"id": mid}
            elif isinstance(entry, dict) and entry.get("id"):
                mid = str(entry["id"])
                models[mid if "/" in mid else f"cursor/{mid}"] = entry
        return models

    def refresh_from_harness(self, spec) -> int:
        """Discover routes from one CLI harness over its spec discovery command.

        Harnesses without a documented models command (``discovery_argv``
        None: claude, codex, muse) contribute 0; their availability is
        surfaced by the doctor matrix instead. Routes for those harnesses
        enter via ``routes add`` / refresh, never invented.
        """
        if not spec.discovery_argv:
            return 0
        if not shutil.which(spec.binary):
            raise RuntimeError(f"{spec.binary} CLI not found in PATH")
        from .providers.harness import discovery_env

        res = subprocess.run(
            [spec.binary, *spec.discovery_argv],
            capture_output=True,
            text=True,
            timeout=30,
            env=discovery_env(spec),
        )
        if res.returncode != 0:
            raise RuntimeError(f"Failed to query {spec.binary} models: {res.stderr}")
        source = f"{spec.binary} {' '.join(spec.discovery_argv)}"
        models = self._parse_harness_discovery(spec, res.stdout)
        known = {r["id"]: r for r in self.data.get("routes", [])}
        discovered_count = 0
        seen_route_ids: set[str] = set()
        for model_id, model_data in models.items():
            seen_route_ids.add(model_id)
            if spec.name == "opencode":
                price_state = classify_price_state(model_data)
                is_active = model_data.get("status") == "active"
            elif spec.name in {"antigravity", "grok"}:
                # Their `models` output is a plain text listing: an id and a
                # description, never pricing. Classifying that as "unknown"
                # would drop every route, so they enter as candidates.
                price_state = PriceState.CANDIDATE
                is_active = True
            else:
                price_state = (
                    classify_price_state(model_data)
                    if len(model_data) > 1
                    else PriceState.CANDIDATE
                )
                is_active = True
            if model_id in known:
                r = known[model_id]
                r["price_state"] = price_state.value
                r["enabled"] = price_state is PriceState.PRICE_OBSERVED_ZERO and is_active
                r["last_verified"] = time.strftime("%Y-%m-%d")
                r["verification_source"] = source
                _apply_price_evidence(r, price_state)
            elif price_state in {PriceState.CANDIDATE, PriceState.PRICE_OBSERVED_ZERO} and is_active:
                route: dict[str, Any] = {
                    "id": model_id,
                    "provider": spec.name,
                    "enabled": price_state is PriceState.PRICE_OBSERVED_ZERO,
                    "price_state": price_state.value,
                    "auth": "hosted-free" if spec.name == "opencode" else "cli-default",
                    "last_verified": time.strftime("%Y-%m-%d"),
                    "verification_source": source,
                }
                _apply_price_evidence(route, price_state)
                self.data["routes"].append(route)
            if price_state in {PriceState.CANDIDATE, PriceState.PRICE_OBSERVED_ZERO}:
                discovered_count += 1
        for route in self.data.get("routes", []):
            if route.get("provider") == spec.name and route.get("id") not in seen_route_ids:
                route["enabled"] = False
                route["price_state"] = PriceState.UNKNOWN.value
                route["disabled_reason"] = f"not present in latest {spec.binary} model response"
                route["disabled_at"] = time.time()
                # Not listed any more means no current pricing evidence, so drop
                # any stale zero rather than leaving it readable as verified-free.
                _apply_price_evidence(route, PriceState.UNKNOWN)
        self.save()
        return discovered_count

    def refresh_from_opencode(self) -> int:
        """Query OpenCode and record candidates separately from observed zero prices."""
        from .providers.opencode import OPENCODE_SPEC

        return self.refresh_from_harness(OPENCODE_SPEC)

    def refresh_from_openrouter(self) -> int:
        """Query OpenRouter and distinguish explicit zero pricing from name candidates."""
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get("https://openrouter.ai/api/v1/models")
                if resp.status_code != 200:
                    raise RuntimeError(f"OpenRouter models API returned HTTP {resp.status_code}")
                data = resp.json().get("data", [])
        except Exception as e:
            raise RuntimeError(f"Failed to fetch OpenRouter models: {e}") from e

        known = {r["id"]: r for r in self.data.get("routes", [])}
        discovered_count = 0
        seen_route_ids: set[str] = set()

        for m in data:
            raw_id = str(m.get("id") or "").strip()
            if not raw_id:
                continue
            route_id = f"openrouter/{raw_id}" if not raw_id.startswith("openrouter/") else raw_id
            seen_route_ids.add(route_id)
            if not supports_text_completion(m):
                # Free but the wrong shape of model (for example an audio
                # generator): it must never enter the ladder a text batch is
                # routed through, however cheap it is.
                existing = known.get(route_id)
                if existing is not None:
                    existing["enabled"] = False
                    existing["price_state"] = PriceState.UNKNOWN.value
                    existing["disabled_reason"] = (
                        "model output modality is not text; unusable for text inference"
                    )
                    existing["disabled_at"] = time.time()
                    _apply_price_evidence(existing, PriceState.UNKNOWN)
                continue
            price_state = classify_price_state(m)

            pricing = m.get("pricing") or m.get("cost") or {}
            prompt_cost = pricing.get("prompt") if pricing.get("prompt") is not None else (pricing.get("input") or 0.0)
            comp_cost = pricing.get("completion") if pricing.get("completion") is not None else (pricing.get("output") or 0.0)

            if route_id in known:
                route = known[route_id]
                route["price_state"] = price_state.value
                route["enabled"] = price_state is PriceState.PRICE_OBSERVED_ZERO
                if price_state is PriceState.PRICE_OBSERVED_ZERO:
                    route["cost_per_1k_input"] = float(prompt_cost) * 1000  # type: ignore[arg-type]
                    route["cost_per_1k_output"] = float(comp_cost) * 1000  # type: ignore[arg-type]
                    route["disabled_reason"] = None
                    route["disabled_at"] = None
                else:
                    route.pop("cost_per_1k_input", None)
                    route.pop("cost_per_1k_output", None)
                    route["disabled_reason"] = "pricing is not currently verified as zero"
                    route["disabled_at"] = time.time()
                route["last_verified"] = time.strftime("%Y-%m-%d")
                route["verification_source"] = "openrouter /api/v1/models pricing"
            elif price_state in {PriceState.CANDIDATE, PriceState.PRICE_OBSERVED_ZERO}:
                route = {
                    "id": route_id,
                    "provider": "openrouter",
                    "enabled": price_state is PriceState.PRICE_OBSERVED_ZERO,
                    "price_state": price_state.value,
                    "auth": "api-key",
                    "last_verified": time.strftime("%Y-%m-%d"),
                    "verification_source": "openrouter /api/v1/models pricing"
                }
                if price_state is PriceState.PRICE_OBSERVED_ZERO:
                    route["cost_per_1k_input"] = float(prompt_cost) * 1000  # type: ignore[arg-type]
                    route["cost_per_1k_output"] = float(comp_cost) * 1000  # type: ignore[arg-type]
                self.data["routes"].append(route)
            discovered_count += 1

        for route in self.data.get("routes", []):
            if route.get("provider") == "openrouter" and route.get("id") not in seen_route_ids:
                route["enabled"] = False
                route["price_state"] = PriceState.UNKNOWN.value
                route["disabled_reason"] = "not present in latest OpenRouter model response"
                route["disabled_at"] = time.time()
                # A route the provider no longer lists has no current pricing
                # evidence: drop the stale cost figures so nothing reads it as
                # still-verified-free. (A retired ':free' variant is exactly
                # this case, and its surviving base model may well be billed.)
                _apply_price_evidence(route, PriceState.UNKNOWN)

        self.save()
        return discovered_count

    def refresh_all(self) -> dict[str, int]:
        """Refresh the provider route catalogue and current pricing."""
        from .providers.registry import HARNESS_SPECS

        results: dict[str, Any] = {}
        for spec in HARNESS_SPECS:
            try:
                results[spec.name] = self.refresh_from_harness(spec)
            except Exception as e:
                results[f"{spec.name}_error"] = str(e)

        try:
            results["openrouter"] = self.refresh_from_openrouter()
        except Exception as e:
            results["openrouter_error"] = str(e)

        try:
            results["openai_compatible"] = self.refresh_from_openai_compatible()
        except Exception as e:
            results["openai_compatible_error"] = str(e)

        return results

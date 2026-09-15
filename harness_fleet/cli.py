"""Human and machine CLI for the SQLite-backed harness-fleet control plane."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import ui
from .calibrate import PARAM_KINDS
from .catalog import PriceState, RouteCatalog
from .discover import (
    BACKENDS as DISCOVER_BACKENDS,
)
from .discover import (
    USER_AGENT as DISCOVER_USER_AGENT,
)
from .discover import (
    DiscoverError,
    crawl_site,
    discover_sitemap_url,
    fetch_ashby_org,
    fetch_devto_tag,
    fetch_discourse_search,
    fetch_greenhouse_board,
    fetch_hn_thread,
    fetch_lemmy,
    fetch_lever_org,
    fetch_lobsters,
    fetch_reddit_posts,
    fetch_reddit_rss,
    fetch_sitemap_urls,
    fetch_smart_url,
    fetch_stackexchange_questions,
    fetch_yc_companies,
    run_discovery,
    to_input_items,
    write_items_csv,
    write_items_jsonl,
)
from .engine import DEFAULT_PROMPT_TIMEOUT_SEC, Engine
from .export import export_clean_packet
from .input_data import iter_input_items, load_input_items
from .models import (
    CandidateModelOutput,
    ClaimFilter,
    CleanPacket,
    DoctorCheck,
    DoctorReport,
    InputItem,
    ModelOutput,
    ProviderReceipt,
    RoutePolicy,
    SortSpec,
    TaskSpec,
    ValidationReport,
)
from .packer import iter_packed_batches
from .profile import IdealCompanyProfile
from .setup import installed_skill_matches, setup_workspace, skill_destination
from .store import SCHEMA_VERSION, HarnessStore
from .task import (
    PRESETS,
    create_task_from_preset,
    load_task_spec,
    parse_half_life,
    parse_source_weight,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _split_csv_list(values: list[str] | str | None) -> list[str] | None:
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for item in values:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                result.append(cleaned)
    return result if result else None


def _studio_policy(store: HarnessStore, args: argparse.Namespace) -> dict[str, Any]:
    """The route policy fields implied by the studio's persisted selection.

    Honours the selection's mode: 'free' pins the verified zero-price routes
    of the chosen providers (and implies --free-only); 'specific' pins the
    exact route ids the operator ticked.
    """
    selection = store.get_studio_selection()
    if not selection or not (selection.get("providers") or selection.get("routes")):
        return {}
    mode = selection.get("mode") or "free"
    chosen = [str(p) for p in selection.get("providers") or []]
    if mode == "free":
        catalog = RouteCatalog(db_path=store.path)
        routes = [r["id"] for r in catalog.get_routes(free_only=False, include_disabled=False)
                  if r["provider"] in chosen and r["price_state"] == "price_observed_zero"]
        return {"allowed_routes": routes or None, "free_only": True}
    routes = [str(r) for r in selection.get("routes") or []]
    return {"allowed_routes": routes or None}


def _extract_policy(args: argparse.Namespace) -> RoutePolicy | None:
    store = _store(args)
    from_studio = bool(getattr(args, "from_studio", False))
    studio_fields: dict[str, Any] = _studio_policy(store, args) if from_studio else {}
    providers = _split_csv_list(getattr(args, "provider", None)) or studio_fields.get("allowed_transports")
    exclude_providers = _split_csv_list(getattr(args, "exclude_provider", None)) or []
    routes = _split_csv_list(getattr(args, "route", None)) or studio_fields.get("allowed_routes")
    exclude_routes = _split_csv_list(getattr(args, "exclude_route", None)) or []
    zdr = bool(getattr(args, "zdr", False))
    no_data_coll = bool(getattr(args, "no_data_collection", False))
    max_cost_in = float(getattr(args, "max_cost_in", 0.0) or 0.0)
    max_cost_out = float(getattr(args, "max_cost_out", 0.0) or 0.0)
    raw_req_cost = getattr(args, "max_request_cost", None)
    max_request_cost = float(raw_req_cost) if raw_req_cost is not None else None
    free_only = bool(getattr(args, "free_only", False)) or bool(studio_fields.get("free_only"))
    openrouter_providers = _split_csv_list(getattr(args, "openrouter_providers", None))
    openrouter_ignore = _split_csv_list(getattr(args, "openrouter_ignore", None)) or []
    openrouter_order = _split_csv_list(getattr(args, "openrouter_order", None))

    if not any([
        providers, exclude_providers, routes, exclude_routes,
        zdr, no_data_coll, max_cost_in > 0, max_cost_out > 0,
        max_request_cost is not None,
        free_only,
        openrouter_providers, openrouter_ignore, openrouter_order,
    ]):
        return None

    return RoutePolicy(
        allowed_transports=providers if providers else None,
        excluded_transports=exclude_providers,
        allowed_routes=routes if routes else None,
        excluded_routes=exclude_routes,
        zdr=zdr,
        allow_data_collection=not no_data_coll,
        max_cost_per_1k_input=max_cost_in,
        max_cost_per_1k_output=max_cost_out,
        max_request_cost=max_request_cost,
        free_only=free_only,
        openrouter_providers=openrouter_providers,
        openrouter_ignore=openrouter_ignore,
        openrouter_order=openrouter_order,
    )


def _workspace_path(value: str | Path, workspace_root: str | Path = ".") -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else Path(workspace_root).expanduser().resolve() / candidate


def _input_source(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    only_ids = getattr(args, "only_ids", None)
    if isinstance(only_ids, str) and not any(separator in only_ids for separator in (",", ";", "\n")):
        # The help promises "a file (CSV, JSONL, TXT) or a comma-separated list",
        # so a single bare value is an ID unless it really is a filter file. It
        # used to be welded to the workspace root unconditionally, which made
        # `--only-ids acme.dev` a missing file instead of one selected ID.
        candidate = _workspace_path(only_ids, getattr(args, "workspace_root", "."))
        if candidate.is_file():
            only_ids = candidate
        elif candidate.suffix.lower() in {".csv", ".json", ".jsonl", ".txt"}:
            only_ids = candidate  # let the loader report the missing file
        else:
            only_ids = [only_ids]
    return _workspace_path(args.input, getattr(args, "workspace_root", ".")), {
        "id_column": getattr(args, "id_column", None),
        "text_column": getattr(args, "text_column", None),
        "title_column": getattr(args, "title_column", None),
        "uri_column": getattr(args, "uri_column", None),
        "only_ids": only_ids,
        "fuzzy_ids": getattr(args, "only_ids_fuzzy", False),
    }


def _iter_input(args: argparse.Namespace):
    source, options = _input_source(args)
    return iter_input_items(source, **options)


def _input_factory(args: argparse.Namespace):
    source, options = _input_source(args)
    return lambda: iter_input_items(source, **options)


def _load_input(args: argparse.Namespace) -> list[InputItem]:
    """Compatibility helper for commands that intentionally need a list."""
    return list(_iter_input(args))


def _package_version() -> str:
    executable = Path(sys.argv[0]).stem.lower()
    # Unified entry-point table: each distribution ships its own script, but
    # this module is byte-identical across repos, so every product name maps
    # to its distribution here.
    preferred = {
        "harness-fleet": "harness-fleet",
        "account-fleet": "account-fleet",
        "career-fleet": "career-fleet",
        "career-lanes": "career-fleet",
    }.get(executable)
    # The code that is running knows its version; installed metadata can be
    # stale or belong to a different distribution on the path (a PYTHONPATH
    # checkout next to an older install is the common case).
    try:
        from . import __version__

        if __version__ and __version__ != "0.0.0":
            return __version__
    except ImportError:
        pass
    package_names = [preferred] if preferred else []
    package_names.extend(
        name for name in ("harness-fleet", "account-fleet", "career-fleet")
        if name not in package_names
    )
    for name in package_names:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return "0.0.0"


def _emit(value: Any, json_mode: bool, human: str | None = None) -> None:
    payload = value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value
    if json_mode or human is None:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(human)


def _store(args: argparse.Namespace) -> HarnessStore:
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    explicit = getattr(args, "db", None)
    configured = os.environ.get("HARNESS_FLEET_DB")
    path = Path(explicit or configured or "harness-fleet.db").expanduser()
    return HarnessStore(path if path.is_absolute() else workspace / path)


def _resolve_task(reference: str, store: HarnessStore, workspace_root: str | Path = ".") -> TaskSpec:
    path = _workspace_path(reference, workspace_root)
    if path.is_file():
        spec = load_task_spec(path)
        store.register_task(spec)
        return spec
    return store.get_task(reference)


def _resolve_profile(args: argparse.Namespace, store: HarnessStore) -> tuple[IdealCompanyProfile | None, str | None]:
    """Load the selected ICP and return it with its immutable revision ID."""
    profile_path = getattr(args, "profile", None)
    if profile_path:
        workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
        candidate = Path(profile_path).expanduser()
        profile = IdealCompanyProfile.load(candidate if candidate.is_absolute() else workspace / candidate)
        return profile, store.save_profile(profile)
    if not getattr(args, "use_active_profile", False):
        return None, None
    revision = store.active_profile_revision_id("ideal_company")
    return (store.load_profile("ideal_company"), revision) if revision else (None, None)


def cmd_profile(args: argparse.Namespace) -> None:
    """Create, inspect, and persist the account-side Ideal Company Profile."""
    store = _store(args)
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    profile_path = Path(getattr(args, "path", "ideal_company_profile.json")).expanduser()
    if not profile_path.is_absolute():
        profile_path = workspace / profile_path
    profile_exists = profile_path.is_file()
    if getattr(args, "init", False) and profile_exists and not getattr(args, "force", False):
        # Raise so main() renders this in whichever mode was requested
        # (--json included) instead of writing human text to stdout.
        raise ValueError(f"profile already exists at {profile_path}; use --force to replace it")

    if getattr(args, "init", False) or (not profile_exists and store.load_profile() is None):
        profile = IdealCompanyProfile()
        profile.save(profile_path)
    elif profile_exists:
        profile = IdealCompanyProfile.load(profile_path)
    else:
        loaded_profile = store.load_profile()
        if loaded_profile is None:
            raise FileNotFoundError(
                f"No Ideal Company Profile found at {profile_path} or in {store.path}. Use 'harness-fleet profile --init'."
            )
        profile = loaded_profile
        profile.save(profile_path)

    revision = store.save_profile(profile)
    _emit(
        {
            "profile_kind": "ideal_company",
            "revision": revision,
            "database": str(store.path.resolve()),
            "path": str(profile_path),
            "profile": profile.model_dump(mode="json"),
        },
        getattr(args, "json", False),
        f"Ideal Company Profile: {profile.profile_name} (v{profile.version})\n"
        f"Revision: {revision}\nDatabase: {store.path.resolve()}\nPath: {profile_path}",
    )


def cmd_routes(args: argparse.Namespace) -> None:
    catalog = RouteCatalog(db_path=_store(args).path)
    action = getattr(args, "action", "list")
    add_alias = getattr(args, "add", None)
    route_id = getattr(args, "route_id", None) or add_alias

    # Only `add` (or the legacy --add alias) registers a route; a bare route_id
    # with `list`/`refresh` is a filter/target, never an implicit write.
    if action == "add" or add_alias:
        if not route_id:
            raise ValueError("route_id required to add a route")
        provider = getattr(args, "provider", None) or (route_id.split("/", 1)[0] if "/" in route_id else "openai_compatible")
        is_free = bool(getattr(args, "free", False))
        in_cost = getattr(args, "input_cost", None)
        out_cost = getattr(args, "output_cost", None)
        if is_free:
            price_state = PriceState.PRICE_OBSERVED_ZERO.value
            in_cost = 0.0 if in_cost is None else float(in_cost)
            out_cost = 0.0 if out_cost is None else float(out_cost)
        elif in_cost is not None and out_cost is not None and float(in_cost) == 0.0 and float(out_cost) == 0.0:
            price_state = PriceState.PRICE_OBSERVED_ZERO.value
            in_cost = float(in_cost)
            out_cost = float(out_cost)
        else:
            price_state = PriceState.UNKNOWN.value
            in_cost = float(in_cost) if in_cost is not None else None
            out_cost = float(out_cost) if out_cost is not None else None

        created = catalog.add_route(
            route_id=route_id,
            provider=provider,
            cost_per_1k_input=in_cost,
            cost_per_1k_output=out_cost,
            enabled=not getattr(args, "disable", False),
            price_state=price_state,
            verification_source="manual_registration",
        )
        if args.json:
            _emit({"route": created.model_dump(mode="json"), "status": "added"}, True)
        else:
            print(f"Added route '{created.id}' (provider: {created.provider}, price_state: {created.price_state}, enabled: {created.enabled})")
        return

    refresh = catalog.refresh_all() if (getattr(args, "refresh", False) or action == "refresh") else None
    routes = catalog.get_routes(free_only=not args.all, include_disabled=args.all)
    if args.json:
        _emit({"routes": routes, "count": len(routes), "refresh": refresh}, True)
    else:
        ui.banner()
        ui.print_routes_table(routes)


def cmd_cooldowns(args: argparse.Namespace) -> None:
    store = _store(args)
    if getattr(args, "clear", False):
        route_id = getattr(args, "route", None)
        cleared = store.clear_cooldowns(route_id=route_id)
        msg = f"Cleared {cleared} active cooldown record(s)" + (f" for route '{route_id}'" if route_id else " for all routes")
        _emit({"cleared": cleared, "route_id": route_id}, args.json, msg)
        return

    cooldowns = store.get_active_cooldown_details()
    if args.json:
        _emit({"cooldowns": cooldowns, "count": len(cooldowns)}, True)
    else:
        ui.banner()
        ui.print_cooldowns_table(cooldowns)


def cmd_tasks(args: argparse.Namespace) -> None:
    tasks = _store(args).list_tasks()
    human = "No tasks registered." if not tasks else "\n".join(
        f"{task['task_name']}  {task['revision_id'][:12]}" for task in tasks
    )
    _emit({"tasks": tasks, "count": len(tasks)}, args.json, human)


def _infer_schema_from_example(path: Path, label_column: str | None = None) -> tuple[dict[str, Any], str]:
    """Infer a draft claims_schema from a labeled CSV/JSONL example file."""
    import csv as _csv
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with open(path, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("example CSV has no header")
            rows = list(reader)
            if not rows:
                raise ValueError("example CSV is empty")
            fieldnames = list(reader.fieldnames)
            # Auto-detect label column if not provided
            if label_column is None:
                candidates = [c for c in fieldnames if c.lower() in ("label", "target", "category", "class", "priority", "severity", "pricing_type")]
                label_column = candidates[0] if candidates else fieldnames[-1]
            if label_column not in fieldnames:
                raise ValueError(f"label column '{label_column}' not found in {fieldnames}")
            distinct = sorted({(r.get(label_column) or "").strip() for r in rows if (r.get(label_column) or "").strip()})
            # Build schema: infer enum vs string
            if distinct and len(distinct) <= 20 and all(len(v) < 50 for v in distinct):
                schema = {
                    "type": "string",
                    "enum": distinct,
                    "description": f"Category label from '{label_column}' examples; choose exactly one listed value, supported by the cited quotes.",
                }
            else:
                schema = {
                    "type": "string",
                    "description": f"Category label from '{label_column}' examples, supported by the cited quotes.",
                }
            # Detect additional label columns (secondary labels)
            other_labels = [c for c in fieldnames if c != label_column and c.lower() not in ("id", "item_id", "text", "body", "content", "title", "source_uri", "url")]
            props: dict[str, Any] = {
                "label": schema,
                "summary": {
                    "type": "string",
                    "description": "One or two sentences supported only by the cited source quotes, no outside knowledge.",
                },
            }
            required = ["label", "summary"]
            # Add other columns as optional string props if they look like labels
            for col in other_labels[:3]:
                vals = {r.get(col, "") for r in rows[:10]}
                if any(vals):
                    props[col] = {
                        "type": "string",
                        "description": f"Additional label from '{col}' examples, supported by the cited quotes.",
                    }
            return {"type": "object", "properties": props, "required": required, "additionalProperties": False}, label_column
    elif suffix in (".jsonl", ".json"):
        import json as _json
        items = []
        if suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    items.append(_json.loads(line))
        else:
            data = _json.loads(path.read_text(encoding="utf-8"))
            items = data["items"] if isinstance(data, dict) and "items" in data else (data if isinstance(data, list) else [])
        if not items or not isinstance(items, list):
            raise ValueError("example file is empty or does not contain a list of items")
        # Look for expected claims in metadata or top-level
        label_column = label_column or "label"
        return {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Category label, supported by the cited quotes.",
                },
                "summary": {
                    "type": "string",
                    "description": "One or two sentences supported only by the cited source quotes, no outside knowledge.",
                },
            },
            "required": ["label", "summary"],
            "additionalProperties": False,
        }, label_column
    raise ValueError(f"unsupported example format {suffix}; use .csv or .jsonl")


def cmd_init(args: argparse.Namespace) -> None:
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    from_example = getattr(args, "from_example", None)
    label_col = getattr(args, "label_column", None)
    if from_example:
        example_path = Path(from_example).expanduser()
        if not example_path.is_absolute():
            example_path = workspace / example_path
        claims_schema, detected_label = _infer_schema_from_example(example_path, label_col)
        instructions = f"Classify each item and provide a supported summary. Labels were inferred from column '{detected_label}' in {example_path.name}."
        preset_name = f"from-example:{example_path.name}"
        spec = TaskSpec(
            name=args.name,
            instructions=instructions,
            batch_size=args.batch_size,
            claims_schema=claims_schema,
        )
    else:
        weight_rules = {}
        for raw in getattr(args, "source_weight", None) or []:
            match, weight = parse_source_weight(raw)
            weight_rules[match] = weight
        half_lives = {}
        for raw in getattr(args, "half_life", None) or []:
            item_id, days = parse_half_life(raw)
            half_lives[item_id] = days
        spec = create_task_from_preset(
            args.name,
            preset_name=args.preset,
            batch_size=args.batch_size,
            source_weights=weight_rules,
            recency_half_lives=half_lives,
        )
        preset_name = args.preset
    store = _store(args)
    revision = store.register_task(spec)
    sample_path = Path(args.sample or f"{args.name}.sample.jsonl").expanduser()
    if not sample_path.is_absolute():
        sample_path = workspace / sample_path
    if not sample_path.exists():
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample = InputItem(item_id="item_1", title="Example", text="Replace this text with the source you want to process.")
        sample_path.write_text(json.dumps(sample.model_dump(mode="json", by_alias=True), ensure_ascii=False) + "\n")
    next_validate = shlex.join([
        "harness-fleet", "validate", spec.name, "--input", str(sample_path),
        "--db", str(store.path.resolve()), "--workspace-root", str(workspace),
    ])
    _emit(
        {
            "created": True,
            "task": spec.name,
            "preset": preset_name,
            "revision": revision,
            "database": str(store.path.resolve()),
            "sample_input": str(sample_path),
            "claims_schema": spec.claims_schema,
            "next": next_validate,
        },
        args.json,
        f"Created task '{spec.name}' from '{preset_name}'.\n"
        f"Claims: {list(spec.claims_schema.get('properties', {}).keys())}\n"
        f"Sample: {sample_path}\nNext: {next_validate}",
    )


def cmd_validate(args: argparse.Namespace) -> None:
    store = _store(args)
    task = _resolve_task(args.task, store, getattr(args, "workspace_root", "."))
    batch_count = 0
    total_items = 0
    # Detect long documents that were sliced into partial windows
    truncated = 0
    total_slices = 0
    for b in iter_packed_batches(_iter_input(args), task.batch_size, task.max_slice_chars):
        batch_count += 1
        for itm in b.get("items", []) if isinstance(b, dict) else []:
            total_items += 1
            slices = itm.get("slices", []) if isinstance(itm, dict) else []
            total_slices += len(slices)
            if any(s.get("partial") for s in slices):
                truncated += 1
    partial_msg = ""
    if truncated:
        partial_msg = f"\nWarning: {truncated}/{total_items} item(s) exceed max_slice_chars={task.max_slice_chars} and were sliding-window sliced (lossless overlapping windows) with partial:true. Quotes must lie within one window; consider raising --max-slice-chars for fewer windows." if truncated else ""
    _emit(
        ValidationReport(
            valid=True, task=task.name, input_items=total_items, batches=batch_count,
            # Surface the slicing caveat in the typed report too, not just on
            # stderr: a JSON consumer must see why grounding may be limited.
            errors=[partial_msg.strip()] if truncated else [],
        ),
        args.json,
        f"Valid. Task '{task.name}' will process {total_items} items in {batch_count} batches.{partial_msg}",
    )
    if truncated and not args.json:
        print(partial_msg, file=sys.stderr)


def cmd_test(args: argparse.Namespace) -> None:
    store = _store(args)
    task = _resolve_task(args.task, store)
    policy = _extract_policy(args)
    input_factory = _input_factory(args)
    # Validate the complete source first, but only pack the first batch for
    # the real inference call. This keeps the test bounded without accepting
    # malformed records hidden after the first batch.
    for _ in input_factory():
        pass
    batch = next(iter_packed_batches(input_factory(), task.batch_size, task.max_slice_chars), None)
    if batch is None:
        raise ValueError("input contains no packable items to test")
    engine = Engine(task=task, store=store, policy=policy)
    ok, results, receipt, error = engine.execute_batch(batch)
    _emit(
        {"ok": ok, "results": results,
         "receipt": receipt.model_dump(mode="json") if isinstance(receipt, ProviderReceipt) else receipt,
         "error": error},
        args.json,
        f"{'Passed' if ok else 'Failed'} one batch.\n{json.dumps(results if ok else {'error': error}, indent=2)}",
    )
    if not ok:
        raise SystemExit(1)


def _check_routes_for_run(store: HarnessStore, policy: RoutePolicy | None) -> None:
    """Fail early, and helpfully, when the run has no route it could use.

    Two cases used to surface as one misleading engine error ("No enabled route
    has observed zero pricing or matches active policy"):

    * the database has no routes at all yet — a fresh workspace needs
      ``routes refresh`` before anything can run;
    * a ``--route`` was pinned to something this database does not know.

    The offline demo route is a third case, and it is registered on demand:
    ``quickstart`` always did that, so ``run --route demo/fake`` failing on a
    fresh database was a trap for exactly the users who wanted no keys at all.
    """
    from .catalog import RouteCatalog

    pinned = list(getattr(policy, "allowed_routes", None) or [])
    catalog = RouteCatalog(db_path=store.path)
    known = {route["id"] for route in catalog.data.get("routes", [])}

    for route_id in pinned:
        if route_id in known:
            continue
        if route_id.startswith("demo/"):
            catalog.add_route(
                route_id=route_id,
                provider="demo",
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                enabled=True,
                price_state="price_observed_zero",
                verification_source="explicit --route (synthetic, deterministic)",
            )
            known.add(route_id)
            continue
        raise ValueError(
            f"route '{route_id}' is not registered in {store.path}. "
            "Run `harness-fleet routes refresh` to discover free routes, or "
            "`harness-fleet routes add` to register this one."
        )

    if pinned:
        return
    # The catalog ships hints, not evidence: on a fresh database every route is
    # a candidate and nothing is verified free, so the run has nothing to lease.
    usable = [
        route for route in catalog.data.get("routes", [])
        if route.get("enabled") and route.get("price_state") == "price_observed_zero"
    ]
    if not usable:
        raise ValueError(
            f"no verified-free route is registered in {store.path} yet "
            f"({len(known)} hint(s), none priced at zero). Run "
            "`harness-fleet routes refresh` to discover free routes, or use "
            "`harness-fleet quickstart` for the offline demo."
        )


def cmd_run(args: argparse.Namespace) -> None:
    store = _store(args)
    task = _resolve_task(args.task, store, getattr(args, "workspace_root", "."))
    input_path, input_options = _input_source(args)
    def input_factory():
        return iter_input_items(input_path, **input_options)

    policy = _extract_policy(args)
    _check_routes_for_run(store, policy)
    profile, profile_revision_id = _resolve_profile(args, store)
    run_id = args.run_id or f"{task.name}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    output = _workspace_path(args.output or f"runs/{run_id}/clean_packet.json", getattr(args, "workspace_root", "."))
    packet = Engine(
        task=task, store=store, policy=policy,
        prompt_timeout_sec=int(getattr(args, "timeout", 0) or DEFAULT_PROMPT_TIMEOUT_SEC),
    ).run_campaign(
        raw_items=input_factory(),
        run_id=run_id,
        input_path=str(input_path.resolve()),
        concurrency=args.sessions,
        max_attempts=args.max_attempts,
        output_packet_path=output,
        policy=policy,
        profile_revision_id=profile_revision_id,
        profile=profile,
        raw_items_factory=input_factory,
    )
    _emit(
        {"run_id": run_id, "packet": str(output), "result": packet},
        args.json,
        f"Run '{run_id}' {store.run_snapshot(run_id)['status']}.\nPacket: {output}\nVerified records: {packet['total_verified_records']}",
    )


def cmd_rescore(args: argparse.Namespace) -> None:
    """Score fresh evidence as a new run linked to its parent run.

    Each rescore round stands on its own evidence: new sources arrive as new
    items (grouped by metadata.entity), the worker re-answers the checklist,
    and score_history records the trajectory per entity across the lineage.
    The flow is a one-node DAG spec executed by run_dag, not inline steps.
    """
    from .dag import DagSpec, RescoreNode, run_dag

    store = _store(args)
    workspace = getattr(args, "workspace_root", ".")
    parent = store.run_snapshot(args.parent_run)
    task_ref = getattr(args, "task", None)
    if task_ref:
        task_name = _resolve_task(task_ref, store, workspace).name
    else:
        task_name = store.get_run_task(args.parent_run).name
    run_id = args.run_id or f"{task_name}-rescore-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    input_path, input_options = _input_source(args)
    options = dict(input_options)
    options.pop("only_ids", None)
    options.pop("only_ids_fuzzy", None)
    policy = _extract_policy(args)
    spec = DagSpec(name=f"rescore-{args.parent_run}", nodes=[RescoreNode(
        id="rescore",
        parent_run=args.parent_run,
        task=task_ref,
        input=str(input_path),
        run_id=run_id,
        output=args.output,
        sessions=args.sessions,
        max_attempts=args.max_attempts,
        policy=policy,
        id_column=options.get("id_column"),
        text_column=options.get("text_column"),
        title_column=options.get("title_column"),
        uri_column=options.get("uri_column"),
        profile=getattr(args, "profile", None),
        use_active_profile=getattr(args, "use_active_profile", False),
    )])
    state = run_dag(spec, store, workspace_root=workspace, dag_id=run_id)
    node = state["nodes"]["rescore"]
    output = _workspace_path(args.output or f"runs/{run_id}/clean_packet.json", workspace)
    result = {
        "run_id": run_id,
        "status": store.run_snapshot(run_id)["status"],
        "total_verified_records": node["verified"],
        "output_path": str(output),
    }
    cached = " (cached)" if node.get("cached") else ""
    _emit(
        {"run_id": run_id, "parent_run": args.parent_run, "packet": str(output), "result": result},
        args.json,
        f"Rescore '{run_id}' of '{args.parent_run}' ({parent['status']}) {result['status']}{cached}.\nPacket: {output}\nVerified records: {result['total_verified_records']}",
    )


def cmd_history(args: argparse.Namespace) -> None:
    store = _store(args)
    rows = store.get_entity_history(args.entity)
    if args.json:
        _emit({"entity": args.entity, "rounds": rows}, True, "")
        return
    if not rows:
        print(f"No score history for entity '{args.entity}'.")
        return
    print(f"Score trajectory for '{args.entity}' ({len(rows)} rounds):")
    for row in rows:
        parent = f" (child of {row['parent_run_id']})" if row.get("parent_run_id") else ""
        print(f"  {row['created_at']}  run={row['run_id']} item={row['item_id']} score={row['score']}{parent}")


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Fit scoring calibration against labeled samples; dry-run unless --apply.

    The flow is a one-node DAG spec executed by run_dag, not inline steps.
    """
    from .dag import CalibrateNode, DagSpec, run_dag

    store = _store(args)
    workspace = getattr(args, "workspace_root", ".")
    route_id = getattr(args, "route", None)
    if not route_id:
        raise ValueError("calibrate pins a single rater route: pass --route ROUTE_ID")
    input_path, input_options = _input_source(args)
    options = dict(input_options)
    options.pop("only_ids", None)
    options.pop("only_ids_fuzzy", None)
    params = [part.strip() for part in (getattr(args, "params", "") or ",".join(PARAM_KINDS)).split(",") if part.strip()]
    spec = DagSpec(name=f"calibrate-{args.task}", nodes=[CalibrateNode(
        id="calibrate",
        task=args.task,
        input=str(input_path),
        expected=getattr(args, "expected", None) or "score",
        route=route_id,
        params=params,
        max_sweeps=int(getattr(args, "max_sweeps", 50)),
        apply=bool(getattr(args, "apply", False)),
        id_column=options.get("id_column"),
        text_column=options.get("text_column"),
        title_column=options.get("title_column"),
        uri_column=options.get("uri_column"),
    )])
    state = run_dag(spec, store, workspace_root=workspace)
    report = state["nodes"]["calibrate"]["report"]
    base_mae, fit_mae = report["baseline"]["mae"], report["fitted_train"]["mae"]
    holdout = report["n_holdout"]
    cached = " (cached)" if state["nodes"]["calibrate"].get("cached") else ""
    summary = (
        f"Calibration for '{report['task']}' on {report['n_train']} train"
        f"{f' + {holdout} holdout' if holdout else ''} samples via {route_id}: "
        f"train MAE {base_mae:.2f} -> {fit_mae:.2f}."
        + (f" Holdout MAE {report['fitted_holdout']['mae']:.2f}." if report["fitted_holdout"] else "")
        + (f" Registered revision {report['new_revision']}." if report["applied"] else " Dry run; pass --apply to register.")
        + cached
    )
    _emit(report, args.json, summary)


def cmd_resume(args: argparse.Namespace) -> None:
    store = _store(args)
    task = store.get_run_task(args.run_id)
    snapshot = store.run_snapshot(args.run_id)
    output = Path(args.output or snapshot.get("output_path") or f"runs/{args.run_id}/clean_packet.json")
    policy = _extract_policy(args)
    packet = Engine(task=task, store=store, policy=policy).resume_campaign(
        args.run_id,
        concurrency=args.sessions,
        output_packet_path=output,
    )
    _emit(
        {"run_id": args.run_id, "packet": str(output), "result": packet},
        args.json,
        f"Run '{args.run_id}' {store.run_snapshot(args.run_id)['status']}.\nPacket: {output}\nVerified records: {packet['total_verified_records']}",
    )


def cmd_sessions(args: argparse.Namespace) -> None:
    snapshot = _store(args).run_snapshot(args.run_id)
    _emit(
        {"run_id": args.run_id, "status": snapshot["status"], "sessions": snapshot["sessions"]},
        args.json,
        f"Run '{args.run_id}' is {snapshot['status']}. Recorded worker sessions: {len(snapshot['sessions'])}.",
    )


def cmd_status(args: argparse.Namespace) -> None:
    store = _store(args)
    if not getattr(args, "watch", False):
        report = store.get_run_status(args.run_id)
        if args.json:
            _emit(report, True)
        else:
            ui.print_status_dashboard(report)
        return

    try:
        while True:
            report = store.get_run_status(args.run_id)
            if not args.json:
                print("\033[H\033[J", end="")
                ui.print_status_dashboard(report)
            if report.status in ("completed", "completed_with_failures", "budget_exhausted"):
                break
            time.sleep(getattr(args, "interval", 2.0))
    except KeyboardInterrupt:
        pass
    if args.json:
        # --json promises exactly one document, even under --watch.
        _emit(store.get_run_status(args.run_id), True)


def cmd_eval(args: argparse.Namespace) -> None:
    from .eval import RouteEvaluator

    store = _store(args)
    task = _resolve_task(args.task, store)
    input_path, input_options = _input_source(args)
    def input_factory():
        return iter_input_items(input_path, **input_options)

    target_routes = [r.strip() for r in args.routes.split(",") if r.strip()] if getattr(args, "routes", None) else None

    evaluator = RouteEvaluator(task=task, store=store)
    report = evaluator.evaluate_all(
        samples=input_factory(),
        routes=target_routes,
        expected_claims_key=getattr(args, "expected_claims_col", None),
        concurrency=int(getattr(args, "concurrency", 4)),
        samples_factory=input_factory,
    )
    if args.json:
        _emit(report, True)
    else:
        ui.print_eval_table(report)


def cmd_dag(args: argparse.Namespace) -> None:
    from .dag import DagError, DagSpec, run_dag

    try:
        spec = DagSpec.model_validate_json(Path(args.spec).read_text(encoding="utf-8"))
    except Exception as exc:
        raise DagError(f"invalid DAG spec '{args.spec}': {exc}") from exc
    order = spec.topo_order()
    if getattr(args, "dry_run", False):
        _emit(
            {"name": spec.name, "order": order, "nodes": [n.id for n in spec.nodes]},
            args.json,
            f"DAG '{spec.name}' is valid. Execution order: {' -> '.join(order)}.",
        )
        return
    store = _store(args)
    state = run_dag(
        spec,
        store,
        workspace_root=getattr(args, "workspace_root", "."),
        dag_id=getattr(args, "dag_id", None),
        resume=not getattr(args, "no_resume", False),
    )
    _emit(
        state,
        args.json,
        f"DAG '{spec.name}' complete ({state['dag_id']}). "
        + ", ".join(f"{nid}: {info.get('kind')}" for nid, info in state["nodes"].items()),
    )


def cmd_export(args: argparse.Namespace) -> None:
    store = _store(args)
    snapshot = store.run_snapshot(args.run_id)
    fmt = getattr(args, "format", "json") or "json"
    # `run` stores its packet at runs/<run_id>/clean_packet.json. Exporting to
    # the same name with a subset silently rewrote the run's own record (and its
    # audit) — the default is a distinct name, and an explicit path that lands
    # on the stored packet is refused unless --force says otherwise.
    stored_packet = Path(
        (snapshot.get("run") or {}).get("output_path")
        or _workspace_path(f"runs/{args.run_id}/clean_packet.json", getattr(args, "workspace_root", "."))
    )
    default_name = f"runs/{args.run_id}/export.{fmt}"
    output = Path(args.output or default_name)
    if not getattr(args, "force", False) and output.resolve() == stored_packet.resolve():
        raise ValueError(
            f"{output} is this run's stored packet; exporting over it would rewrite the run's "
            "own record. Choose another --output, or pass --force to overwrite it deliberately."
        )
    bias_map: dict[str, float] | None = None
    score_field = getattr(args, "score_field", "score") or "score"
    if getattr(args, "adjust_scores", False):
        try:
            task_name = (snapshot.get("task") or {}).get("name")
            raw_bias = store.get_route_claim_bias(task_name) if task_name else {}
            bias_map = {v["route_id"]: v["bias"] for v in raw_bias.values()}
        except Exception:
            bias_map = None
    sort_by = getattr(args, "sort_by", None)
    sort = SortSpec(field=sort_by, descending=bool(getattr(args, "desc", True))) if sort_by else None
    filter_arg = getattr(args, "filter", None)
    claim_filter = ClaimFilter.model_validate_json(filter_arg) if filter_arg else None
    packet = export_clean_packet(
        snapshot,
        output,
        export_format=fmt,
        sort=sort,
        top=getattr(args, "top", None),
        rank=bool(getattr(args, "rank", False)),
        claim_filter=claim_filter,
        bias_map=bias_map,
        score_field=score_field,
    )
    _emit(
        {"run_id": args.run_id, "output": str(output), "format": fmt, "result": packet},
        args.json,
        f"Exported {packet['total_verified_records']} verified records to {output} (format: {fmt}).",
    )


def cmd_settings(args: argparse.Namespace) -> None:
    """Show or clear the studio's persisted harness/model selection."""
    store = _store(args)
    if getattr(args, "clear", False):
        removed = store.get_studio_selection()
        store.clear_studio_selection()
        _emit({"cleared": bool(removed), "previous": removed}, args.json,
              "Cleared the studio selection." if removed else "No studio selection was set.")
        return
    selection = store.get_studio_selection()
    if selection:
        mode = selection.get("mode") or "free"
        providers = selection.get("providers") or []
        routes = selection.get("routes") or []
        if mode == "free":
            policy = _studio_policy(store, args)
            routes = list(policy.get("allowed_routes") or [])
        message = f"Mode {mode}: {', '.join(providers) or 'no providers'}" + (f" · {len(routes)} route(s)" if routes else "")
        _emit({"mode": mode, "providers": providers, "routes": routes,
               "revision": store.get_studio_revision()},
              args.json, message)
        return
    _emit({"mode": None, "providers": [], "routes": [], "revision": None},
          args.json, "No studio selection has been saved. Open the studio to set one.")


def cmd_schema(args: argparse.Namespace) -> None:
    models: dict[str, Any] = {
        "task": TaskSpec,
        "input": InputItem,
        "candidate-output": CandidateModelOutput,
        "output": ModelOutput,
        "packet": CleanPacket,
        "profile": IdealCompanyProfile,
    }
    from .store import get_database_schema_sql
    schema = (
        {"schema_version": SCHEMA_VERSION, "sql": get_database_schema_sql()}
        if args.kind == "database"
        else models[args.kind].model_json_schema(by_alias=True)
    )
    _emit(schema, True)


def cmd_setup(args: argparse.Namespace) -> None:
    report = setup_workspace(
        scope=args.scope,
        workspace_root=args.workspace_root,
        db_path=args.db,
        skill_root=args.skill_root,
        dry_run=args.dry_run,
        force=args.force,
        refresh_routes=args.refresh_routes,
    )
    next_lines = "\n".join(shlex.join(command) for command in report.next_commands)
    _emit(
        report,
        args.json,
        f"Configured harness-fleet at {report.skill_path}.\n"
        f"Database: {report.database}\nNext:\n{next_lines}",
    )


def cmd_doctor(args: argparse.Namespace) -> None:
    from .providers.registry import HARNESS_SPECS, configured_routes

    store = _store(args)
    catalog = RouteCatalog(db_path=store.path)
    observed_routes = catalog.get_routes(free_only=True)
    openrouter_key = bool(os.environ.get("OPENROUTER_API_KEY"))
    schema_version = store.schema_version()
    # migrate() always writes SCHEMA_VERSION, so a mismatch means the migration
    # did not run or commit. Comparing against that constant keeps this check
    # honest across schema bumps instead of blessing a stale hardcoded list.
    database_ok = schema_version == SCHEMA_VERSION
    checks = [
        DoctorCheck(
            name="database",
            ok=database_ok,
            detail=f"SQLite schema {schema_version} at {store.path.resolve()}"
            + ("" if database_ok else f" (expected {SCHEMA_VERSION}; re-run any command to migrate)"),
        ),
        *(
            DoctorCheck(
                name=spec.name,
                ok=bool(path),
                detail=path or f"{spec.binary} not found in PATH",
            )
            for spec in HARNESS_SPECS
            for path in [shutil.which(spec.binary)]
        ),
        DoctorCheck(name="openrouter", ok=openrouter_key, detail="OPENROUTER_API_KEY configured" if openrouter_key else "optional key not configured"),
        DoctorCheck(name="routes", ok=bool(observed_routes), detail=f"{len(observed_routes)} enabled observed-zero routes"),
    ]
    workspace = Path(args.workspace_root).expanduser().resolve()
    destination = skill_destination(args.scope, Path.home().resolve(), workspace, args.skill_root)
    skill_ok = installed_skill_matches(destination)
    checks.append(DoctorCheck(
        name="skill",
        ok=skill_ok,
        detail=str(destination) if skill_ok else f"missing or outdated at {destination}",
    ))
    usable = configured_routes(observed_routes)
    checks.append(DoctorCheck(name="configured_routes", ok=bool(usable), detail=f"{len(usable)} routes have their own transport configured; live authentication is not tested"))
    ready = checks[0].ok and bool(usable)
    report = DoctorReport(ready=ready, database=str(store.path.resolve()), checks=checks)
    human = "\n".join(f"{'OK' if check.ok else '--'}  {check.name}: {check.detail}" for check in checks)
    _emit(report, args.json, f"{'Ready' if ready else 'Not ready'}\n{human}")
    if not ready:
        raise SystemExit(1)


def cmd_quickstart(args: argparse.Namespace) -> None:
    """Zero-key offline demo: registers demo/fake route, runs bundled examples, writes packet+CSV."""
    store = _store(args)
    catalog = RouteCatalog(db_path=store.path)
    # Ensure demo route exists and is enabled as verified zero-cost
    catalog.add_route(
        route_id="demo/fake",
        provider="demo",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        enabled=True,
        price_state=PriceState.PRICE_OBSERVED_ZERO.value,
        verification_source="quickstart demo (deterministic)",
    )
    # Import here to avoid circular
    from pathlib import Path as _P  # noqa: N814

    # Discover bundled examples
    pkg_root = _P(__file__).resolve().parent
    # Examples are in repo root /examples; try multiple locations
    # Direct paths to sample files
    saas_task = pkg_root.parent / "examples" / "saas_intelligence" / "task.json"
    saas_data = pkg_root.parent / "examples" / "saas_intelligence" / "sample_data.jsonl"
    # Fallback if not found (installed wheel)
    if not saas_task.is_file():
        saas_task = _P.cwd() / "examples" / "saas_intelligence" / "task.json"
    if not saas_data.is_file():
        saas_data = _P.cwd() / "examples" / "saas_intelligence" / "sample_data.jsonl"

    run_id = getattr(args, "run_id", None) or f"demo-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    output = _P(getattr(args, "output", None) or f"runs/{run_id}/clean_packet.json")

    # Choose first available example task/input
    account_example = pkg_root / "resources" / "examples" / "account_research"
    if (account_example / "task.json").is_file():
        saas_task = account_example / "task.json"
        saas_data = account_example / "sample_accounts.csv"
    task_path = saas_task if saas_task.is_file() else None
    input_path = saas_data if saas_data.is_file() else None
    if not task_path or not input_path:
        # Fallback: create synthetic triage task + tiny input
        from .models import TaskSpec as _TS  # noqa: N814
        spec = _TS(
            name="demo-triage",
            instructions="Assign a supported triage priority and explain why.",
            claims_schema={"type": "object", "properties": {"priority": {"enum": ["high", "medium", "low", "unknown"]}, "reason": {"type": "string"}}, "required": ["priority", "reason"], "additionalProperties": False},
        )
        store.register_task(spec)
        items = load_input_items(str(input_path)) if input_path and _P(str(input_path)).is_file() else [
            InputItem(item_id="demo_1", text="The checkout button gave a 500 error and blocks purchases."),
            InputItem(item_id="demo_2", text="Fast shipping and recyclable packaging was appreciated."),
        ]
        # Use Engine with demo registry (registered automatically)
        packet = Engine(task=spec, store=store, policy=RoutePolicy(allowed_routes=["demo/fake"], free_only=True)).run_campaign(
            raw_items=items,
            run_id=run_id,
            input_path=str(input_path) if input_path else "demo-synthetic",
            concurrency=2,
            max_attempts=10,
            output_packet_path=output,
        )
    else:
        spec = load_task_spec(task_path)
        items = load_input_items(str(input_path))
        packet = Engine(task=spec, store=store, policy=RoutePolicy(allowed_routes=["demo/fake"], free_only=True)).run_campaign(
            raw_items=items,
            run_id=run_id,
            input_path=str(input_path.resolve()),
            concurrency=2,
            max_attempts=10,
            output_packet_path=output,
        )

    # Also export CSV alongside JSON
    csv_output = output.with_suffix(".csv")
    snapshot = store.run_snapshot(run_id)
    from .export import export_clean_packet as _export
    _export(snapshot, csv_output, export_format="csv")

    _emit(
        {"run_id": run_id, "packet": str(output.resolve()), "csv": str(csv_output.resolve()), "result": packet, "verified": packet["total_verified_records"]},
        args.json,
        f"Demo run '{run_id}' completed.\nPacket: {output.resolve()}\nCSV: {csv_output.resolve()}\nVerified records: {packet['total_verified_records']}\nTry: harness-fleet status {run_id} --json | harness-fleet export {run_id} --format jsonl",
    )


def cmd_db_backup(args: argparse.Namespace) -> None:
    store = _store(args)
    dest = Path(args.destination)
    # Use SQLite backup API (safe while running under WAL)
    import sqlite3

    dest.parent.mkdir(parents=True, exist_ok=True)
    with store.connect() as src:
        with sqlite3.connect(str(dest)) as dst:
            src.backup(dst)
    size = dest.stat().st_size if dest.is_file() else 0
    _emit(
        {"source": str(store.path.resolve()), "destination": str(dest.resolve()), "bytes": size},
        args.json,
        f"Backup created: {dest.resolve()} ({size} bytes) from {store.path.resolve()}",
    )


def cmd_serve(args: argparse.Namespace) -> None:
    from .mcp_server import run_mcp_server

    run_mcp_server(args.workspace_root, args.db)


def cmd_partners(args: argparse.Namespace) -> None:
    """Run the partner sourcing plan: find candidates, or enrich one partner."""
    from .bundler import export_bundled_csv
    from .partner_sourcing import enrich_partner, find_partners, load_plan

    plan = load_plan(getattr(args, "plan", None))
    output = Path(getattr(args, "output", None) or (
        "partners.csv" if args.partners_command == "find" else f"partner-{args.domain}.csv"
    ))
    backends = getattr(args, "backend", None) or None
    max_per_query = int(getattr(args, "max", 8) or 8)
    delay = float(getattr(args, "delay", 1.0) or 0.0)
    snippets_only = bool(getattr(args, "snippets_only", False))
    if args.partners_command == "find":
        items, report = find_partners(
            plan=plan,
            backends=backends,
            max_per_query=max_per_query,
            delay=delay,
            snippets_only=snippets_only,
            tech=getattr(args, "tech", "") or "",
            vertical=getattr(args, "vertical", "") or "",
            subreddits=getattr(args, "subreddit", None) or [],
        )
    else:
        items, report = enrich_partner(
            args.domain,
            plan=plan,
            backends=backends,
            max_per_query=max_per_query,
            delay=delay,
            snippets_only=snippets_only,
            max_pages=int(getattr(args, "max_pages", 8) or 8),
            include_fetch=not bool(getattr(args, "no_fetch", False)),
        )
    # Every backend skipped means the run could not search at all: reporting
    # "Found 0 partner dossier(s)" with exit 0 hides a missing dependency.
    backend_skips = [entry for entry in report.skipped if entry.get("backend")]
    if not items and backend_skips and len(backend_skips) >= max(1, report.searched):
        first = backend_skips[0].get("reason", "no backend could be reached")
        raise ValueError(
            f"every search backend was unavailable ({len(backend_skips)} of {report.searched} "
            f"searches skipped): {first}"
        )
    path = export_bundled_csv(items, output)
    payload = {"output": str(path), "partners": len(items), "report": report.as_dict()}
    _emit(
        payload,
        args.json,
        f"{'Found' if args.partners_command == 'find' else 'Enriched'} {len(items)} partner dossier(s) -> {path}"
        + (f" ({report.searched} searches, {report.dropped_unattributed} unattributed hits dropped)"
           if report.searched or report.dropped_unattributed else "")
        + _format_skips(report.skipped),
    )


def cmd_studio(args: argparse.Namespace) -> None:
    from .studio import run_studio_server

    run_studio_server(args.workspace_root, args.db, int(args.port))


def cmd_board(args: argparse.Namespace) -> None:
    """Serve the read-only results board, or print its payload with --json."""
    from .board import build_board_payload, run_board_server

    if getattr(args, "json", False):
        store = _store(args)
        _emit(build_board_payload(store.path, args.run_id), True, "")
        return
    store = _store(args)
    run_board_server(
        store.path,
        args.run_id,
        port=int(args.port),
        host=args.host,
        open_browser=bool(getattr(args, "open", False)),
    )


def _claude_config_candidates() -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates.append(Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Claude" / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        candidates.extend(config_root / name / "claude_desktop_config.json" for name in ("Claude", "claude"))
    return candidates


def _cursor_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


# Provider keys this codebase already reads from the environment (the
# `<PROVIDER>_API_KEY` / `<PROVIDER>_BASE_URL` convention in
# providers/openai_compatible.py plus the doctor's OpenRouter check). Desktop
# clients do not inherit the shell environment, so these are the variables a user
# most likely needs to hand to the MCP server with `mcp install --env`.
MCP_ENV_KNOWN_PROVIDER_VARS: tuple[str, ...] = (
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "OLLAMA_BASE_URL",
)


def _redacted_server_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy of the server entry that never carries secret values.

    The env block is written into the client config on purpose, but the same entry
    is echoed by `--json` and `--dry-run`, so values must not travel with it.
    """
    if "env" not in entry:
        return entry
    return {**entry, "env": {name: "<redacted>" for name in entry["env"]}}


def _existing_mcp_path_for_client(client: str, workspace_root: Path | None = None) -> Path | None:
    # Try to find existing config; if not found, return default path for that client
    if client == "claude":
        for p in _claude_config_candidates():
            if p.is_file():
                return p
        return _claude_config_candidates()[0]
    if client == "cursor":
        return _cursor_config_path()
    return None


def cmd_mcp_install(args: argparse.Namespace) -> None:
    from .setup import installed_cli_path

    workspace_root = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    if not workspace_root.is_dir():
        raise ValueError(f"workspace root not found: {workspace_root}")
    configured_db = (
        getattr(args, "db", None)
        or os.environ.get("HARNESS_FLEET_DB")
    )
    db_path = Path(configured_db).expanduser() if configured_db else workspace_root / "harness-fleet.db"
    db_path = (db_path if db_path.is_absolute() else workspace_root / db_path).resolve()
    if not db_path.is_relative_to(workspace_root):
        raise ValueError("MCP database must stay below the workspace root")

    cli_cmd = installed_cli_path()
    server_entry: dict[str, Any] = {
        "command": cli_cmd,
        "args": ["serve", "--workspace-root", str(workspace_root), "--db", str(db_path)],
    }

    # --env NAME copies this shell's value into the client config, because desktop
    # apps do not inherit the shell environment. Missing names are skipped, never
    # fatal, and only NAMES ever reach human or JSON output.
    requested_env = _split_csv_list(getattr(args, "env", None)) or []
    resolved_env: dict[str, str] = {}
    missing_env: list[str] = []
    for name in dict.fromkeys(requested_env):
        value = os.environ.get(name, "")
        if value:
            resolved_env[name] = value
        else:
            missing_env.append(name)
    if resolved_env:
        server_entry["env"] = resolved_env

    target_clients: list[str] = []
    requested = (getattr(args, "client", "auto") or "auto").lower()
    if requested == "auto":
        # Detect existing configs; if none, default to claude
        found = []
        for cand in ["claude", "cursor"]:
            p = _existing_mcp_path_for_client(cand, workspace_root)
            if p and p.is_file():
                found.append(cand)
        target_clients = found if found else ["claude"]
    elif requested in ("claude", "cursor"):
        target_clients = [requested]
    elif requested == "all":
        target_clients = ["claude", "cursor"]
    else:
        raise ValueError(f"unknown --client '{requested}'; use auto, claude, cursor, or all")

    results: list[dict[str, Any]] = []
    notes: list[str] = []
    for client in target_clients:
        config_path = _existing_mcp_path_for_client(client, workspace_root)
        if config_path is None:
            continue
        dry_run = bool(getattr(args, "dry_run", False))
        # Load existing config or create new
        existing: dict[str, Any] = {}
        if config_path.is_file():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    raise ValueError("config must contain a JSON object")
            except (ValueError, OSError) as exc:
                raise ValueError(f"Cannot read existing config {config_path}: {exc}; file was not changed") from exc

        servers = existing.get("mcpServers")
        if "mcpServers" in existing and not isinstance(servers, dict):
            raise ValueError(f"Invalid mcpServers in config {config_path}; file was not changed")
        if servers is None:
            servers = {}
            existing["mcpServers"] = servers

        already = servers.get("harness-fleet")
        force = bool(getattr(args, "force", False))
        needs_update = force or already != server_entry
        if not needs_update:
            status = "unchanged"
        elif dry_run:
            status = "planned"
        else:
            status = "updated" if already is not None else "created"
        # A plain re-run drops env keys an earlier --env opted into; record them so
        # the output can say so instead of silently rewriting without them.
        previous_env = already.get("env") if isinstance(already, dict) else None
        dropped_env = sorted(set(previous_env) - set(resolved_env)) if isinstance(previous_env, dict) else []

        if needs_update and not dry_run:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            servers["harness-fleet"] = server_entry
            # Preserve other keys (e.g., globalShortcut)
            config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            notes.extend(
                f"env: {name} dropped from the {client} config (not requested in this run)"
                for name in dropped_env
            )

        results.append({
            "client": client,
            "config_path": str(config_path),
            "status": status,
            "server": _redacted_server_entry(server_entry),
            "env": sorted(resolved_env),
            "env_missing": missing_env,
            "env_dropped": dropped_env,
            "exists": config_path.is_file(),
        })

    summary_lines = [
        f"{r['client']}: {r['status']} at {r['config_path']}\n  -> {r['server']['command']} {' '.join(r['server']['args'])}"
        for r in results
    ]
    summary_lines.extend(f"env: {name} (set)" for name in resolved_env)
    summary_lines.extend(f"env: {name} requested but not set in this shell; skipping" for name in missing_env)
    summary_lines.extend(notes)
    if not requested_env:
        # Opt-in only: writing a key into a plaintext config must never be implicit.
        present = [name for name in MCP_ENV_KNOWN_PROVIDER_VARS if os.environ.get(name)]
        if present:
            summary_lines.append(
                f"tip: {', '.join(present)} is set in this shell but was not passed to the client "
                f"(desktop apps do not inherit your shell environment). Add it with: "
                f"harness-fleet mcp install --env {present[0]}"
            )
    _emit(
        {"installed": results, "workspace_root": str(workspace_root), "db": str(db_path), "command": cli_cmd},
        args.json,
        "\n".join(summary_lines) + f"\nRestart {', '.join(r['client'] for r in results)} to load harness-fleet. Verify with: harness-fleet doctor --workspace-root {workspace_root} --json",
    )


def cmd_mcp(args: argparse.Namespace) -> None:
    sub = getattr(args, "mcp_command", None)
    if sub == "install":
        cmd_mcp_install(args)
        return
    raise ValueError(f"unknown mcp subcommand '{sub}'")


def _write_discovered(
    items: list,
    output: str | None,
    fmt: str,
    default_name: str,
    skipped: list[dict[str, str]] | None = None,
) -> str:
    if not items:
        detail = ""
        if skipped:
            reasons = "; ".join(
                f"{entry.get('url') or entry.get('query') or entry.get('source')}: {entry.get('reason')}"
                for entry in skipped[:3]
            )
            detail = f" ({reasons})"
        raise DiscoverError(f"no items discovered{detail}; see 'skipped' in --json output for full detail")
    writer = write_items_jsonl if fmt == "jsonl" else write_items_csv
    return str(writer(items, output or default_name))


def _format_skips(skipped: list[dict[str, str]], limit: int = 3) -> str:
    if not skipped:
        return ""
    lines = "\n".join(
        f"  - {entry.get('url') or entry.get('query') or entry.get('source')}: {entry.get('reason')}"
        for entry in skipped[:limit]
    )
    extra = f"\n  ... and {len(skipped) - limit} more (see --json)" if len(skipped) > limit else ""
    return f"\nSkipped ({len(skipped)}):\n{lines}{extra}"


def _coverage_value(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number between 0.0 and 1.0") from exc
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be a number between 0.0 and 1.0")
    return parsed


def _capture_quality(captured: int, skipped: int, threshold: float | None) -> dict[str, Any]:
    attempted = captured + skipped
    coverage = captured / attempted if attempted else 0.0
    return {
        "threshold": threshold,
        "attempted": attempted,
        "captured": captured,
        "coverage": round(coverage, 3),
        "meets_threshold": threshold is None or (attempted > 0 and coverage >= threshold),
    }


def cmd_discover(args: argparse.Namespace) -> None:
    backends = args.backend or ["ddgs", "hn"]
    fmt = getattr(args, "format", "csv") or "csv"
    snippets_only = bool(getattr(args, "snippets_only", False))
    min_source_coverage = getattr(args, "min_source_coverage", None)
    items, report = run_discovery(
        queries=args.query,
        backends=backends,
        max_results=args.max_results,
        fetch_full_text=not snippets_only,
        searxng_url=getattr(args, "searxng_url", None),
        timeout=float(getattr(args, "timeout", 20.0) or 20.0),
        delay=float(getattr(args, "delay", 1.0) or 0.0),
        respect_robots=not getattr(args, "ignore_robots", False),
        max_chars=getattr(args, "max_chars", None),
        render_js=bool(getattr(args, "js", False)),
        reddit_subreddits=getattr(args, "subreddit", None) or [],
        se_tagged=getattr(args, "se_tagged", None) or [],
        se_site=getattr(args, "se_site", None) or "stackoverflow",
        discourse_url=getattr(args, "discourse_url", None),
        lemmy_instance=getattr(args, "lemmy_instance", None) or "https://programming.dev",
        min_source_coverage=min_source_coverage,
        min_chars=getattr(args, "min_chars", None),
        allowed_evidence=getattr(args, "evidence", None),
        required_stack=getattr(args, "require_stack", None) or [],
        excluded_stack=getattr(args, "exclude_stack", None) or [],
    )
    source_quality = report.get("source_quality") or {}
    if min_source_coverage is not None and source_quality and not source_quality.get("meets_threshold", False):
        coverage = float(source_quality.get("coverage", 0.0))
        captured = int(source_quality.get("captured", len(items)))
        attempted = int(source_quality.get("attempted", report.get("hits", 0)))
        raise DiscoverError(
            f"source coverage {coverage:.1%} ({captured}/{attempted}) is below the "
            f"minimum {float(min_source_coverage):.1%}; narrow the query or use a healthier source"
        )
    output = _write_discovered(items, getattr(args, "output", None), fmt, f"accounts.{fmt}", report["skipped"])
    skipped = report["skipped"]
    indicator_note = (
        "\nNote: --snippets-only records are triage indicators (evidence=indicator), "
        "not grounding-grade. Re-run without it for full text."
        if snippets_only else ""
    )
    _emit(
        {"items": len(items), "output": output, "format": fmt, "report": report},
        args.json,
        f"Discovered {len(items)} items from {report['hits']} hits -> {output}."
        + (
            f" Source coverage: {float(source_quality.get('coverage', 0.0)):.1%}"
            f" ({source_quality.get('captured', len(items))}/{source_quality.get('attempted', report['hits'])})."
            if source_quality else ""
        )
        + _format_skips(skipped) + indicator_note,
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    import httpx as _httpx

    fmt = getattr(args, "format", "csv") or "csv"
    timeout = float(getattr(args, "timeout", 20.0) or 20.0)
    delay = float(getattr(args, "delay", 1.0) or 0.0)
    respect_robots = not getattr(args, "ignore_robots", False)
    max_jobs = getattr(args, "max_jobs", None)

    urls: list[str] = list(getattr(args, "url", None) or [])
    url_file = getattr(args, "url_file", None)
    if url_file:
        try:
            urls.extend(line.strip() for line in Path(url_file).read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError as exc:
            raise DiscoverError(f"cannot read --url-file '{url_file}': {exc.strerror or exc}") from exc

    greenhouse = getattr(args, "greenhouse_board", None)
    ashby = getattr(args, "ashby_org", None)
    lever = getattr(args, "lever_org", None)
    yc = bool(getattr(args, "yc", False))
    sitemap = getattr(args, "sitemap", None)
    site = getattr(args, "site", None)
    render_js = bool(getattr(args, "js", False))
    subreddits = getattr(args, "subreddit", None) or []
    reddit_query = getattr(args, "reddit_query", None)
    hn_refs = getattr(args, "hn", None) or []
    max_comments = int(getattr(args, "max_comments", 50) or 50)
    se_query = getattr(args, "stackexchange_query", None)
    se_tagged = getattr(args, "se_tag", None) or []
    se_site = getattr(args, "se_site", None) or "stackoverflow"
    se_answers = bool(getattr(args, "se_answers", False))
    discourse = getattr(args, "discourse", None)
    discourse_query = getattr(args, "discourse_query", None)
    lobsters_tag = getattr(args, "lobsters_tag", None)
    lemmy_query = getattr(args, "lemmy_query", None)
    lemmy_instance = getattr(args, "lemmy_instance", None) or "https://programming.dev"
    devto_tag = getattr(args, "devto_tag", None)
    has_qa = any([se_query, discourse, lobsters_tag is not None, lemmy_query, devto_tag])
    if not urls and not sitemap and not site and not subreddits and not reddit_query and not hn_refs and not has_qa and not greenhouse and not ashby and not lever and not yc:
        raise DiscoverError("fetch requires --url, --url-file, --sitemap, --site, --subreddit, --reddit-query, --hn, a Q&A source, --greenhouse-board, --ashby-org, --lever-org, or --yc")
    if render_js:
        from .discover import require_playwright

        require_playwright()

    records: list = []
    skipped: list[dict[str, str]] = []
    ats_sources: list[tuple[str, Any, str]] = []
    if greenhouse:
        ats_sources.append((f"greenhouse:{greenhouse}", fetch_greenhouse_board, greenhouse))
    if ashby:
        ats_sources.append((f"ashby:{ashby}", fetch_ashby_org, ashby))
    if lever:
        ats_sources.append((f"lever:{lever}", fetch_lever_org, lever))
    with _httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": DISCOVER_USER_AGENT}) as client:
        if sitemap:
            try:
                urls.extend(fetch_sitemap_urls(sitemap, client=client, timeout=timeout,
                                               max_urls=max_jobs or 200))
            except DiscoverError as exc:
                skipped.append({"source": f"sitemap:{sitemap}", "reason": str(exc)})
        if site:
            try:
                sitemap_url = discover_sitemap_url(site, client=client, timeout=timeout)
                urls.extend(fetch_sitemap_urls(sitemap_url, client=client, timeout=timeout,
                                               max_urls=max_jobs or 200))
            except DiscoverError:
                site_records, site_skipped = crawl_site(
                    site if "://" in site else f"https://{site}",
                    max_pages=int(getattr(args, "max_pages", 20) or 20),
                    max_depth=int(getattr(args, "max_depth", 2) or 0),
                    timeout=timeout, delay=delay, respect_robots=respect_robots,
                    render_js=render_js, client=client,
                )
                records.extend(site_records)
                skipped.extend(site_skipped)
        if yc:
            try:
                before = len(records)
                records.extend(fetch_yc_companies(
                    query=getattr(args, "yc_query", None),
                    batch=getattr(args, "yc_batch", None),
                    tags=getattr(args, "yc_tag", None) or [],
                    max_companies=max_jobs, timeout=timeout, client=client,
                ))
                if len(records) == before:
                    skipped.append({"source": "ycombinator",
                                    "reason": "0 companies matched (widen --yc-query/--yc-batch/--yc-tag)"})
            except DiscoverError as exc:
                skipped.append({"source": "ycombinator", "reason": str(exc)})
        ats_kwargs = {
            "title_include": getattr(args, "title_include", None) or [],
            "title_exclude": getattr(args, "title_exclude", None) or [],
            "required_stack": getattr(args, "require_stack", None) or [],
            "excluded_stack": getattr(args, "exclude_stack", None) or [],
        }
        for source, fetcher, ref in ats_sources:
            try:
                before = len(records)
                records.extend(fetcher(ref, max_jobs=max_jobs, timeout=timeout, client=client, **ats_kwargs))
                if len(records) == before:
                    skipped.append({"source": source, "reason": "0 postings (empty board?)"})
            except DiscoverError as exc:
                skipped.append({"source": source, "reason": str(exc)})
        for sub in subreddits:
            try:
                before = len(records)
                records.extend(fetch_reddit_rss(sub, sort=getattr(args, "subreddit_sort", "new") or "new",
                                                max_results=max_jobs or 25, timeout=timeout, client=client))
                if len(records) == before:
                    skipped.append({"source": f"reddit-rss:{sub}", "reason": "0 posts"})
            except DiscoverError as exc:
                skipped.append({"source": f"reddit-rss:{sub}", "reason": str(exc)})
        if reddit_query:
            try:
                before = len(records)
                records.extend(fetch_reddit_posts(reddit_query, subreddits=subreddits,
                                                  max_posts=max_jobs, timeout=timeout, client=client))
                if len(records) == before:
                    skipped.append({"source": "reddit-search", "reason": "0 posts matched"})
            except DiscoverError as exc:
                skipped.append({"source": "reddit-search", "reason": str(exc)})
        if se_query:
            try:
                before = len(records)
                records.extend(fetch_stackexchange_questions(
                    se_query, tagged=se_tagged, site=se_site, max_questions=max_jobs,
                    include_answers=se_answers, timeout=timeout, client=client))
                if len(records) == before:
                    skipped.append({"source": f"stackexchange:{se_site}", "reason": "0 questions matched"})
            except DiscoverError as exc:
                skipped.append({"source": f"stackexchange:{se_site}", "reason": str(exc)})
        if discourse:
            try:
                before = len(records)
                micro_records, micro_skipped = fetch_discourse_search(
                    discourse, discourse_query, max_topics=max_jobs, max_posts_each=max_comments,
                    timeout=timeout, client=client)
                records.extend(micro_records)
                skipped.extend(micro_skipped)
                if len(records) == before and not micro_skipped:
                    skipped.append({"source": f"discourse:{discourse}", "reason": "0 topics matched"})
            except DiscoverError as exc:
                skipped.append({"source": f"discourse:{discourse}", "reason": str(exc)})
        if lobsters_tag is not None:
            try:
                before = len(records)
                records.extend(fetch_lobsters(tag=lobsters_tag or None, max_results=max_jobs or 25,
                                              timeout=timeout, client=client))
                if len(records) == before:
                    skipped.append({"source": "lobsters", "reason": "0 stories"})
            except DiscoverError as exc:
                skipped.append({"source": "lobsters", "reason": str(exc)})
        if lemmy_query:
            try:
                before = len(records)
                records.extend(fetch_lemmy(lemmy_query, instance=lemmy_instance,
                                           max_results=max_jobs or 25, timeout=timeout, client=client))
                if len(records) == before:
                    skipped.append({"source": f"lemmy:{lemmy_instance}", "reason": "0 posts matched"})
            except DiscoverError as exc:
                skipped.append({"source": f"lemmy:{lemmy_instance}", "reason": str(exc)})
        if devto_tag:
            try:
                before = len(records)
                records.extend(fetch_devto_tag(devto_tag, max_articles=max_jobs,
                                               timeout=timeout, client=client))
                if len(records) == before:
                    skipped.append({"source": "dev.to", "reason": "0 articles"})
            except DiscoverError as exc:
                skipped.append({"source": "dev.to", "reason": str(exc)})
        for ref in hn_refs:
            try:
                records.append(fetch_hn_thread(ref, max_comments=max_comments, timeout=timeout, client=client))
            except DiscoverError as exc:
                skipped.append({"source": f"hn:{ref}", "reason": str(exc)})
        for url in urls:
            try:
                records.append(fetch_smart_url(url, client=client, respect_robots=respect_robots,
                                               render_js=render_js))
            except DiscoverError as exc:
                skipped.append({"url": url, "reason": str(exc)})
            if delay > 0:
                import time as _time

                _time.sleep(delay)

    items = to_input_items(
        records,
        max_chars=getattr(args, "max_chars", None),
        min_chars=getattr(args, "min_chars", None),
        allowed_evidence=getattr(args, "evidence", None),
        required_stack=getattr(args, "require_stack", None) or [],
        excluded_stack=getattr(args, "exclude_stack", None) or [],
    )
    min_source_coverage = getattr(args, "min_source_coverage", None)
    source_quality = _capture_quality(len(items), len(skipped), min_source_coverage)
    if min_source_coverage is not None and not source_quality["meets_threshold"]:
        raise DiscoverError(
            f"source coverage {source_quality['coverage']:.1%} "
            f"({source_quality['captured']}/{source_quality['attempted']}) is below the "
            f"minimum {float(min_source_coverage):.1%}; fix or narrow the source set"
        )
    output = _write_discovered(items, getattr(args, "output", None), fmt, f"fetched.{fmt}", skipped)
    _emit(
        {"items": len(items), "output": output, "format": fmt, "skipped": skipped,
         "source_quality": source_quality},
        args.json,
        f"Fetched {len(items)} items -> {output}."
        f" Source coverage: {source_quality['coverage']:.1%}"
        f" ({source_quality['captured']}/{source_quality['attempted']})."
        + _format_skips(skipped),
    )


def _input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id-column", help="CSV column to use for item ID")
    parser.add_argument("--text-column", help="CSV column to use for source text")
    parser.add_argument("--title-column", help="Optional CSV column for item title")
    parser.add_argument("--uri-column", help="Optional CSV column for source URI")
    parser.add_argument("--only-ids", help="Optional file (CSV, JSONL, TXT) or comma-separated list of IDs to restrict input items to")
    parser.add_argument("--only-ids-fuzzy", action="store_true", help="Allow legacy underscore/space ID equivalence (default: exact ID match)")


def _policy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transport", "--provider", dest="provider", action="append", help="Allow specific transport provider (can repeat or comma-separate)")
    parser.add_argument("--exclude-transport", "--exclude-provider", dest="exclude_provider", action="append", help="Exclude specific transport provider (can repeat or comma-separate)")
    parser.add_argument("--route", action="append", help="Allow specific route ID (can repeat or comma-separate). Pin a single --route for Phase-B judge scoring so ranked deliverables use one rater.")
    parser.add_argument("--exclude-route", action="append", help="Exclude specific route ID (can repeat or comma-separate)")
    parser.add_argument("--zdr", action="store_true", help="Require Zero Data Retention upstream")
    parser.add_argument("--no-data-collection", action="store_true", help="Deny provider data collection")
    parser.add_argument("--free-only", action="store_true", help="Restrict to verified zero-cost routes only (explicit; replaces implicit max-cost=0 sentinel)")
    parser.add_argument("--max-cost-in", type=float, default=0.0, help="Max cost per 1k input tokens (default: 0.0, deprecated: use --free-only)")
    parser.add_argument("--max-cost-out", type=float, default=0.0, help="Max cost per 1k output tokens (default: 0.0, deprecated: use --free-only)")
    parser.add_argument("--max-request-cost", type=float, default=None, help="Max allowed spend per single request (default: unlimited)")
    parser.add_argument("--openrouter-provider", "--openrouter-providers", dest="openrouter_providers", action="append", help="Upstream OpenRouter inference host preference (can repeat or comma-separate, e.g. Together, DeepInfra)")
    parser.add_argument("--openrouter-order", action="append", help="Upstream OpenRouter provider order preference (can repeat or comma-separate)")
    parser.add_argument("--openrouter-ignore", action="append", help="Upstream OpenRouter inference host to ignore (can repeat or comma-separate)")


def _common(parser: argparse.ArgumentParser, *, json_output: bool = True, database: bool = True) -> None:
    if database:
        parser.add_argument("--db", help="SQLite control-plane path (default: ./harness-fleet.db)")
    if json_output:
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-fleet",
        description="Coordinated free LLM worker fleet for bulk classification, extraction, summarization, and triage",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    parser.add_argument("--json", dest="global_json", action="store_true", help="Emit machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="Bootstrap a portable harness workspace")
    setup.add_argument("--scope", choices=["user", "project"], default="project")
    setup.add_argument("--workspace-root", default=".")
    setup.add_argument("--skill-root", help="Advanced: nonstandard parent directory for installed skills")
    setup.add_argument("--db", help="SQLite path below the workspace root")
    setup.add_argument("--refresh-routes", action="store_true", help="Contact providers and record current pricing")
    setup.add_argument("--dry-run", action="store_true", help="Return the setup plan without writing")
    setup.add_argument("--force", action="store_true", help="Update managed skill files when the destination differs")
    setup.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    routes = commands.add_parser("routes", help="List, add, or refresh model routes")
    routes.add_argument("action", nargs="?", choices=["list", "add", "refresh"], default="list", help="Action: list, add, or refresh")
    routes.add_argument("route_id", nargs="?", help="Route ID to add (e.g. ollama/llama3.2, groq/llama-3.3-70b)")
    routes.add_argument("--add", help="Route ID to add")
    routes.add_argument("--provider", help="Provider for added route (e.g. ollama, openai_compatible, groq)")
    routes.add_argument("--free", action="store_true", help="Mark added route as observed zero price")
    routes.add_argument("--input-cost", type=float, default=None, help="Cost per 1k input tokens")
    routes.add_argument("--output-cost", type=float, default=None, help="Cost per 1k output tokens")
    routes.add_argument("--disable", action="store_true", help="Register route as disabled")
    routes.add_argument("--all", action="store_true", help="Include non-zero, candidate, and disabled routes")
    routes.add_argument("--refresh", action="store_true", help="Contact providers and update route observations")
    _common(routes)

    cooldowns = commands.add_parser("cooldowns", help="Inspect and manage rate-limit route cooldowns")
    cooldowns.add_argument("--clear", action="store_true", help="Clear active cooldowns")
    cooldowns.add_argument("--route", help="Specific route ID to clear")
    _common(cooldowns)

    settings = commands.add_parser("settings", help="Show or clear the studio's saved harness/model selection")
    settings.add_argument("--clear", action="store_true", help="Forget the saved selection")
    _common(settings)

    tasks = commands.add_parser("tasks", help="List registered task definitions")
    _common(tasks)

    init = commands.add_parser("init", help="Create a typed task from a preset (or --from-example)")
    init.add_argument("name")
    init.add_argument("--preset", choices=sorted(PRESETS), default="classify")
    init.add_argument("--from-example", dest="from_example", help="Infer draft claims_schema from a labeled CSV/JSONL (e.g. labels.csv)")
    init.add_argument("--label-column", help="Column containing labels in --from-example (auto-detected if omitted)")
    init.add_argument("--batch-size", type=int, default=4)
    init.add_argument("--source-weight", action="append", default=[], metavar="MATCH=WEIGHT",
                      help="Repeatable source-evidence weight 0-1, longest URI-substring match wins (e.g. --source-weight boards.greenhouse.io=1 --source-weight aggregator.example=0.4)")
    init.add_argument("--half-life", action="append", default=[], metavar="ITEM=DAYS",
                      help="Repeatable evidence half-life in days per checklist item; refines preset defaults (e.g. --half-life hiring_or_trigger=21)")
    init.add_argument("--sample", help="Sample input path")
    init.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _common(init)

    profile = commands.add_parser("profile", help="Create or inspect the Ideal Company Profile")
    profile.add_argument("--path", default="ideal_company_profile.json", help="Path to the Ideal Company Profile JSON")
    profile.add_argument("--init", action="store_true", help="Generate a fresh default profile")
    profile.add_argument("--force", action="store_true", help="Replace an existing profile when used with --init")
    profile.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _common(profile)

    validate = commands.add_parser("validate", help="Validate a task and input without inference")
    validate.add_argument("task", help="Registered task name or TaskSpec JSON path")
    validate.add_argument("--input", required=True)
    validate.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _input_options(validate)
    _common(validate)

    quickstart = commands.add_parser("quickstart", help="One-command offline demo (no API keys)")
    quickstart.add_argument("--demo", action="store_true", help="Run bundled examples with deterministic fake provider")
    quickstart.add_argument("--run-id", help="Custom run ID (default: demo-<timestamp>)")
    quickstart.add_argument("--output", help="Packet output path (default: runs/<run_id>/clean_packet.json)")
    _common(quickstart)

    db_parser = commands.add_parser("db", help="Database utilities")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    backup = db_sub.add_parser("backup", help="Create a SQLite backup file (safe while running)")
    backup.add_argument("destination", help="Destination file path for the backup (e.g. ./backup.db)")
    # Flags live on the subparser only: parent-parser defaults would overwrite
    # them and silently back up the wrong database.
    _common(backup, database=True)

    test = commands.add_parser("test", help="Run one real inference batch")
    test.add_argument("task", help="Registered task name or TaskSpec JSON path")
    test.add_argument("--input", required=True)
    _input_options(test)
    _policy_options(test)
    _common(test)

    run = commands.add_parser("run", help="Create and execute a resumable run")
    run.add_argument("task", help="Registered task name or TaskSpec JSON path")
    run.add_argument("--input", required=True)
    run.add_argument("--sessions", type=_positive_int, default=4)
    run.add_argument("--max-attempts", type=_positive_int, default=300)
    run.add_argument(
        "--timeout", type=_positive_int, default=DEFAULT_PROMPT_TIMEOUT_SEC,
        help=f"Seconds to allow one model attempt before it is abandoned (default {DEFAULT_PROMPT_TIMEOUT_SEC}; "
             "raise it for slow free routes, lower it to fail fast)",
    )
    run.add_argument("--run-id")
    run.add_argument("--output")
    run.add_argument("--profile", help="Optional Ideal Company Profile JSON; persist and attach its revision to this run")
    run.add_argument("--use-active-profile", action="store_true", help="Explicitly attach the active Ideal Company Profile from SQLite")
    run.add_argument("--from-studio", action="store_true", help="Route this run through the studio's saved harness/model selection")
    run.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _input_options(run)
    _policy_options(run)
    _common(run)

    resume = commands.add_parser("resume", help="Resume a run from its SQLite queue")
    resume.add_argument("run_id")
    resume.add_argument("--sessions", type=_positive_int, default=4)
    resume.add_argument("--output")
    _policy_options(resume)
    _common(resume)

    rescore = commands.add_parser("rescore", help="Score fresh evidence as a new run linked to a parent run")
    rescore.add_argument("parent_run", help="Existing run this round builds on (lineage only; evidence comes from --input)")
    rescore.add_argument("--task", help="Registered task name or TaskSpec JSON path (default: the parent run's task; override to rescore under recalibrated weights)")
    rescore.add_argument("--input", required=True)
    rescore.add_argument("--sessions", type=_positive_int, default=4)
    rescore.add_argument("--max-attempts", type=_positive_int, default=300)
    rescore.add_argument("--timeout", type=_positive_int, default=DEFAULT_PROMPT_TIMEOUT_SEC,
                         help=f"Seconds to allow one model attempt (default {DEFAULT_PROMPT_TIMEOUT_SEC})")
    rescore.add_argument("--run-id")
    rescore.add_argument("--output")
    rescore.add_argument("--profile", help="Optional Ideal Company Profile JSON; persist and attach its revision to this run")
    rescore.add_argument("--use-active-profile", action="store_true", help="Explicitly attach the active Ideal Company Profile from SQLite")
    rescore.add_argument("--from-studio", action="store_true", help="Route this run through the studio's saved harness/model selection")
    rescore.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _input_options(rescore)
    _policy_options(rescore)
    _common(rescore)

    history = commands.add_parser("history", help="Show an entity's score trajectory across rescore rounds")
    history.add_argument("entity", help="Entity key (InputItem metadata.entity, else the item_id)")
    _common(history)

    calibrate = commands.add_parser("calibrate", help="Fit checklist points, source weights, and half-lives against labeled samples (dry-run unless --apply)")
    calibrate.add_argument("task", help="Registered task name or TaskSpec JSON path (must carry a checklist)")
    calibrate.add_argument("--input", required=True, help="Labeled samples; metadata holds the expected score")
    calibrate.add_argument("--expected", default="score", help="Metadata key holding the expected numeric score")
    calibrate.add_argument("--route", required=True, help="Single rater route for observation collection")
    calibrate.add_argument("--params", default=",".join(PARAM_KINDS), help="Comma-separated subset of points,weights,halves to fit")
    calibrate.add_argument("--max-sweeps", type=int, default=50)
    calibrate.add_argument("--apply", action="store_true", help="Register the recalibrated task revision (default: dry-run report only)")
    calibrate.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _input_options(calibrate)
    _common(calibrate)

    status = commands.add_parser("status", help="Show real-time progress, attempts, and route stats for a run")
    status.add_argument("run_id")
    status.add_argument("--watch", action="store_true", help="Live monitor run progress until completion")
    status.add_argument("--interval", type=float, default=2.0, help="Watch refresh interval in seconds")
    _common(status)

    eval_cmd = commands.add_parser("eval", help="Benchmark routes against test samples and update intelligent route scores")
    eval_cmd.add_argument("task", help="Registered task name or TaskSpec JSON path")
    eval_cmd.add_argument("--input", required=True, help="Evaluation dataset (CSV, JSONL, or JSON)")
    eval_cmd.add_argument("--routes", help="Optional comma-separated route IDs to test")
    eval_cmd.add_argument("--expected-claims-col", help="Column/metadata key containing ground-truth claims")
    eval_cmd.add_argument("--concurrency", type=_positive_int, default=4, help="Parallel sample evaluation workers per route (default: 4)")
    _input_options(eval_cmd)
    _common(eval_cmd)

    sessions = commands.add_parser("sessions", help="Inspect recorded run sessions")
    sessions.add_argument("run_id")
    _common(sessions)

    export = commands.add_parser("export", help="Export a validated packet from a run")
    export.add_argument("run_id")
    export.add_argument("--output")
    export.add_argument(
        "--force", action="store_true",
        help="Allow writing over this run's stored packet (refused by default)",
    )
    export.add_argument("--format", choices=["json", "csv", "jsonl"], default="json", help="Output format: json, csv, or jsonl (one record per line)")
    export.add_argument("--sort-by", help="Claim key to sort records by (e.g. score, priority)")
    export.add_argument("--desc", action="store_true", default=True, help="Sort descending (default: True)")
    export.add_argument("--asc", action="store_false", dest="desc", help="Sort ascending")
    export.add_argument("--top", type=int, help="Limit export to top N records after sorting")
    export.add_argument("--rank", action="store_true", help="Include 1-indexed rank column in CSV export")
    export.add_argument("--filter", dest="filter", help="ClaimFilter JSON (e.g. '{\"all\": [{\"field\": \"score\", \"op\": \">=\", \"value\": 80}]}'; ops: ==, !=, >=, <=, >, <, in, not_in; \"any\" holds OR branches)")

    dag = commands.add_parser("dag", help="Run a declarative DAG workflow (typed nodes, lossless edges)")
    dag.add_argument("--spec", required=True, help="DAG spec JSON file")
    dag.add_argument("--dag-id", help="Workflow ID (default: <name>-<spec digest>)")
    dag.add_argument("--dry-run", action="store_true", help="Validate the spec and print execution order without running")
    dag.add_argument("--no-resume", action="store_true", help="Re-execute completed run nodes instead of resuming them")
    dag.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    _common(dag)
    export.add_argument("--adjust-scores", action="store_true", help="Rank by bias-adjusted scores (raw - per-route bias from eval goldens). Recommended when Phase-A used multiple raters; prefer single-judge Phase-B for final ranking.")
    export.add_argument("--score-field", default="score", help="Numeric claim field bias applies to (default: score)")
    _common(export)

    schema = commands.add_parser("schema", help="Print an admitted JSON Schema")
    schema.add_argument("kind", choices=["task", "input", "candidate-output", "output", "packet", "profile", "database"])

    doctor = commands.add_parser("doctor", help="Check the local CLI, database, auth, and routes")
    doctor.add_argument("--scope", choices=["user", "project"], default="project")
    doctor.add_argument("--workspace-root", default=".")
    doctor.add_argument("--skill-root", help="Advanced: nonstandard parent directory for installed skills")
    _common(doctor)

    serve = commands.add_parser("serve", help="Run the MCP server over stdio")
    serve.add_argument("--workspace-root", default=".")
    serve.add_argument("--db", help="SQLite path below workspace root")

    board = commands.add_parser(
        "board",
        help="Serve the read-only results board for a run (attributes, scores, quotes, provenance)",
    )
    board.add_argument("--run-id", default=None, help="Run to show (default: the most recent)")
    board.add_argument("--port", type=int, default=8100, help="Localhost port (default: 8100)")
    board.add_argument("--host", default="127.0.0.1", help="Host to listen on (default: 127.0.0.1)")
    board.add_argument("--open", action="store_true", help="Open the page in a browser")
    _common(board)
    partners = commands.add_parser(
        "partners",
        help="Run the packaged partner sourcing plan (find candidates, or enrich one partner)",
    )
    partner_actions = partners.add_subparsers(dest="partners_command", required=True)

    def _partner_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--backend", action="append", help="Limit to specific search backends (repeatable)")
        p.add_argument("--max", type=int, default=8, help="Max hits per query per backend (default: 8)")
        p.add_argument("--delay", type=float, default=1.0, help="Politeness delay between fetches (default: 1.0)")
        p.add_argument("--snippets-only", action="store_true", help="Store search snippets without fetching pages")
        p.add_argument("--plan", default=None, help="Override the packaged partner source plan")
        p.add_argument("--output", default=None, help="Output CSV (default: partners.csv)")
        _common(p)

    partners_find = partner_actions.add_parser("find", help="Cold start: search every backend for partner candidates")
    partners_find.add_argument("--tech", default="", help="Technology to anchor queries, e.g. Kafka")
    partners_find.add_argument("--vertical", default="", help="Target vertical, e.g. fintech")
    partners_find.add_argument("--subreddit", action="append", help="Restrict reddit queries to subreddits (repeatable)")
    _partner_common(partners_find)

    partners_enrich = partner_actions.add_parser("enrich", help="Enrich one partner already known by domain")
    partners_enrich.add_argument("domain", help="Partner domain, e.g. trace3.com")
    partners_enrich.add_argument("--max-pages", type=int, default=8, help="Site pages to crawl (default: 8)")
    partners_enrich.add_argument("--no-fetch", action="store_true", help="Skip site/ATS/directory fetches; mentions only")
    _partner_common(partners_enrich)

    studio = commands.add_parser("studio", help="Serve the local harness studio UI (localhost only)")
    studio.add_argument("--workspace-root", default=".")
    studio.add_argument("--db", help="SQLite path below workspace root")
    studio.add_argument("--port", type=int, default=8080, help="Localhost port (default: 8080)")

    mcp = commands.add_parser("mcp", help="MCP client integration")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_install = mcp_sub.add_parser("install", help="Install MCP server entry into Claude/Cursor config (one-command setup)")
    mcp_install.add_argument("--client", choices=["auto", "claude", "cursor", "all"], default="auto", help="Target client config to write (default: auto-detect, falls back to claude)")
    mcp_install.add_argument("--workspace-root", default=".", help="Workspace root for the MCP server (default: .)")
    mcp_install.add_argument("--db", help="SQLite path below workspace root (default: <workspace>/harness-fleet.db)")
    mcp_install.add_argument("--env", action="append", metavar="NAME", help="Copy this shell's environment variable into the client config (can repeat or comma-separate, e.g. --env OPENROUTER_API_KEY). Desktop apps do not inherit the shell environment. Unset names are skipped with a warning; only names are ever printed")
    mcp_install.add_argument("--dry-run", action="store_true", help="Preview without writing")
    mcp_install.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    # Note: --db and --json are also added via _common but we keep explicit for discoverability
    mcp_install.add_argument("--force", action="store_true", help="Overwrite existing harness-fleet entry even if identical (no-op otherwise)")

    discover = commands.add_parser("discover", help="Broad web search to accounts file (mechanical discovery)")
    discover.add_argument("--query", action="append", required=True, help="Search query (repeatable)")
    discover.add_argument("--backend", action="append", choices=sorted(DISCOVER_BACKENDS), help="Search backend (repeatable; default: ddgs + hn)")
    discover.add_argument("--subreddit", action="append", help="Restrict reddit backend to subreddits (repeatable)")
    discover.add_argument("--se-tagged", action="append", help="Restrict stackexchange backend to tags (repeatable)")
    discover.add_argument("--se-site", default="stackoverflow", help="Stack Exchange site (default: stackoverflow)")
    discover.add_argument("--discourse-url", help="Discourse instance to search (required for discourse backend)")
    discover.add_argument("--lemmy-instance", default="https://programming.dev", help="Lemmy instance (default: programming.dev)")
    discover.add_argument("--searxng-url", help="Self-hosted SearXNG base URL (required for searxng backend)")
    discover.add_argument("--max-results", type=int, default=10, help="Max hits per query per backend (default: 10)")
    discover.add_argument("--snippets-only", action="store_true", help="Store search snippets without fetching full pages")
    discover.add_argument("--delay", type=float, default=1.0, help="Politeness delay between fetches in seconds (default: 1.0)")
    discover.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds (default: 20.0)")
    discover.add_argument("--max-chars", type=int, default=None, help="Truncate item text to N chars (default: none)")
    discover.add_argument("--min-chars", type=int, default=None, help="Drop records shorter than N chars before the LLM sees them (default: none)")
    discover.add_argument("--evidence", action="append", help="Admit only this evidence grade (repeatable; e.g. fetched, profile). Omit to admit all grades.")
    discover.add_argument("--require-stack", action="append", help="Stack term that must appear for a matched signal (repeatable; annotates metadata, never filters)")
    discover.add_argument("--exclude-stack", action="append", help="Stack term that sets stack_veto when present (repeatable)")
    discover.add_argument("--min-source-coverage", type=_coverage_value, default=0.70,
                          help="Require at least this fraction of unique hits to become captured items (default: 0.70; use 0 to disable)")
    discover.add_argument("--ignore-robots", action="store_true", help="Ignore robots.txt (default: respect it)")
    discover.add_argument("--js", action="store_true", help="Render JS-heavy pages via Playwright (experimental; needs harness-fleet[js])")
    discover.add_argument("--output", help="Output file (default: accounts.<format>)")
    discover.add_argument("--format", choices=["csv", "jsonl"], default="csv", help="Output format (default: csv)")
    discover.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    fetch = commands.add_parser("fetch", help="Fetch URLs or ATS boards to accounts file")
    fetch.add_argument("--url", action="append", help="URL to fetch and parse (repeatable)")
    fetch.add_argument("--url-file", help="File with one URL per line")
    fetch.add_argument("--sitemap", help="Sitemap URL: fetch every listed page (e.g. https://docs.example.com/sitemap.xml)")
    fetch.add_argument("--site", help="Site origin or URL: use its sitemap, else BFS crawl same-origin pages")
    fetch.add_argument("--max-pages", type=int, default=20, help="Max pages for --site crawl (default: 20)")
    fetch.add_argument("--max-depth", type=int, default=2, help="Link-follow depth for --site crawl (default: 2)")
    fetch.add_argument("--max-comments", type=int, default=50, help="Max comments per HN/Reddit thread (default: 50)")
    fetch.add_argument("--subreddit", action="append", help="Fetch fresh posts from subreddit RSS (repeatable)")
    fetch.add_argument("--subreddit-sort", default="new", choices=["new", "hot", "top", "rising"], help="Subreddit listing (default: new)")
    fetch.add_argument("--reddit-query", help="Fetch full Reddit posts matching a query (Arctic Shift archive)")
    fetch.add_argument("--hn", action="append", help="Fetch full HN thread by item id or URL (repeatable)")
    fetch.add_argument("--stackexchange-query", help="Fetch full Stack Exchange question bodies for a query")
    fetch.add_argument("--se-tag", action="append", help="Restrict Stack Exchange to tags (repeatable)")
    fetch.add_argument("--se-site", default="stackoverflow", help="Stack Exchange site (default: stackoverflow)")
    fetch.add_argument("--se-answers", action="store_true", help="Include top answer per question (costs API quota)")
    fetch.add_argument("--discourse", help="Fetch full topics from a Discourse instance URL")
    fetch.add_argument("--discourse-query", help="Search query within --discourse (omit for latest topics)")
    fetch.add_argument("--lobsters-tag", nargs="?", const="", default=None, help="Fetch Lobsters newest (bare flag) or one tag's listing")
    fetch.add_argument("--lemmy-query", help="Fetch full Lemmy posts/comments for a query")
    fetch.add_argument("--lemmy-instance", default="https://programming.dev", help="Lemmy instance (default: programming.dev)")
    fetch.add_argument("--devto-tag", help="Fetch Dev.to articles for a tag (full markdown bodies)")
    fetch.add_argument("--greenhouse-board", help="Greenhouse board token (e.g. stripe)")
    fetch.add_argument("--ashby-org", help="Ashby org slug (e.g. linear)")
    fetch.add_argument("--lever-org", help="Lever org slug (fallback; many orgs migrated ATS)")
    fetch.add_argument("--title-include", action="append", help="Keep only ATS postings whose title contains this term (repeatable)")
    fetch.add_argument("--title-exclude", action="append", help="Drop ATS postings whose title contains this term (repeatable)")
    fetch.add_argument("--require-stack", action="append", help="Stack term that must appear for a matched signal (repeatable; annotates metadata, never filters)")
    fetch.add_argument("--exclude-stack", action="append", help="Stack term that sets stack_veto when present (repeatable)")
    fetch.add_argument("--min-chars", type=int, default=None, help="Drop records shorter than N chars before the LLM sees them (default: none)")
    fetch.add_argument("--evidence", action="append", help="Admit only this evidence grade (repeatable; e.g. fetched, profile). Omit to admit all grades.")
    fetch.add_argument("--yc", action="store_true", help="Dump YC company directory profiles (indicator-grade)")
    fetch.add_argument("--yc-query", help="Filter YC companies by keyword")
    fetch.add_argument("--yc-batch", help="Filter YC companies by batch (e.g. W24)")
    fetch.add_argument("--yc-tag", action="append", help="Filter YC companies by tag/industry (repeatable)")
    fetch.add_argument("--js", action="store_true", help="Render JS-heavy pages via Playwright (experimental; needs harness-fleet[js])")
    fetch.add_argument("--max-jobs", type=int, default=None, help="Max items per source: postings per ATS board, pages per sitemap (default 200), companies for --yc, posts for feeds (default: source-specific)")
    fetch.add_argument("--delay", type=float, default=1.0, help="Politeness delay between fetches in seconds (default: 1.0)")
    fetch.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds (default: 20.0)")
    fetch.add_argument("--max-chars", type=int, default=None, help="Truncate item text to N chars (default: none)")
    fetch.add_argument("--min-source-coverage", type=_coverage_value, default=0.70,
                       help="Require at least this fraction of attempted source items to be captured (default: 0.70; use 0 to disable)")
    fetch.add_argument("--ignore-robots", action="store_true", help="Ignore robots.txt (default: respect it)")
    fetch.add_argument("--output", help="Output file (default: fetched.<format>)")
    fetch.add_argument("--format", choices=["csv", "jsonl"], default="csv", help="Output format (default: csv)")
    fetch.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "global_json", False):
        args.json = True
    # Handle `db backup` and `mcp install` subcommand wrappers
    if getattr(args, "command", None) == "db":
        sub = getattr(args, "db_command", None)
        if sub == "backup":
            cmd_db_backup(args)
            return
        print(f"Unknown db subcommand: {sub}", file=sys.stderr)
        raise SystemExit(2)
    if getattr(args, "command", None) == "mcp":
        cmd_mcp(args)
        return
    handlers = {
        "setup": cmd_setup,
        "routes": cmd_routes,
        "cooldowns": cmd_cooldowns,
        "settings": cmd_settings,
        "tasks": cmd_tasks,
        "init": cmd_init,
        "profile": cmd_profile,
        "validate": cmd_validate,
        "test": cmd_test,
        "run": cmd_run,
        "resume": cmd_resume,
        "rescore": cmd_rescore,
        "history": cmd_history,
        "calibrate": cmd_calibrate,
        "status": cmd_status,
        "eval": cmd_eval,
        "sessions": cmd_sessions,
        "export": cmd_export,
        "schema": cmd_schema,
        "doctor": cmd_doctor,
        "serve": cmd_serve,
        "studio": cmd_studio,
        "board": cmd_board,
        "partners": cmd_partners,
        "quickstart": cmd_quickstart,
        "discover": cmd_discover,
        "fetch": cmd_fetch,
        "dag": cmd_dag,
    }
    try:
        handlers[args.command](args)
    except SystemExit:
        raise
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            ui.error(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

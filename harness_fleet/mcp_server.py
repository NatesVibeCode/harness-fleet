"""Typed MCP tools with a workspace-confined file boundary."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import __version__
from .catalog import RouteCatalog
from .engine import Engine
from .export import export_clean_packet
from .input_data import iter_input_items
from .models import (
    ID_PATTERN,
    BatchTestResult,
    CandidateModelOutput,
    ClaimFilter,
    CleanPacket,
    CooldownDetail,
    CooldownsReport,
    DoctorCheck,
    DoctorReport,
    EntityHistoryReport,
    InputItem,
    LaneReport,
    ModelOutput,
    ProfileResult,
    RegistryDomain,
    RouteEvalReport,
    RoutePolicy,
    RoutesResult,
    RunStatusReport,
    SchemaResult,
    SortSpec,
    SourceChannel,
    SourceProposal,
    SourcesReport,
    TaskRegistrationResult,
    TaskSpec,
    TasksResult,
    TaskSummary,
    ValidationReport,
)
from .packer import iter_packed_batches
from .profile import IdealCompanyProfile
from .store import SCHEMA_VERSION, HarnessStore
from .task import PRESETS, create_task_from_preset, load_task_spec


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")

    def path(self, value: str, *, exists: bool = False) -> Path:
        candidate = Path(value).expanduser()
        resolved = (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"path escapes workspace root: {value}")
        if exists and not resolved.exists():
            raise FileNotFoundError(f"path not found: {value}")
        return resolved


def _server_name() -> str:
    """The product this install is: the entry point the user actually runs.

    A client lists servers by the name the server reports, so an account-fleet
    install must not introduce itself as harness-fleet, and two fleets installed
    side by side must not both claim one name.
    """
    try:
        from .setup import installed_cli_path

        name = Path(installed_cli_path()).name
        return name or "harness-fleet"
    except Exception:  # a missing entry point must not break the server
        return "harness-fleet"


def create_mcp_server(workspace_root: str | Path, db_path: str | Path | None = None) -> FastMCP:
    workspace = Workspace(workspace_root)
    configured_db = (
        db_path
        or os.environ.get("HARNESS_FLEET_DB")
    )
    resolved_db = workspace.path(str(configured_db)) if configured_db else workspace.path("harness-fleet.db")
    if not resolved_db.is_relative_to(workspace.root):
        raise ValueError("MCP database must stay below the workspace root")
    store = HarnessStore(resolved_db)

    def resolve_task(reference: str) -> TaskSpec:
        try:
            candidate = workspace.path(reference)
            if candidate.is_file():
                task = load_task_spec(candidate)
                store.register_task(task)
                return task
        except Exception:
            pass
        return store.get_task(reference)

    server = FastMCP(
        _server_name(),
        instructions="Execute typed, evidence-bound bulk tasks using free LLM workers inside the configured workspace.",
    )
    # FastMCP takes no version, so without this the SDK advertises its own
    # version in serverInfo and every client reports the wrong product build.
    server._mcp_server.version = __version__

    @server.tool(structured_output=True)
    def harness_fleet_routes(
        refresh: Annotated[bool, Field(description="Refresh provider catalogues")] = False,
        observed_zero_only: Annotated[bool, Field(description="Return only routes with observed zero pricing")] = True,
    ) -> RoutesResult:
        """List admitted routes; optionally append fresh provider price observations."""
        catalog = RouteCatalog(db_path=store.path)
        refresh_result = catalog.refresh_all() if refresh else None
        routes = catalog.get_routes(free_only=observed_zero_only, include_disabled=not observed_zero_only)
        return RoutesResult.model_validate({"routes": routes, "refresh": refresh_result})

    @server.tool(structured_output=True)
    def harness_fleet_cooldowns(
        action: Annotated[str, Field(pattern="^(list|clear)$", description="Action: list active cooldowns or clear cooldowns")] = "list",
        route_id: Annotated[str | None, Field(description="Optional specific route ID to clear")] = None,
    ) -> CooldownsReport:
        """Inspect active rate-limit route cooldowns or clear them."""
        if action == "clear":
            cleared = store.clear_cooldowns(route_id=route_id)
            return CooldownsReport(action="clear", count=cleared, cleared=cleared, route_id=route_id, cooldowns=[])
        cooldowns = store.get_active_cooldown_details()
        return CooldownsReport(
            action="list",
            count=len(cooldowns),
            cooldowns=[CooldownDetail.model_validate(c) for c in cooldowns],
        )

    @server.tool(structured_output=True)
    def harness_fleet_init(
        task_name: Annotated[str, Field(description="Name of the task to register", pattern=ID_PATTERN)],
        preset: Annotated[str, Field(description=f"Task preset template ({', '.join(sorted(PRESETS))})")] = "score",
        instructions: Annotated[str | None, Field(description="Optional custom instructions overriding the preset default")] = None,
        batch_size: Annotated[int, Field(ge=1, le=50, description="Inference batch size")] = 4,
    ) -> TaskRegistrationResult:
        """Initialize and register a typed task from a built-in preset (score, filter, triage, classify, extract, summarize)."""
        spec = create_task_from_preset(task_name, preset_name=preset, instructions=instructions, batch_size=batch_size)
        revision = store.register_task(spec)
        return TaskRegistrationResult(task=spec.name, revision=revision)

    @server.tool(structured_output=True)
    def harness_fleet_register_task(task: TaskSpec) -> TaskRegistrationResult:
        """Validate and register one immutable, declarative task revision."""
        revision = store.register_task(task)
        return TaskRegistrationResult(task=task.name, revision=revision)

    @server.tool(structured_output=True)
    def harness_fleet_lane_report(
        run_id: Annotated[str, Field(description="Run to measure")],
        lane: Annotated[str, Field(description="Lane whose bar to judge against (optional)")] = "",
        sample: Annotated[int, Field(ge=0, le=50, description="Entities to re-check for truth")] = 5,
    ) -> LaneReport:
        """Measure a finished run: yield, coverage, support quality, truth sample, cost.

        Read-only: it reports what the run produced and never recomputes a score,
        so it can be taken before and after a configuration change. Fetching for
        the truth sample uses the workspace's normal polite fetch, and a page
        that cannot be reached is reported as unreachable rather than as a
        failed quote.
        """
        from .lane_report import build_lane_report
        from .lanes import LaneError, load_lane

        resolved = None
        if lane.strip():
            path = workspace.path(f"lanes/{lane.strip()}.json")
            try:
                resolved = load_lane(path)
            except LaneError as exc:
                raise ValueError(str(exc)) from exc
        return build_lane_report(
            store, run_id, workspace_root=workspace.root, lane=resolved, sample=sample
        )

    @server.tool(structured_output=True)
    def harness_fleet_sources() -> SourcesReport:
        """What the source taxonomy knows: promoted domains, waiting candidates, installed channels.

        A domain stays a *candidate* — a lead, not evidence — until it is
        promoted, so this is also the honest answer to "why did that source not
        count?".
        """
        from .channels import load_channels
        from .registry import load, propose

        registry_path = workspace.path("source_registry.json")
        data = load(registry_path)
        return SourcesReport(
            registry=str(registry_path),
            domains=[
                RegistryDomain(domain=domain, category=str(entry.get("category") or ""),
                               reason=str(entry.get("reason") or ""))
                for domain, entry in sorted((data.get("domains") or {}).items())
            ],
            candidate_count=len(data.get("candidates") or {}),
            proposals=[
                SourceProposal(domain=p["domain"], category=p["category"],
                               confidence=float(p["confidence"]), sightings=int(p["sightings"]))
                for p in propose(registry_path)
            ],
            channels=[
                SourceChannel(name=name, category=channel.category, path=channel.path)
                for name, channel in sorted(load_channels(workspace.root).items())
            ],
        )

    @server.tool(structured_output=True)
    def harness_fleet_promote_source(
        domain: Annotated[str, Field(description="Domain to promote, e.g. vendorhub.example")],
        category: Annotated[str, Field(description="Category from the central source taxonomy")],
        reason: Annotated[str, Field(description="Why it belongs there; recorded with the entry")] = "",
    ) -> SourcesReport:
        """Promote a domain into the taxonomy so its pages can carry evidence."""
        from .contracts import SOURCE_CATEGORIES
        from .registry import promote

        if category.strip().upper() not in SOURCE_CATEGORIES:
            known = ", ".join(sorted(name.lower() for name in SOURCE_CATEGORIES))
            raise ValueError(f"unknown category '{category}'; use one of: {known}")
        promote(domain, category.strip().lower(), reason=reason, path=workspace.path("source_registry.json"))
        return harness_fleet_sources()

    @server.tool(structured_output=True)
    def harness_fleet_tasks() -> TasksResult:
        """List the current registered task names and their exact revision IDs."""
        # Validate each row into the model instead of trusting the dict shape:
        # the previous `type: ignore` here is what let a column the model did
        # not know about (scorable) break this tool at runtime.
        tasks = [TaskSummary(**row) for row in store.list_tasks()]
        return TasksResult(tasks=tasks, count=len(tasks))

    @server.tool(structured_output=True)
    def harness_fleet_save_profile(profile: IdealCompanyProfile) -> ProfileResult:
        """Persist and activate one immutable Ideal Company Profile revision."""
        revision = store.save_profile(profile)
        return ProfileResult(
            profile_kind="ideal_company",
            revision=revision,
            profile=profile.model_dump(mode="json"),
        )

    @server.tool(structured_output=True)
    def harness_fleet_get_profile() -> ProfileResult:
        """Return the active Ideal Company Profile stored in SQLite."""
        profile = store.load_profile("ideal_company")
        if profile is None:
            raise ValueError("no active Ideal Company Profile; save one first")
        revision = store.active_profile_revision_id("ideal_company")
        if revision is None:
            raise ValueError("active Ideal Company Profile has no revision")
        return ProfileResult(
            profile_kind="ideal_company",
            revision=revision,
            profile=profile.model_dump(mode="json"),
        )

    @server.tool(structured_output=True)
    def harness_fleet_test(
        task: Annotated[str, Field(description="Registered task name or workspace-relative TaskSpec JSON")],
        input_path: Annotated[str, Field(description="Workspace-relative JSON, JSONL, or CSV input")],
        id_column: Annotated[str | None, Field(description="Optional CSV ID column")] = None,
        text_column: Annotated[str | None, Field(description="Optional CSV text column")] = None,
    ) -> BatchTestResult:
        """Run one real inference batch and return only typed, source-grounded results."""
        task_spec = resolve_task(task)
        input_file = workspace.path(input_path, exists=True)
        input_options = {"id_column": id_column, "text_column": text_column}
        def input_factory():
            return iter_input_items(input_file, **input_options)

        for _ in input_factory():
            pass
        batch = next(iter_packed_batches(
            input_factory(), task_spec.batch_size, task_spec.max_slice_chars
        ), None)
        if batch is None:
            raise ValueError("input contains no packable items")
        ok, results, receipt, error = Engine(task=task_spec, store=store).execute_batch(batch)
        return BatchTestResult.model_validate({
            "ok": ok,
            "results": results,
            "receipt": receipt or None,
            "error": error,
        })

    @server.tool(structured_output=True)
    def harness_fleet_validate(
        task: Annotated[str, Field(description="Registered task name or workspace-relative TaskSpec JSON")],
        input_path: Annotated[str, Field(description="Workspace-relative JSON, JSONL, or CSV input")],
        id_column: Annotated[str | None, Field(description="Optional CSV ID column")] = None,
        text_column: Annotated[str | None, Field(description="Optional CSV text column")] = None,
    ) -> ValidationReport:
        """Validate a task and all input records offline without invoking a model."""
        task_spec = resolve_task(task)
        total_items = 0
        batch_count = 0
        truncated = 0
        for batch in iter_packed_batches(
            iter_input_items(workspace.path(input_path, exists=True), id_column=id_column, text_column=text_column),
            task_spec.batch_size,
            task_spec.max_slice_chars,
        ):
            batch_count += 1
            for item in batch.get("items", []) if isinstance(batch, dict) else []:
                total_items += 1
                slices = item.get("slices", []) if isinstance(item, dict) else []
                if any(s.get("partial") for s in slices):
                    truncated += 1
        errors: list[str] = []
        if truncated:
            errors.append(
                f"{truncated}/{total_items} item(s) exceed max_slice_chars={task_spec.max_slice_chars} "
                "and were sliding-window sliced with partial:true; quotes must lie within one "
                "window, so raise max_slice_chars for fewer windows"
            )
        return ValidationReport(
            valid=True, task=task_spec.name, input_items=total_items, batches=batch_count, errors=errors,
        )

    @server.tool(structured_output=True)
    def harness_fleet_run(
        task: Annotated[str, Field(description="Registered task name or workspace-relative TaskSpec JSON")],
        input_path: Annotated[str, Field(description="Workspace-relative JSON, JSONL, or CSV input")],
        run_id: Annotated[str, Field(description="Stable run identifier", pattern=ID_PATTERN, max_length=128)],
        sessions: Annotated[int, Field(ge=1, le=64)] = 4,
        max_attempts: Annotated[int, Field(ge=1, le=100_000)] = 300,
        output_packet: Annotated[str | None, Field(description="Optional workspace-relative packet path")] = None,
        id_column: Annotated[str | None, Field(description="Optional CSV ID column")] = None,
        text_column: Annotated[str | None, Field(description="Optional CSV text column")] = None,
        only_ids: Annotated[str | None, Field(description="Optional workspace-relative file or comma-separated list of IDs to restrict input items to")] = None,
        policy: Annotated[RoutePolicy | None, Field(description="Optional RoutePolicy with privacy, transport, or cost bounds")] = None,
        profile_path: Annotated[str | None, Field(description="Optional workspace-relative Ideal Company Profile JSON to persist and attach to this run")] = None,
        use_active_profile: Annotated[bool, Field(description="Explicitly attach the active Ideal Company Profile from SQLite")] = False,
        parent_run_id: Annotated[str | None, Field(description="Optional parent run for rescore lineage")] = None,
    ) -> CleanPacket:
        """Create and execute a bounded, resumable SQLite-backed bulk campaign."""
        task_spec = resolve_task(task)
        input_file = workspace.path(input_path, exists=True)
        resolved_only_ids: Path | str | None = None
        if only_ids:
            try:
                candidate = workspace.path(only_ids)
                if candidate.is_file():
                    resolved_only_ids = candidate
                else:
                    resolved_only_ids = only_ids
            except Exception:
                resolved_only_ids = only_ids
        input_options = {
            "id_column": id_column,
            "text_column": text_column,
            "only_ids": resolved_only_ids,
        }
        def input_factory():
            return iter_input_items(input_file, **input_options)

        if profile_path:
            profile: IdealCompanyProfile | None = IdealCompanyProfile.load(workspace.path(profile_path, exists=True))
            profile_revision_id: str | None = store.save_profile(profile)
        elif use_active_profile:
            profile_revision_id = store.active_profile_revision_id("ideal_company")
            profile = store.load_profile("ideal_company") if profile_revision_id else None
        else:
            profile_revision_id = None
            profile = None
        packet_path = workspace.path(output_packet) if output_packet else workspace.path(f"runs/{run_id}/clean_packet.json")
        packet = Engine(task=task_spec, store=store, policy=policy).run_campaign(
            raw_items=input_factory(),
            run_id=run_id,
            input_path=str(input_file),
            concurrency=sessions,
            max_attempts=max_attempts,
            output_packet_path=packet_path,
            policy=policy,
            profile_revision_id=profile_revision_id,
            profile=profile,
            raw_items_factory=input_factory,
            parent_run_id=parent_run_id,
        )
        return CleanPacket.model_validate(packet)

    @server.tool(structured_output=True)
    def harness_fleet_resume(
        run_id: Annotated[str, Field(description="Existing SQLite run identifier")],
        sessions: Annotated[int, Field(ge=1, le=64)] = 4,
        output_packet: Annotated[str | None, Field(description="Optional workspace-relative packet path")] = None,
        policy: Annotated[RoutePolicy | None, Field(description="Optional explicit paid-route approval and policy for this resume session")] = None,
    ) -> CleanPacket:
        """Resume pending batches from an existing run without rereading source files. Paid routes must be explicitly approved again in each new session; free routes remain the default."""
        task_spec = store.get_run_task(run_id)
        snapshot = store.run_snapshot(run_id)
        raw_out = snapshot.get("output_path")
        packet_path = workspace.path(output_packet) if output_packet else (workspace.path(raw_out) if raw_out else workspace.path(f"runs/{run_id}/clean_packet.json"))
        packet = Engine(task=task_spec, store=store, policy=policy).resume_campaign(run_id, sessions, packet_path)
        return CleanPacket.model_validate(packet)

    @server.tool(structured_output=True)
    def harness_fleet_status(
        run_id: Annotated[str, Field(description="Existing SQLite run identifier")],
    ) -> RunStatusReport:
        """Show real-time progress, batch status counts, and per-route reliability metrics for a run."""
        return store.get_run_status(run_id)

    @server.tool(structured_output=True)
    def harness_fleet_history(
        entity: Annotated[str, Field(description="Entity key (InputItem metadata.entity, else the item_id)")],
    ) -> EntityHistoryReport:
        """Show an entity's score trajectory across rescore rounds, oldest first."""
        from .models import ScoreHistoryRound

        return EntityHistoryReport(
            entity=entity,
            rounds=[ScoreHistoryRound.model_validate(row) for row in store.get_entity_history(entity)],
        )

    @server.tool(structured_output=True)
    def harness_fleet_eval(
        task: Annotated[str, Field(description="Registered task name or workspace-relative TaskSpec JSON")],
        input_path: Annotated[str, Field(description="Evaluation dataset (CSV, JSONL, or JSON)")],
        routes: Annotated[list[str] | None, Field(description="Optional list of route IDs to benchmark")] = None,
        id_column: Annotated[str | None, Field(description="Optional CSV ID column")] = None,
        text_column: Annotated[str | None, Field(description="Optional CSV text column")] = None,
    ) -> RouteEvalReport:
        """Benchmark candidate routes against test samples and update intelligent ranking priors."""
        from .eval import RouteEvaluator
        task_spec = resolve_task(task)
        input_file = workspace.path(input_path, exists=True)
        input_options = {"id_column": id_column, "text_column": text_column}
        def input_factory():
            return iter_input_items(input_file, **input_options)

        evaluator = RouteEvaluator(task=task_spec, store=store)
        return evaluator.evaluate_all(
            samples=input_factory(), routes=routes, samples_factory=input_factory
        )

    @server.tool(structured_output=True)
    def harness_fleet_export(
        run_id: Annotated[str, Field(description="Existing SQLite run identifier")],
        output_path: Annotated[str | None, Field(description="Optional workspace-relative export path")] = None,
        export_format: Annotated[str, Field(description="Export format: json, csv, or jsonl")] = "json",
        sort: Annotated[dict | None, Field(description="SortSpec JSON (e.g. {\"field\": \"score\", \"descending\": true})")] = None,
        top_n: Annotated[int | None, Field(description="Limit output to top N records")] = None,
        rank: Annotated[bool, Field(description="Prepend a 1-indexed rank column in CSV export")] = False,
        claim_filter: Annotated[dict | None, Field(description="ClaimFilter JSON (e.g. {\"all\": [{\"field\": \"score\", \"op\": \">=\", \"value\": 80}]}); \"any\" holds OR branches")] = None,
        adjust_scores: Annotated[bool, Field(description="Rank by bias-adjusted scores when multiple raters produced the run")] = False,
        score_field: Annotated[str, Field(description="Numeric claim field bias applies to")] = "score",
    ) -> CleanPacket:
        """Export verified records as a self-validating typed packet (JSON) or flat CSV. For comparable ranking, pin Phase-B to one judge route; use adjust_scores only when mixed raters are unavoidable."""
        snapshot = store.run_snapshot(run_id)
        default_ext = "csv" if export_format == "csv" else ("jsonl" if export_format == "jsonl" else "json")
        destination = workspace.path(output_path) if output_path else workspace.path(f"runs/{run_id}/clean_packet.{default_ext}")
        bias_map: dict[str, float] | None = None
        if adjust_scores:
            try:
                task_name = (snapshot.get("task") or {}).get("name")
                raw_bias = store.get_route_claim_bias(task_name) if task_name else {}
                bias_map = {v["route_id"]: v["bias"] for v in raw_bias.values()}
            except Exception:
                bias_map = None
        packet = export_clean_packet(
            snapshot,
            destination,
            export_format=export_format,
            sort=SortSpec.model_validate(sort) if sort else None,
            top=top_n,
            rank=rank,
            claim_filter=ClaimFilter.model_validate(claim_filter) if claim_filter else None,
            bias_map=bias_map,
            score_field=score_field,
        )
        return CleanPacket.model_validate(packet)

    @server.tool(structured_output=True)
    def harness_fleet_schema(
        kind: Annotated[str, Field(pattern="^(task|input|candidate-output|output|packet|profile|database)$")],
    ) -> SchemaResult:
        """Return an admitted JSON Schema or the authoritative SQLite schema."""
        models: dict[str, Any] = {
            "task": TaskSpec,
            "input": InputItem,
            "candidate-output": CandidateModelOutput,
            "output": ModelOutput,
            "packet": CleanPacket,
            "profile": IdealCompanyProfile,
        }
        from .store import get_database_schema_sql
        document = (
            {"schema_version": SCHEMA_VERSION, "sql": get_database_schema_sql()}
            if kind == "database"
            else models[kind].model_json_schema(by_alias=True)
        )
        return SchemaResult(kind=kind, schema_document=document)  # type: ignore[arg-type]

    @server.tool(structured_output=True)
    def harness_fleet_doctor() -> DoctorReport:
        """Check workspace SQLite state, provider availability, and usable routes."""
        from .providers.registry import HARNESS_SPECS, configured_routes

        catalog = RouteCatalog(db_path=store.path)
        routes = catalog.get_routes(free_only=True)
        openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))
        schema_version = store.schema_version()
        # migrate() always writes SCHEMA_VERSION, so a mismatch means the
        # migration did not run or commit. Comparing against that constant keeps
        # this check honest across schema bumps instead of blessing a stale list.
        database_ok = schema_version == SCHEMA_VERSION
        checks = [
            DoctorCheck(
                name="database",
                ok=database_ok,
                detail=f"SQLite schema {schema_version}"
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
            DoctorCheck(name="openrouter", ok=openrouter, detail="configured" if openrouter else "optional key not configured"),
            DoctorCheck(name="routes", ok=bool(routes), detail=f"{len(routes)} enabled observed-zero routes"),
        ]
        usable = configured_routes(routes)
        checks.append(DoctorCheck(name="configured_routes", ok=bool(usable), detail=f"{len(usable)} routes have their own transport configured; live authentication is not tested"))
        ready = checks[0].ok and bool(usable)
        return DoctorReport(ready=ready, database=str(store.path), checks=checks)

    return server


def run_mcp_server(workspace_root: str | Path = ".", db_path: str | Path | None = None) -> None:
    create_mcp_server(workspace_root, db_path).run(transport="stdio")

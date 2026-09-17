"""Declarative DAG workflows over the existing executor.

Every workflow is a directed acyclic graph of typed nodes. Edges carry ID
sets and run references in-process — never lossy CSV round-trips — so
multi-stage funnels keep full drill-through (offsets, digests) at every hop.
Nodes reuse the current primitives (Engine campaigns, export filters), and
each ``run``/``rescore`` node is one SQLite run, so per-node resume works
unchanged. Multi-step flows (rescore rounds, calibration fitting) are node
kinds here, not imperative scripts: the CLI builds specs and runs them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .calibrate import PARAM_KINDS, collect_observations, fit_calibration
from .catalog import RouteCatalog
from .engine import Engine
from .export import (
    _filter_and_sort_records,
    export_clean_packet,
    verified_records_from_snapshot,
)
from .gates import DEFAULT_LADDER, FETCHED, SNIPPET, LadderRung, _evaluable_gates
from .input_data import load_input_items
from .models import CalibrationReport, ClaimFilter, ClosedModel, RoutePolicy, SortSpec
from .profile import IdealCompanyProfile
from .providers.registry import ProviderRegistry, ProviderResolutionError
from .rungs import (
    advances_to,
    candidate_name,
    count_rows,
    gate_rows,
    next_rung,
    read_table,
    record_text,
    resolved_gate_profile,
    surviving_ids,
    table_path,
    write_table,
)
from .store import HarnessStore
from .task import load_task_spec


class DagError(ValueError):
    pass


class RunNode(ClosedModel):
    kind: Literal["run"] = "run"
    id: str
    task: str | None = None
    task_from: str | None = None
    input: str
    ids_from: list[str] = []
    ids_mode: Literal["union", "intersection"] = "intersection"
    sessions: int = 4
    max_attempts: int = 300
    policy: RoutePolicy | None = None
    id_column: str | None = None
    text_column: str | None = None
    title_column: str | None = None
    uri_column: str | None = None

    @model_validator(mode="after")
    def check_task_source(self) -> RunNode:
        if (self.task is None) == (self.task_from is None):
            raise DagError("run node needs exactly one of 'task' or 'task_from'")
        return self


class RescoreNode(ClosedModel):
    """A rescore round: fresh evidence as one SQLite run linked to a parent run.

    The parent is either an upstream node (``from_run``) or a literal
    existing run id (``parent_run``, the CLI spelling) — exactly one.
    Without ``task`` the parent run's task is reused.
    """
    kind: Literal["rescore"] = "rescore"
    id: str
    from_run: str | None = None
    parent_run: str | None = None
    task: str | None = None
    input: str
    run_id: str | None = None
    output: str | None = None
    sessions: int = 4
    max_attempts: int = 300
    policy: RoutePolicy | None = None
    id_column: str | None = None
    text_column: str | None = None
    title_column: str | None = None
    uri_column: str | None = None
    profile: str | None = None
    use_active_profile: bool = False

    @model_validator(mode="after")
    def check_parent_source(self) -> RescoreNode:
        if (self.from_run is None) == (self.parent_run is None):
            raise DagError("rescore node needs exactly one of 'from_run' or 'parent_run'")
        return self


class CalibrateNode(ClosedModel):
    """Fit scoring calibration against labeled samples with one rater route.

    On ``apply`` the recalibrated task revision is registered and recorded,
    so a downstream run node can consume it via ``task_from``.
    """
    kind: Literal["calibrate"] = "calibrate"
    id: str
    task: str
    input: str
    expected: str = "score"
    route: str
    params: list[str] = Field(default_factory=lambda: list(PARAM_KINDS))
    max_sweeps: int = Field(default=50, ge=1)
    apply: bool = False
    id_column: str | None = None
    text_column: str | None = None
    title_column: str | None = None
    uri_column: str | None = None

    @model_validator(mode="after")
    def check_params(self) -> CalibrateNode:
        unknown = [name for name in self.params if name not in PARAM_KINDS]
        if unknown:
            raise DagError(f"calibrate node params {unknown} are not in {list(PARAM_KINDS)}")
        if not self.params:
            raise DagError("calibrate node needs at least one param to fit")
        return self


class FilterNode(ClosedModel):
    kind: Literal["filter"] = "filter"
    id: str
    from_run: str
    filter: ClaimFilter | None = None
    sort: SortSpec | None = None
    top: int | None = None


class ExportNode(ClosedModel):
    kind: Literal["export"] = "export"
    id: str
    from_run: str
    format: Literal["json", "csv", "jsonl"] = "json"
    output: str | None = None
    sort: SortSpec | None = None
    top: int | None = None
    rank: bool = False
    filter: ClaimFilter | None = None


class ReviewNode(ClosedModel):
    """A bounded uncertainty loop: gather only what the evidence is missing.

    Reads a run, asks the central contract what each record's evidence lacks
    for its tier, searches for exactly those kinds, rescores the new sources as
    a child run (so the lineage in ``history`` shows the trajectory), and
    repeats until every record clears the bar, a round adds nothing new, or
    ``max_rounds`` is reached. A DAG spec cannot cycle, so the loop lives here
    and the acyclicity of the graph is preserved.
    """
    kind: Literal["review"] = "review"
    id: str
    from_run: str
    task: str | None = None
    tier: str = "tier_1"
    require_kinds: list[str] = []
    max_rounds: int = 2
    max_entities: int = 5
    max_results: int = 6
    backends: list[str] = []
    sessions: int = 4
    max_attempts: int = 300
    policy: RoutePolicy | None = None
    workspace_root: str | None = None


class GateNode(ClosedModel):
    """One rung of a lane's ladder, put to the population the rung above left.

    A gate node reads exactly one source, and the source is what its verdict is
    entitled to stand on: the discovery run's records (the rung that reads
    search results), a retrieve node's fetched prose (the rung that spends a
    page visit), or the gate above it (a rung that adds gates but no evidence).
    Either way it writes the table this rung produced — every candidate it was
    given, the gates' verdicts, and who it passes on — so the population is
    inspectable at each step rather than only at the end.
    """

    kind: Literal["gate"] = "gate"
    id: str
    lane: str
    rung: str
    from_run: str | None = None
    #: The captured items a research run is holding when the first rung runs —
    #: a path, because a funnel that starts from a file can be re-run, read and
    #: argued with, and the alternative is a stage that only exists in memory.
    from_items: str | None = None
    from_gate: str | None = None
    from_retrieve: str | None = None
    #: An explicit grade for what this gate is reading. Empty infers it: text
    #: that came through a retrieve node was read, so it gates as ``fetched``;
    #: a run's own records were given, so they gate as results.
    evidence: Literal["", "snippet", "fetched"] = ""
    profile: str | None = None

    @model_validator(mode="after")
    def check_source(self) -> GateNode:
        sources = [self.from_run, self.from_items, self.from_gate, self.from_retrieve]
        if len([source for source in sources if source]) != 1:
            raise DagError(
                f"gate node '{self.id}' needs exactly one of 'from_run', 'from_items', "
                "'from_gate' or 'from_retrieve'"
            )
        return self


class RetrieveNode(ClosedModel):
    """Spend the page visits one rung declared, and write down what came back.

    The walk is bounded by the rung's own surfaces rather than by a list
    assembled at the call site, so a rung that names case studies is the reason
    case studies get read and a rung that names none reads none. The prose it
    fetched is kept next to the table, one file per candidate, because that text
    is what the next rung's gate is judged on — and a verdict has to be
    re-readable after the run.
    """

    kind: Literal["retrieve"] = "retrieve"
    id: str
    lane: str
    rung: str
    from_gate: str | None = None
    from_items: str | None = None
    max_pages: int = 8
    per_surface: int = 3
    timeout: float = 20.0
    delay: float = 1.0
    respect_robots: bool = True
    vendor_stories: bool = True

    @model_validator(mode="after")
    def check_source(self) -> RetrieveNode:
        if (self.from_gate is None) == (self.from_items is None):
            raise DagError(
                f"retrieve node '{self.id}' needs exactly one of 'from_gate' or 'from_items'"
            )
        return self


DagNode = (
    RunNode | RescoreNode | CalibrateNode | FilterNode | ExportNode | ReviewNode
    | GateNode | RetrieveNode
)


class DagSpec(ClosedModel):
    name: str
    nodes: list[DagNode]

    @model_validator(mode="after")
    def check_graph(self) -> DagSpec:
        # Fail fast at parse time: duplicate ids, unknown or wrong-kind
        # references, self-edges, and cycles never reach the executor.
        self.topo_order()
        return self

    def topo_order(self) -> list[str]:
        """Topological node ids (Kahn's algorithm, spec order breaks ties).

        Raises DagError on duplicate ids, unknown references, wrong-kind
        references, self-edges, and cycles.
        """
        by_id: dict[str, DagNode] = {}
        for node in self.nodes:
            if node.id in by_id:
                raise DagError(f"duplicate node id '{node.id}'")
            by_id[node.id] = node

        deps: dict[str, set[str]] = {}
        for node in self.nodes:
            if isinstance(node, ReviewNode):
                deps[node.id] = set()
                continue
            if isinstance(node, RunNode):
                refs = list(node.ids_from)
                for ref in refs:
                    target = by_id.get(ref)
                    if target is None:
                        raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                    if not isinstance(target, FilterNode):
                        raise DagError(f"node '{node.id}' ids_from '{ref}' is not a filter node")
                    if ref == node.id:
                        raise DagError(f"node '{node.id}' references itself")
                task_refs = [node.task_from] if node.task_from else []
                for ref in task_refs:
                    target = by_id.get(ref)
                    if target is None:
                        raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                    if not isinstance(target, CalibrateNode):
                        raise DagError(f"node '{node.id}' task_from '{ref}' is not a calibrate node")
                    if ref == node.id:
                        raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = set(refs) | set(task_refs)
            elif isinstance(node, RescoreNode):
                if node.from_run is None:
                    deps[node.id] = set()
                    continue
                ref = node.from_run
                target = by_id.get(ref)
                if target is None:
                    raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                if not isinstance(target, (RunNode, RescoreNode)):
                    raise DagError(f"node '{node.id}' from_run '{ref}' is not a run or rescore node")
                if ref == node.id:
                    raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {ref}
            elif isinstance(node, CalibrateNode):
                deps[node.id] = set()
            elif isinstance(node, GateNode):
                refs: list[tuple[str, tuple[type, ...]]] = []
                if node.from_items:
                    # Captured items are a file the run wrote before the first
                    # rung: no node produces them, so nothing to depend on.
                    items_path = Path(node.from_items)
                    if not items_path.is_file():
                        raise DagError(
                            f"node '{node.id}' reads '{node.from_items}', which is not a file"
                        )
                if node.from_run:
                    # A gate reads either a run node in this graph or an
                    # existing run by id, the way a review node does: a ladder
                    # is worth running over a discovery run that already
                    # happened, and demanding a `run` node for it would make
                    # every ladder graph begin with work it does not need.
                    if by_id.get(node.from_run) is not None:
                        refs.append((node.from_run, (RunNode, RescoreNode)))
                if node.from_gate:
                    refs.append((node.from_gate, (GateNode,)))
                if node.from_retrieve:
                    refs.append((node.from_retrieve, (RetrieveNode,)))
                for ref, allowed in refs:
                    target = by_id.get(ref)
                    if target is None:
                        raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                    if not isinstance(target, allowed):
                        wanted = " or ".join(cls.__name__ for cls in allowed)
                        raise DagError(f"node '{node.id}' reads '{ref}', which is not a {wanted}")
                    if ref == node.id:
                        raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {ref for ref, _ in refs}
            elif isinstance(node, RetrieveNode):
                ref = node.from_gate
                if ref is None:
                    deps[node.id] = set()
                    continue
                target = by_id.get(ref)
                if target is None:
                    raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                if not isinstance(target, GateNode):
                    raise DagError(f"node '{node.id}' from_gate '{ref}' is not a gate node")
                if ref == node.id:
                    raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {ref}
            else:
                ref = node.from_run
                target = by_id.get(ref)
                if target is None:
                    raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                if not isinstance(target, (RunNode, RescoreNode)):
                    raise DagError(f"node '{node.id}' from_run '{ref}' is not a run or rescore node")
                if ref == node.id:
                    raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {ref}

        order: list[str] = []
        resolved: set[str] = set()
        remaining = list(by_id)
        while remaining:
            ready = [nid for nid in remaining if deps[nid] <= resolved]
            if not ready:
                raise DagError(f"dependency cycle among nodes: {sorted(remaining)}")
            for nid in ready:
                order.append(nid)
                resolved.add(nid)
                remaining.remove(nid)
        return order


def spec_digest(spec: DagSpec) -> str:
    payload = spec.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _resolve_dag_task(reference: str, store: HarnessStore, workspace_root: Path):
    path = (workspace_root / reference).expanduser()
    if path.is_file():
        spec = load_task_spec(path)
        store.register_task(spec)
        return spec
    return store.get_task(reference)


def _resolve_node_profile(node: RescoreNode, store: HarnessStore, root: Path):
    """Profile selection for rescore nodes, mirroring the run command."""
    if node.profile:
        path = Path(node.profile).expanduser()
        profile = IdealCompanyProfile.load(path if path.is_absolute() else root / path)
        return profile, store.save_profile(profile)
    if node.use_active_profile:
        revision = store.active_profile_revision_id("ideal_company")
        if revision:
            return store.load_profile("ideal_company"), revision
    return None, None


def _resolve_run_ref(state: dict[str, Any], dag_id: str, node_id: str) -> str:
    """Actual run id for a referenced node.

    Downstream nodes name their source node, not its run id, and a `run` or
    `rescore` node may override the deterministic ``<dag-id>-<node-id>``
    default. Read the resolved id back from recorded state; fall back to the
    deterministic name for legacy states written before it was recorded.
    """
    node_state = (state.get("nodes") or {}).get(node_id) or {}
    return str(node_state.get("run_id") or node_state.get("from_run") or f"{dag_id}-{node_id}")


def _execute_rescore_node(
    node: RescoreNode,
    run_id: str,
    state: dict[str, Any],
    store: HarnessStore,
    root: Path,
    dag_id: str,
) -> None:
    parent = _resolve_run_ref(state, dag_id, node.from_run) if node.from_run else node.parent_run
    assert parent is not None
    try:
        store.run_snapshot(parent)
    except KeyError as exc:
        raise DagError(f"rescore node '{node.id}' parent run '{parent}' does not exist") from exc
    task = _resolve_dag_task(node.task, store, root) if node.task else store.get_run_task(parent)
    profile, profile_revision_id = _resolve_node_profile(node, store, root)
    input_path = (root / node.input).expanduser()
    items = load_input_items(
        input_path,
        id_column=node.id_column,
        text_column=node.text_column,
        title_column=node.title_column,
        uri_column=node.uri_column,
    )
    policy = node.policy
    output_path = (root / node.output).expanduser() if node.output else root / "runs" / run_id / "clean_packet.json"
    packet = Engine(task=task, store=store, policy=policy).run_campaign(
        raw_items=items,
        run_id=run_id,
        input_path=str(input_path),
        concurrency=node.sessions,
        max_attempts=node.max_attempts,
        output_packet_path=output_path,
        policy=policy,
        profile_revision_id=profile_revision_id,
        profile=profile,
        parent_run_id=parent,
    )
    state["nodes"][node.id] = {
        "kind": "rescore",
        "run_id": run_id,
        "parent_run": parent,
        "verified": packet.get("total_verified_records", 0),
        "input_digest": packet.get("input_digest"),
        "output": str(output_path),
    }


def _execute_calibrate_node(
    node: CalibrateNode,
    state: dict[str, Any],
    store: HarnessStore,
    root: Path,
    dag_dir: Path,
) -> None:
    from .models import TaskSpec

    task = _resolve_dag_task(node.task, store, root)
    if task.checklist is None:
        raise DagError(f"calibrate node '{node.id}' task '{task.name}' has no checklist to fit")
    catalog = RouteCatalog(db_path=store.path)
    routes_by_id = {item["id"]: item for item in catalog.data.get("routes", [])}
    if node.route not in routes_by_id:
        raise DagError(f"calibrate node '{node.id}' unknown route '{node.route}'")
    provider_hint = routes_by_id[node.route].get("provider")
    try:
        provider = ProviderRegistry().resolve(provider_hint)
    except ProviderResolutionError as exc:
        raise DagError(f"calibrate node '{node.id}' {exc}") from exc
    input_path = (root / node.input).expanduser()
    samples = load_input_items(
        input_path,
        id_column=node.id_column,
        text_column=node.text_column,
        title_column=node.title_column,
        uri_column=node.uri_column,
    )
    if not samples:
        raise DagError(f"calibrate node '{node.id}' input '{node.input}' contains no samples")
    observations, skipped, scored_at = collect_observations(
        task, samples, node.expected, provider.run_prompt, node.route
    )
    fit = fit_calibration(
        task, observations, scored_at,
        kinds=tuple(node.params), max_sweeps=node.max_sweeps,
    )
    new_revision = None
    if node.apply:
        recalibrated = TaskSpec.model_validate(task.model_copy(update={
            "checklist": fit["points"],
            "source_weights": fit["weights"],
            "default_source_weight": fit["default_source_weight"],
            "recency_half_lives": fit["halves"],
        }).model_dump(mode="json"))
        new_revision = store.register_task(recalibrated)
    report = CalibrationReport(
        task=task.name, route=node.route, scored_at=scored_at, params=sorted(node.params),
        sweeps=fit["sweeps"], n_train=fit["n_train"], n_holdout=fit["n_holdout"],
        n_skipped=sum(skipped.values()), skipped=skipped,
        baseline=fit["baseline"], fitted_train=fit["fitted_train"],
        fitted_holdout=fit["fitted_holdout"], points=fit["points"],
        points_float=fit["points_float"], weights=fit["weights"],
        default_source_weight=fit["default_source_weight"], halves=fit["halves"],
        applied=node.apply, new_revision=new_revision,
    ).model_dump(mode="json")
    node_dir = dag_dir / node.id
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    state["nodes"][node.id] = {
        "kind": "calibrate",
        "task": task.name,
        "report": report,
        "revision": new_revision,
    }


def _execute_review_node(
    node: ReviewNode,
    store: HarnessStore,
    root: Path,
    state: dict[str, Any],
    dag_id: str,
) -> dict[str, Any]:
    """Run the uncertainty loop for one node, recording every round."""
    from . import contracts
    from .bundler import bundle_records
    from .discover import run_discovery
    from .engine import Engine
    from .evidence import coverage
    from .export import verified_records_from_snapshot
    from .input_data import iter_input_items
    from .sources import classify_source_category, entity_key_for

    def bar_kinds() -> tuple[str, ...]:
        return tuple(node.require_kinds) or contracts.TIER_MINIMUMS.get(node.tier, ())

    run_id = _resolve_run_ref(state, dag_id, node.from_run)
    text_by_id: dict[str, str] = {}
    rounds: list[dict[str, Any]] = []
    for round_index in range(max(1, int(node.max_rounds))):
        snapshot = store.run_snapshot(run_id)
        records, task = verified_records_from_snapshot(snapshot)
        # The evidence read needs the text the parent run scored, which lives in
        # its input file; without it every claim looks unsupported.
        if not text_by_id:
            input_path = snapshot.get("input_path") or (snapshot.get("run") or {}).get("input_path")
            if input_path and Path(str(input_path)).is_file():
                try:
                    for item in iter_input_items(str(input_path)):
                        text_by_id[str(item.item_id)] = item.text or ""
                except Exception:
                    pass
        if not records:
            break
        # Which entities are short of the bar, and of what exactly?
        gaps: dict[str, list[str]] = {}
        for record in records:
            item_id = str(record.item_id)
            kinds = coverage(text_by_id.get(item_id, ""), record.source_uri or "")
            missing = [kind for kind in bar_kinds() if not kinds.get(kind)]
            if missing:
                gaps[item_id] = missing
        if not gaps:
            rounds.append({"round": round_index, "run_id": run_id, "gaps": {}, "added": 0, "status": "satisfied"})
            break
        selected = dict(list(gaps.items())[: max(1, int(node.max_entities))])
        queries: list[str] = []
        for entity, missing in selected.items():
            queries.extend(contracts.queries_for_gaps(entity, missing))
        if not queries:
            rounds.append({"round": round_index, "run_id": run_id, "gaps": selected, "added": 0, "status": "no_query"})
            break
        items, _report = run_discovery(
            queries=queries,
            backends=node.backends or ["ddgs", "hn"],
            max_results=int(node.max_results),
            delay=0.5,
            min_source_coverage=0.0,
        )
        fresh = [
            item for item in items
            if str(item.item_id) in selected
            or entity_key_for(item.source_uri or "", item.text or "", item.metadata or {}) in selected
        ]
        if not fresh:
            rounds.append({"round": round_index, "run_id": run_id, "gaps": selected, "added": 0, "status": "nothing_new"})
            break
        keyed = [
            item.model_copy(update={
                "item_id": entity_key_for(item.source_uri or "", item.text or "", item.metadata or {}) or item.item_id
            })
            for item in fresh
        ]
        dossiers = bundle_records(keyed)
        round_dir = root / "runs" / dag_id / f"review-{node.id}-r{round_index + 1}"
        round_dir.mkdir(parents=True, exist_ok=True)
        evidence_csv = round_dir / "evidence.csv"
        import csv as _csv

        with evidence_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = _csv.writer(handle)
            writer.writerow(["item_id", "text", "source_uri"])
            for dossier in dossiers:
                writer.writerow([dossier.item_id, dossier.text, dossier.source_uri or ""])
                text_by_id[str(dossier.item_id)] = dossier.text or ""
        next_run = f"{dag_id}-{node.id}-r{round_index + 1}"
        Engine(task=task, store=store, policy=node.policy).run_campaign(
            raw_items=iter_input_items(evidence_csv, id_column="item_id", text_column="text", uri_column="source_uri"),
            run_id=next_run,
            input_path=str(evidence_csv),
            concurrency=int(node.sessions),
            max_attempts=int(node.max_attempts),
            output_packet_path=round_dir / "clean_packet.json",
            policy=node.policy,
            parent_run_id=run_id,
        )
        rounds.append({
            "round": round_index,
            "run_id": next_run,
            "parent_run": run_id,
            "gaps": selected,
            "queries": queries,
            "added": len(dossiers),
            "status": "rescored",
        })
        run_id = next_run

    final_snapshot = store.run_snapshot(run_id)
    final_records, _task = verified_records_from_snapshot(final_snapshot)
    remaining: dict[str, list[str]] = {}
    for record in final_records:
        item_id = str(record.item_id)
        kinds = coverage(text_by_id.get(item_id, ""), record.source_uri or "")
        missing = [kind for kind in bar_kinds() if not kinds.get(kind)]
        if missing:
            remaining[item_id] = missing
    return {
        "run_id": run_id,
        "rounds": rounds,
        "rounds_used": len([entry for entry in rounds if entry.get("status") == "rescored"]),
        "unsatisfied": remaining,
        "satisfied": not remaining,
        "categories": sorted({
            classify_source_category(record.source_uri or "") for record in final_records
        }),
    }


def _combine_ids(sets: list[set[str]], mode: str) -> set[str] | None:
    if not sets:
        return None
    if mode == "union":
        combined: set[str] = set()
        for s in sets:
            combined |= s
        return combined
    combined = set(sets[0])
    for s in sets[1:]:
        combined &= s
    return combined


def _run_complete(store: HarnessStore, run_id: str) -> bool:
    try:
        snapshot = store.run_snapshot(run_id)
    except Exception:
        return False
    batches = (snapshot.get("batches") or {}).values()
    return bool(list(batches)) and all(
        b.get("status") in ("verified", "failed") for b in batches
    )


def _ensure_routes(store: HarnessStore, policy: RoutePolicy | None) -> None:
    """A DAG run gets the same first-run route refresh the CLI gives `run`."""
    if policy is not None and getattr(policy, "allowed_routes", None):
        return
    from .catalog import RouteCatalog

    catalog = RouteCatalog(db_path=store.path)
    usable = [
        route for route in catalog.data.get("routes", [])
        if route.get("enabled") and route.get("price_state") == "price_observed_zero"
    ]
    if usable:
        return
    print("No verified-free route yet; refreshing route prices from the providers...")
    catalog.refresh_all()

def _lane_for(name: str, root: Path):
    """The lane a gate or retrieve node names, workspace lane first."""
    from .lanes import load_available_lanes

    lane = load_available_lanes(root).get(name)
    if lane is None:
        raise DagError(f"lane '{name}' is not available in {root / 'lanes'}")
    return lane


def _ladder_of(lane: Any) -> list[LadderRung]:
    return list(lane.funnel.rungs() or DEFAULT_LADDER)


def _rung_of(lane: Any, name: str) -> tuple[LadderRung, list[LadderRung]]:
    ladder = _ladder_of(lane)
    for rung in ladder:
        if rung.name == name:
            return rung, ladder
    known = ", ".join(rung.name for rung in ladder) or "none"
    raise DagError(f"lane '{lane.name}' has no rung '{name}' (its rungs: {known})")


class _Carried:
    """One upstream row, read back as the record a gate node can gate on.

    A row that came through a gate or a retrieve node is not a run record, but
    the gate contract only needs three things from it — who it is about, the
    text the verdict stands on, and where that text came from — and all three
    are in the table plus the text file the table points at.
    """

    __slots__ = ("item_id", "candidate", "text", "source_uri", "metadata")

    def __init__(self, item_id: str, candidate: str, text: str, source_uri: str = "") -> None:
        self.item_id = item_id
        self.candidate = candidate
        self.text = text
        self.source_uri = source_uri
        self.metadata = {"entity": candidate, "carried_from": source_uri}


def _read_text(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _text_file(directory: Path, index: int, candidate: str) -> Path:
    slug = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in candidate) or "candidate"
    return directory / "text" / f"{index:04d}-{slug}.txt"


def _execute_gate_node(
    node: GateNode,
    store: HarnessStore,
    root: Path,
    dag_dir: Path,
    state: dict[str, Any],
    dag_id: str,
) -> dict[str, Any]:
    """Put one rung's gates to the population above it, and write the table."""
    lane = _lane_for(node.lane, root)
    rung, ladder = _rung_of(lane, node.rung)
    profile = resolved_gate_profile(lane, workspace=root, profile_path=node.profile)
    directory = dag_dir / node.id
    directory.mkdir(parents=True, exist_ok=True)

    carried: list[Any] = []
    evidence = node.evidence or SNIPPET
    text_paths: dict[str, str] = {}
    upstream_table: str = ""
    if node.from_items:
        items_path = Path(node.from_items)
        if not items_path.is_file():
            items_path = root / node.from_items
        if not items_path.is_file():
            raise DagError(f"gate node '{node.id}' reads '{node.from_items}', which is not a file")
        for index, item in enumerate(load_input_items(items_path)):
            item_id = str(getattr(item, "item_id", "") or "")
            metadata = getattr(item, "metadata", None)
            entity = ""
            if isinstance(metadata, dict):
                entity = str(metadata.get("entity") or metadata.get("domain") or "")
            candidate = entity.strip() or item_id
            text = record_text(item)
            path = _text_file(directory, index, candidate)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            text_paths[item_id] = str(path)
            carried.append(_Carried(item_id, candidate, text,
                                    str(getattr(item, "source_uri", "") or "")))
    elif node.from_run:
        saved_node = (state.get("nodes") or {}).get(node.from_run)
        run_id = str(
            (saved_node or {}).get("run_id") if isinstance(saved_node, dict) else ""
        ) or node.from_run
        try:
            snapshot = store.run_snapshot(run_id)
        except KeyError as exc:
            raise DagError(
                f"gate node '{node.id}' reads run '{run_id}', which does not exist"
            ) from exc
        records, _task = verified_records_from_snapshot(snapshot)
        # The rung that reads search results reads the item the run was given,
        # not the quotes the model chose to cite: a citation is the sentence
        # that supported a claim, and a headcount or a country can sit anywhere
        # on the result the run was handed. The item file is the only place the
        # whole of it still exists.
        given: dict[str, Any] = {}
        input_path = str(snapshot.get("input_path") or "")
        if input_path and Path(input_path).is_file():
            try:
                given = {
                    str(item.item_id): item
                    for item in load_input_items(
                        Path(input_path),
                        only_ids={str(getattr(record, "item_id", "")) for record in records},
                    )
                }
            except (OSError, ValueError):
                given = {}
        for index, record in enumerate(records):
            item_id = str(getattr(record, "item_id", "") or "")
            source = given.get(item_id)
            candidate = candidate_name(source or record, item_id)
            text = record_text(source) if source is not None else record_text(record)
            uri = str(getattr(source or record, "source_uri", "") or "")
            path = _text_file(directory, index, candidate)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            text_paths[item_id] = str(path)
            carried.append(_Carried(item_id, candidate, text, uri))
    else:
        source_id = node.from_gate or node.from_retrieve or ""
        source_state = (state.get("nodes") or {}).get(source_id) or {}
        upstream_table = str(source_state.get("table") or "")
        if not upstream_table or not Path(upstream_table).is_file():
            raise DagError(
                f"gate node '{node.id}' reads '{source_id}', which has no table to read"
            )
        if node.from_retrieve:
            # We read these pages in the node above, so this rung stands on
            # fetched text whether or not it declared that grade itself.
            evidence = node.evidence or FETCHED
        elif not node.evidence:
            evidence = str(source_state.get("evidence") or SNIPPET)
        for row in read_table(upstream_table):
            item_id = str(row.get("item_id") or "")
            text_path = str(row.get("text_path") or "")
            text_paths[item_id] = text_path
            carried.append(
                _Carried(
                    item_id=item_id,
                    candidate=str(row.get("candidate") or item_id),
                    text=_read_text(text_path),
                    source_uri=str(row.get("source_uri") or ""),
                )
            )
    if node.from_gate and node.evidence:
        evidence = node.evidence

    rows, reports = gate_rows(
        carried,
        rung=rung,
        ladder=ladder,
        profile=profile,
        evidence=evidence,
    )
    following = next_rung(ladder, rung)
    for row, report in zip(rows, reports, strict=True):
        row["text_path"] = text_paths.get(str(row["item_id"]), "")
        row["advances"] = advances_to(report, following)
        row["next_rung"] = following.name if following else ""
    path = table_path(directory)
    write_table(path, rows)
    counts = count_rows(rows)
    live = sorted(_evaluable_gates(profile))
    note = ""
    if live == ["kind"] or not live:
        note = (
            "running on the kind question alone: no profile states a headcount, a "
            "territory or an industry, so nothing else can rule a candidate out"
        )
    passing = surviving_ids([row for row in rows if row["advances"]]) if following else []
    return {
        "kind": "gate",
        "lane": node.lane,
        "rung": node.rung,
        "evidence": evidence,
        "gates": list(rung.gates),
        "from_run": node.from_run or "",
        "upstream_table": upstream_table,
        "table": str(path),
        "next_rung": following.name if following else "",
        "counts": counts,
        "gates_live": live,
        "note": note,
        "ids": passing,
        "count": len(passing),
        "gated": len(rows),
        "gates_run": [
            {
                "gate": result.gate,
                "outcome": result.outcome,
                "reason": result.reason,
            }
            for report in reports
            for result in report.results
        ],
    }


def _execute_retrieve_node(
    node: RetrieveNode,
    root: Path,
    dag_dir: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Spend one rung's page visits and write down what came back."""
    from .enrich import walk_entities

    lane = _lane_for(node.lane, root)
    rung, _ladder = _rung_of(lane, node.rung)
    wanted: list[Any] = []
    if node.from_items:
        # No gates asked for, so every captured candidate is walked.
        items_path = Path(node.from_items)
        if not items_path.is_file():
            items_path = root / node.from_items
        if not items_path.is_file():
            raise DagError(
                f"retrieve node '{node.id}' reads '{node.from_items}', which is not a file"
            )
        for item in load_input_items(items_path):
            metadata = getattr(item, "metadata", None)
            entity = ""
            if isinstance(metadata, dict):
                entity = str(metadata.get("entity") or metadata.get("domain") or "")
            wanted.append(_Carried(
                item_id=str(getattr(item, "item_id", "") or ""),
                candidate=entity.strip() or str(getattr(item, "item_id", "") or ""),
                text=record_text(item),
                source_uri=str(getattr(item, "source_uri", "") or ""),
            ))
    else:
        source_state = (state.get("nodes") or {}).get(node.from_gate) or {}
        upstream_table = str(source_state.get("table") or "")
        if not upstream_table or not Path(upstream_table).is_file():
            raise DagError(
                f"retrieve node '{node.id}' reads gate '{node.from_gate}', which has no table"
            )
        passing = {str(item) for item in source_state.get("ids") or []}
        for row in read_table(upstream_table):
            item_id = str(row.get("item_id") or "")
            if item_id not in passing:
                # The rung above decided this candidate is not owed a page visit.
                # Not reading it is the decision, so it is not in this table.
                continue
            wanted.append(_Carried(
                item_id=item_id,
                candidate=str(row.get("candidate") or item_id),
                text=_read_text(str(row.get("text_path") or "")),
                source_uri=str(row.get("source_uri") or ""),
            ))
    # The walk is the engine's: the same gap-driven, per-host-paced walk every
    # research run does, reading exactly the surfaces this rung declared.
    extra, walk_report = walk_entities(
        wanted,
        lane,
        per_surface=node.per_surface,
        max_pages=node.max_pages,
        timeout=node.timeout,
        delay=node.delay,
        respect_robots=node.respect_robots,
        vendor_stories=node.vendor_stories,
        surface_order=list(rung.surfaces),
    )
    by_entity: dict[str, list[Any]] = {}
    for item in extra:
        by_entity.setdefault(str(getattr(item, "item_id", "") or ""), []).append(item)
    walked = {str(entry.get("entity") or ""): entry for entry in walk_report}

    directory = dag_dir / node.id
    directory.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for index, candidate_item in enumerate(wanted):
        candidate = candidate_item.candidate
        entry = walked.get(candidate_item.item_id, {})
        records = by_entity.get(candidate_item.item_id, [])
        if not entry:
            outcome, because = "complete", "nothing this rung's evidence could add was missing"
        elif records:
            outcome, because = "read", ""
        else:
            outcome, because = "nothing", f"no page on {', '.join(rung.surfaces)} named them"
        text = "\n\n".join(
            str(getattr(record, "text", "") or "") for record in records
        )
        path = _text_file(directory, index, candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        rows.append({
            "item_id": candidate_item.item_id,
            "candidate": candidate,
            "rung": rung.name,
            "evidence": FETCHED,
            "outcome": outcome,
            "earned": "",
            "because": because,
            "gates": [],
            "surfaces": entry.get("by_surface") or {},
            "pages": [str(getattr(record, "source_uri", "") or "") for record in records],
            "text_path": str(path),
            "source_uri": candidate_item.source_uri,
            "visited": entry.get("visited", 0),
            "kept": entry.get("kept", 0),
            "skipped": entry.get("skipped") or [],
            "advances": True,
        })
    path = table_path(directory)
    write_table(path, rows)
    items_path = directory / "items.jsonl"
    with items_path.open("w", encoding="utf-8") as handle:
        for item in extra:
            handle.write(json.dumps(item.model_dump(mode="json", by_alias=True)) + "\n")
    return {
        "kind": "retrieve",
        "lane": node.lane,
        "rung": rung.name,
        "surfaces": list(rung.surfaces),
        "from_gate": node.from_gate,
        "table": str(path),
        "items": str(items_path),
        "count": len(rows),
        "read": sum(1 for row in rows if row["outcome"] == "read"),
        "empty": sum(1 for row in rows if row["outcome"] == "nothing"),
        "skipped": sum(1 for row in rows if row["outcome"] == "complete"),
        "visited": sum(int(row["visited"]) for row in rows),
        "ids": [str(row["item_id"]) for row in rows],
    }


def run_dag(
    spec: DagSpec,
    store: HarnessStore,
    workspace_root: str | Path = ".",
    dag_id: str | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Execute a DAG spec topologically; return a lineage summary.

    Node run ids are deterministic (``{dag_id}-{node_id}``), so re-running
    resumes: completed ``run`` nodes are reused from SQLite, while
    ``filter``/``export`` nodes re-execute (cheap and deterministic).
    Lineage (spec digest, per-node run ids, counts, digests, artifacts) is
    recorded in ``runs/<dag_id>/dag.json``.
    """
    root = Path(workspace_root).expanduser().resolve()
    dag_id = dag_id or f"{spec.name}-{spec_digest(spec)[:8]}"
    order = spec.topo_order()
    by_id = {node.id: node for node in spec.nodes}
    dag_dir = root / "runs" / dag_id
    dag_dir.mkdir(parents=True, exist_ok=True)
    sidecar = dag_dir / "dag.json"

    _ensure_routes(store, next((node.policy for node in spec.nodes if isinstance(node, RunNode)), None))
    state: dict[str, Any] = {"dag_id": dag_id, "spec_digest": spec_digest(spec), "nodes": {}}
    if resume and sidecar.is_file():
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
            if loaded.get("spec_digest") == state["spec_digest"]:
                state = loaded
        except (ValueError, OSError):
            pass

    id_sets: dict[str, set[str]] = {}
    for node_id, saved in state.get("nodes", {}).items():
        if isinstance(saved, dict) and isinstance(saved.get("ids"), list):
            id_sets[node_id] = set(saved["ids"])

    for node_id in order:
        node = by_id[node_id]
        saved = state["nodes"].get(node_id) if isinstance(state.get("nodes"), dict) else None
        if isinstance(node, ReviewNode):
            state["nodes"][node_id] = _execute_review_node(node, store, root, state, dag_id)
            sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        if isinstance(node, GateNode):
            if resume and isinstance(saved, dict) and saved.get("table") and Path(saved["table"]).is_file():
                saved["cached"] = True
                id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
                continue
            saved = _execute_gate_node(node, store, root, dag_dir, state, dag_id)
            state["nodes"][node_id] = saved
            id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
            sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        if isinstance(node, RetrieveNode):
            if resume and isinstance(saved, dict) and saved.get("table") and Path(saved["table"]).is_file():
                saved["cached"] = True
                id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
                continue
            saved = _execute_retrieve_node(node, root, dag_dir, state)
            state["nodes"][node_id] = saved
            id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
            sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        if isinstance(node, RunNode):
            run_id = f"{dag_id}-{node_id}"
            if resume and isinstance(saved, dict) and saved.get("run_id") == run_id and _run_complete(store, run_id):
                saved["cached"] = True
                continue
            if node.task_from:
                saved_cal = state["nodes"].get(node.task_from) if isinstance(state.get("nodes"), dict) else None
                revision = saved_cal.get("revision") if isinstance(saved_cal, dict) else None
                if not revision:
                    raise DagError(
                        f"run node '{node.id}' task_from '{node.task_from}' has no registered "
                        "revision (calibrate node did not apply)"
                    )
                task = store.get_task_revision(revision)
            else:
                assert node.task is not None
                task = _resolve_dag_task(node.task, store, root)
            upstream = [id_sets[ref] for ref in node.ids_from if ref in id_sets]
            only_ids = _combine_ids(upstream, node.ids_mode)
            input_path = (root / node.input).expanduser()
            items = load_input_items(
                input_path,
                id_column=node.id_column,
                text_column=node.text_column,
                title_column=node.title_column,
                uri_column=node.uri_column,
                only_ids=only_ids,
            )
            policy = node.policy
            packet = Engine(task=task, store=store, policy=policy).run_campaign(
                raw_items=items,
                run_id=run_id,
                input_path=str(input_path),
                concurrency=node.sessions,
                max_attempts=node.max_attempts,
                output_packet_path=dag_dir / node_id / "clean_packet.json",
            )
            state["nodes"][node_id] = {
                "kind": "run",
                "run_id": run_id,
                "verified": packet.get("total_verified_records", 0),
                "input_digest": packet.get("input_digest"),
            }
        elif isinstance(node, RescoreNode):
            run_id = node.run_id or f"{dag_id}-{node_id}"
            if resume and isinstance(saved, dict) and saved.get("run_id") == run_id and _run_complete(store, run_id):
                saved["cached"] = True
                continue
            _execute_rescore_node(node, run_id, state, store, root, dag_id)
        elif isinstance(node, CalibrateNode):
            if resume and isinstance(saved, dict) and saved.get("report"):
                saved["cached"] = True
                continue
            _execute_calibrate_node(node, state, store, root, dag_dir)
        elif isinstance(node, FilterNode):
            run_id = _resolve_run_ref(state, dag_id, node.from_run)
            snapshot = store.run_snapshot(run_id)
            records, _ = verified_records_from_snapshot(snapshot)
            selected = _filter_and_sort_records(
                records,
                sort=node.sort,
                top=node.top,
                claim_filter=node.filter,
            )
            ids = {r.item_id for r in selected}
            id_sets[node_id] = ids
            ids_path = dag_dir / node_id / "ids.json"
            ids_path.parent.mkdir(parents=True, exist_ok=True)
            ids_path.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
            state["nodes"][node_id] = {
                "kind": "filter",
                "from_run": run_id,
                "ids": sorted(ids),
                "count": len(ids),
            }
        elif isinstance(node, ExportNode):
            run_id = _resolve_run_ref(state, dag_id, node.from_run)
            snapshot = store.run_snapshot(run_id)
            dest = root / node.output if node.output else dag_dir / node_id / f"output.{node.format}"
            packet = export_clean_packet(
                snapshot,
                dest,
                export_format=node.format,
                sort=node.sort,
                top=node.top,
                rank=node.rank,
                claim_filter=node.filter,
            )
            state["nodes"][node_id] = {
                "kind": "export",
                "from_run": run_id,
                "output": str(dest.resolve()) if dest.is_absolute() else str((root / dest).resolve()),
                "records": packet.get("total_verified_records", 0),
            }
        else:  # Unreachable: DagSpec parsing admits only the five node kinds.
            raise DagError(f"unknown node kind for '{node_id}'")
        sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")

    sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state

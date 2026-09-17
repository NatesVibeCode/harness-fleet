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
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .calibrate import PARAM_KINDS, collect_observations, fit_calibration
from .catalog import RouteCatalog
from .discover import write_items_jsonl
from .engine import Engine
from .export import (
    _filter_and_sort_records,
    export_clean_packet,
    verified_records_from_snapshot,
)
from .gates import (
    DEFAULT_LADDER,
    FETCHED,
    RESOLVABLE_GATES,
    SNIPPET,
    LadderRung,
    _evaluable_gates,
)
from .input_data import iter_input_items, load_input_items
from .ledger import Ledger
from .models import (
    CalibrationReport,
    ClaimFilter,
    ClosedModel,
    InputItem,
    RoutePolicy,
    SortSpec,
)
from .profile import IdealCompanyProfile
from .providers.registry import ProviderRegistry, ProviderResolutionError
from .rungs import (
    RungTables,
    advances_to,
    candidate_name,
    count_rows,
    gate_rows,
    next_rung,
    population,
    record_text,
    resolved_gate_profile,
    surviving_ids,
)
from .store import HarnessStore
from .task import create_task_from_preset, load_task_spec


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
    #: The rung this node runs, as data. A spec that carries its own rung keeps
    #: its meaning after the lane file is revised: re-running an old graph runs
    #: the gates it ran, not the gates the lane has since grown. Empty falls back
    #: to reading the rung out of the lane, which is what a hand-written spec
    #: wants.
    rung_of: dict[str, Any] | None = None
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
    rung_of: dict[str, Any] | None = None
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


class ResolveNode(ClosedModel):
    """Settle a firmographic with one flat search instead of a site visit.

    A headcount and a country are facts about a company that somebody has
    already written down — on a register, a profile, a directory. Walking a
    site for them spends a page fetch per entity and often finds nothing,
    because the address lives on the contact page nobody links from the
    homepage. Asking the search engine the question directly costs one search
    and answers it: the candidate's name, plus the field.

    Only the gates the rung says a search may settle are asked, and only for the
    candidates still holding them open. Everything else is carried through
    untouched, so this node can never make a candidate worse off.
    """

    kind: Literal["resolve"] = "resolve"
    id: str
    lane: str
    from_gate: str
    #: Which gates a search may put. Empty means the node asks nothing.
    fields: list[str] = Field(default_factory=list)
    #: The query, with ``{name}`` and ``{field}`` substituted. The words are
    #: data because what a field is called is the operator's language, not the
    #: engine's: "address" finds a registered office, "headquarters" finds the
    #: city, "employees" finds the headcount.
    query: str = '"{name}" {field}'
    field_words: dict[str, str] = Field(default_factory=dict)
    backends: list[str] = Field(default_factory=list)
    max_results: int = 6
    timeout: float = 20.0

    @model_validator(mode="after")
    def check_fields(self) -> ResolveNode:
        unknown = [f for f in self.fields if f not in RESOLVABLE_GATES]
        if unknown:
            raise DagError(
                f"resolve node '{self.id}' asks for {unknown}; a search can settle "
                f"{sorted(RESOLVABLE_GATES)}"
            )
        return self


class ScoreNode(ClosedModel):
    """Judge the survivors, as a node, and write the answer onto the list.

    Scoring used to happen after the graph, in whatever called it, which meant
    the one stage that produces the number and the facts landed nowhere: the
    run's tables described the funnel, and the deliverable described the score,
    and nothing joined them. It is a node now. It reads the population the
    ladder left standing, gathers what the walks read about those firms, runs
    the campaign, and writes a row per firm carrying the score and the facts —
    which is also what the running list records, so the ledger's answer and the
    run's answer are the same answer.
    """

    kind: Literal["score"] = "score"
    id: str
    lane: str
    #: The gate whose survivors are judged. Usually the last one, and absent
    #: when the graph ran no gates at all — a walk-only run is still scored.
    from_gate: str = ""
    #: Every node whose rows and items belong to these firms: the walks, and the
    #: file of candidates the run started from.
    from_nodes: list[str] = Field(default_factory=list)
    from_items: str | None = None
    task: str = ""
    #: The run id this campaign is filed under. Empty derives one from the graph,
    #: which is right for a graph nobody else is reading; a command that names
    #: the run its deliverable lives in passes it here.
    run_id: str = ""
    sessions: int = 4
    max_attempts: int = 300
    policy: RoutePolicy | None = None
    top: int | None = None

    @model_validator(mode="after")
    def check_source(self) -> ScoreNode:
        if not self.from_gate and not self.from_nodes:
            raise DagError(
                f"score node '{self.id}' needs something to judge: 'from_gate' or 'from_nodes'"
            )
        return self


DagNode = (
    RunNode | RescoreNode | CalibrateNode | FilterNode | ExportNode | ReviewNode
    | GateNode | RetrieveNode | ResolveNode | ScoreNode
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
                pairs: list[tuple[str, tuple[type, ...]]] = []
                if node.from_items:
                    # Captured items are a file the run wrote before the first
                    # rung: no node produces them, so there is no edge here. The
                    # file itself is checked when the node runs — reading the
                    # filesystem to answer a question about the *graph* would
                    # make ordering fail for a spec whose input has since moved.
                    pass
                if node.from_run:
                    # A gate reads either a run node in this graph or an
                    # existing run by id, the way a review node does: a ladder
                    # is worth running over a discovery run that already
                    # happened, and demanding a `run` node for it would make
                    # every ladder graph begin with work it does not need.
                    if by_id.get(node.from_run) is not None:
                        pairs.append((node.from_run, (RunNode, RescoreNode)))
                if node.from_gate:
                    pairs.append((node.from_gate, (GateNode, ResolveNode)))
                if node.from_retrieve:
                    pairs.append((node.from_retrieve, (RetrieveNode,)))
                for source_ref, allowed in pairs:
                    target = by_id.get(source_ref)
                    if target is None:
                        raise DagError(
                            f"node '{node.id}' references unknown node '{source_ref}'"
                        )
                    if not isinstance(target, allowed):
                        wanted = " or ".join(cls.__name__ for cls in allowed)
                        raise DagError(
                            f"node '{node.id}' reads '{source_ref}', which is not a {wanted}"
                        )
                    if source_ref == node.id:
                        raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {source_ref for source_ref, _ in pairs}
            elif isinstance(node, ResolveNode):
                ref = node.from_gate
                target = by_id.get(ref)
                if target is None:
                    raise DagError(f"node '{node.id}' references unknown node '{ref}'")
                if not isinstance(target, GateNode):
                    raise DagError(f"node '{node.id}' from_gate '{ref}' is not a gate node")
                if ref == node.id:
                    raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {ref}
            elif isinstance(node, ScoreNode):
                refs = [ref for ref in [node.from_gate, *node.from_nodes] if ref]
                for source_ref in refs:
                    target = by_id.get(source_ref)
                    if target is None:
                        raise DagError(
                            f"node '{node.id}' references unknown node '{source_ref}'"
                        )
                    if source_ref == node.id:
                        raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = set(refs)
            elif isinstance(node, RetrieveNode):
                gate_ref = node.from_gate
                if gate_ref is None:
                    deps[node.id] = set()
                    continue
                target = by_id.get(gate_ref)
                if target is None:
                    raise DagError(f"node '{node.id}' references unknown node '{gate_ref}'")
                if not isinstance(target, (GateNode, ResolveNode)):
                    raise DagError(
                        f"node '{node.id}' from_gate '{gate_ref}' is not a gate or resolve node"
                    )
                if gate_ref == node.id:
                    raise DagError(f"node '{node.id}' references itself")
                deps[node.id] = {gate_ref}
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

#: What a search is asked, per gate. A person searching for a company's address
#: types "address"; the engine's word for the gate is "location". The mapping is
#: data so a lane can change the question without changing the engine.
DEFAULT_FIELD_WORDS: dict[str, str] = {
    "location": "address OR headquarters",
    "size": "headcount OR employees",
    "vertical": "industries served",
    "kind": "services or software company",
}


def _lane_for(name: str, root: Path):
    """The lane a gate or retrieve node names, workspace lane first."""
    from .lanes import load_available_lanes

    lane = load_available_lanes(root).get(name)
    if lane is None:
        raise DagError(f"lane '{name}' is not available in {root / 'lanes'}")
    return lane


def _ladder_of(lane: Any) -> list[LadderRung]:
    return list(lane.funnel.rungs() or DEFAULT_LADDER)


def _rung_from(node: Any, lane: Any, name: str) -> tuple[LadderRung, list[LadderRung]]:
    """The rung this node runs: its own copy first, the lane's ladder otherwise."""
    contract = getattr(node, "rung_of", None)
    if isinstance(contract, dict) and contract.get("name"):
        rung = LadderRung(**contract)
        return rung, _ladder_of(lane)
    return _rung_of(lane, name)


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


def _execute_gate_node(
    node: GateNode,
    store: HarnessStore,
    root: Path,
    state: dict[str, Any],
    dag_id: str,
) -> dict[str, Any]:
    """Put one rung's gates to the population above it, and write the table."""
    lane = _lane_for(node.lane, root)
    rung, ladder = _rung_from(node, lane, node.rung)
    profile = resolved_gate_profile(lane, workspace=root, profile_path=node.profile)
    tables = RungTables(store)

    carried: list[Any] = []
    evidence = node.evidence or SNIPPET
    texts: dict[str, str] = {}
    upstream_node: str = ""
    if node.from_items:
        items_path = Path(node.from_items)
        if not items_path.is_file():
            items_path = root / node.from_items
        if not items_path.is_file():
            raise DagError(f"gate node '{node.id}' reads '{node.from_items}', which is not a file")
        for item in load_input_items(items_path):
            item_id = str(getattr(item, "item_id", "") or "")
            metadata = getattr(item, "metadata", None)
            entity = ""
            if isinstance(metadata, dict):
                entity = str(metadata.get("entity") or metadata.get("domain") or "")
            candidate = entity.strip() or item_id
            text = record_text(item)
            texts[item_id] = text
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
        for record in records:
            item_id = str(getattr(record, "item_id", "") or "")
            source = given.get(item_id)
            candidate = candidate_name(source or record, item_id)
            text = record_text(source) if source is not None else record_text(record)
            uri = str(getattr(source or record, "source_uri", "") or "")
            texts[item_id] = text
            carried.append(_Carried(item_id, candidate, text, uri))
    else:
        source_id = node.from_gate or node.from_retrieve or ""
        source_state = (state.get("nodes") or {}).get(source_id) or {}
        upstream_node = source_id
        if not isinstance(source_state, dict) or not source_state:
            raise DagError(f"gate node '{node.id}' reads '{source_id}', which has not run")
        if node.from_retrieve:
            # We read these pages in the node above, so this rung stands on
            # fetched text whether or not it declared that grade itself.
            evidence = node.evidence or FETCHED
        elif not node.evidence:
            evidence = str(source_state.get("evidence") or SNIPPET)
        upstream_texts = tables.texts(dag_id, source_id)
        for row in tables.rows(dag_id, source_id):
            item_id = str(row.get("item_id") or "")
            texts[item_id] = upstream_texts.get(item_id, "")
            carried.append(
                _Carried(
                    item_id=item_id,
                    candidate=str(row.get("candidate") or item_id),
                    text=upstream_texts.get(item_id, ""),
                    source_uri=str(row.get("source_uri") or ""),
                )
            )

    rows, reports = gate_rows(
        carried,
        rung=rung,
        ladder=ladder,
        profile=profile,
        evidence=evidence,
    )
    following = next_rung(ladder, rung)
    for gate_row, report in zip(rows, reports, strict=True):
        gate_row["advances"] = advances_to(report, following)
        gate_row["next_rung"] = following.name if following else ""
    attempt = tables.write(dag_id, node.id, rows, texts=texts, kind="gate", lane=node.lane)
    Ledger(store).record(rows, dag_id=dag_id, node_id=node.id, run_seq=attempt, lane=node.lane)
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
        "from_node": upstream_node,
        "next_rung": following.name if following else "",
        "attempt": attempt,
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
    store: HarnessStore,
    root: Path,
    dag_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Spend one rung's page visits and write down what came back."""
    from .enrich import walk_entities

    lane = _lane_for(node.lane, root)
    rung, _ladder = _rung_from(node, lane, node.rung)
    tables = RungTables(store)
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
        if not isinstance(source_state, dict) or not source_state:
            raise DagError(f"retrieve node '{node.id}' reads '{node.from_gate}', which has not run")
        passing = {str(item) for item in source_state.get("ids") or []}
        gate_id = str(node.from_gate or "")
        upstream_texts = tables.texts(dag_id, gate_id)
        for row in tables.rows(dag_id, gate_id):
            item_id = str(row.get("item_id") or "")
            if item_id not in passing:
                # The rung above decided this candidate is not owed a page visit.
                # Not reading it is the decision, so it is not in this table.
                continue
            wanted.append(_Carried(
                item_id=item_id,
                candidate=str(row.get("candidate") or item_id),
                text=upstream_texts.get(item_id, ""),
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

    rows: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    for candidate_item in wanted:
        candidate = candidate_item.candidate
        entry = walked.get(candidate_item.item_id, {})
        records = by_entity.get(candidate_item.item_id, [])
        if not entry:
            outcome, because = "complete", "nothing this rung's evidence could add was missing"
        elif records:
            outcome, because = "read", ""
        else:
            outcome, because = "nothing", f"no page on {', '.join(rung.surfaces)} named them"
        texts[candidate_item.item_id] = "\n\n".join(
            str(getattr(record, "text", "") or "") for record in records
        )
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
            "source_uri": candidate_item.source_uri,
            "visited": entry.get("visited", 0),
            "kept": entry.get("kept", 0),
            "skipped": entry.get("skipped") or [],
            "advances": True,
        })
    attempt = tables.write(dag_id, node.id, rows, texts=texts, kind="retrieve", lane=node.lane)
    Ledger(store).record(rows, dag_id=dag_id, node_id=node.id, run_seq=attempt, lane=node.lane)
    # What the walk gathered is the next node's input, so it goes in the same
    # place: a table queried out of the database rather than a file to re-read.
    tables.write_items(
        dag_id,
        node.id,
        [item.model_dump(mode="json", by_alias=True) for item in extra],
        run_seq=attempt,
    )
    return {
        "kind": "retrieve",
        "lane": node.lane,
        "rung": rung.name,
        "surfaces": list(rung.surfaces),
        "from_gate": node.from_gate,
        "attempt": attempt,
        "items": len(extra),
        "count": len(rows),
        "read": sum(1 for row in rows if row["outcome"] == "read"),
        "empty": sum(1 for row in rows if row["outcome"] == "nothing"),
        "skipped": sum(1 for row in rows if row["outcome"] == "complete"),
        "visited": sum(int(row["visited"]) for row in rows),
        "ids": [str(row["item_id"]) for row in rows],
    }


def _open_gates(row: dict[str, Any]) -> set[str]:
    """Which gates this row is still holding open, from its own verdicts."""
    verdicts = row.get("gates")
    if isinstance(verdicts, str):
        try:
            verdicts = json.loads(verdicts or "[]")
        except ValueError:
            return set()
    if not isinstance(verdicts, list):
        return set()
    return {
        str(result.get("gate"))
        for result in verdicts
        if isinstance(result, dict) and result.get("outcome") == "unknown"
    }


def _execute_resolve_node(
    node: ResolveNode,
    store: HarnessStore,
    root: Path,
    dag_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Ask the search engine the firmographic question, once per candidate."""
    from .discover import web_search

    _lane_for(node.lane, root)
    tables = RungTables(store)
    source_state = (state.get("nodes") or {}).get(node.from_gate) or {}
    if not isinstance(source_state, dict) or not source_state:
        raise DagError(f"resolve node '{node.id}' reads '{node.from_gate}', which has not run")
    passing = {str(item) for item in source_state.get("ids") or []}
    upstream_texts = tables.texts(dag_id, node.from_gate)
    rows: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    for row in tables.rows(dag_id, node.from_gate):
        item_id = str(row.get("item_id") or "")
        if item_id not in passing:
            continue
        candidate = str(row.get("candidate") or item_id)
        carried_text = upstream_texts.get(item_id, "")
        asked = [field for field in node.fields if field in _open_gates(row)]
        queries: list[str] = []
        hits: list[Any] = []
        for field in asked:
            word = node.field_words.get(field) or DEFAULT_FIELD_WORDS.get(field, field)
            query = node.query.format(name=candidate, field=word)
            queries.append(query)
            try:
                found = web_search(
                    query,
                    backends=tuple(node.backends) or ("ddgs",),
                    max_results=node.max_results,
                    timeout=node.timeout,
                )
            except Exception as exc:  # a search that fails answers nothing
                hits.append({"query": query, "error": str(exc)[:200]})
                continue
            hits.extend(
                {
                    "query": query,
                    "url": hit.url,
                    "title": hit.title,
                    "snippet": hit.snippet,
                }
                for hit in found
            )
        found_text = "\n".join(
            f"{hit.get('title') or ''} — {hit.get('snippet') or ''} ({hit.get('url') or ''})".strip()
            for hit in hits
            if hit.get("url")
        )
        texts[item_id] = carried_text + (("\n\n" + found_text) if found_text else "")
        rows.append({
            "item_id": item_id,
            "candidate": candidate,
            "rung": node.rung if hasattr(node, "rung") else "",
            "evidence": SNIPPET,
            "outcome": ("resolved" if found_text else "nothing") if asked else "complete",
            "earned": "",
            "because": (
                "" if not asked
                else (f"asked {len(asked)} question(s): {', '.join(asked)}"
                      if found_text else
                      f"nothing on {', '.join(asked)}: {'; '.join(queries)}")
            ),
            "gates": [],
            "surfaces": {},
            "pages": [str(hit.get("url") or "") for hit in hits if hit.get("url")],
            "queries": queries,
            "hits": len([hit for hit in hits if hit.get("url")]),
            "source_uri": str(row.get("source_uri") or ""),
            "advances": True,
        })
    attempt = tables.write(dag_id, node.id, rows, texts=texts, kind="resolve", lane=node.lane)
    Ledger(store).record(rows, dag_id=dag_id, node_id=node.id, run_seq=attempt, lane=node.lane)
    return {
        "kind": "resolve",
        "lane": node.lane,
        "fields": list(node.fields),
        "from_gate": node.from_gate,
        "evidence": SNIPPET,
        "attempt": attempt,
        "count": len(rows),
        "asked": sum(1 for row in rows if row["queries"]),
        "questions": sum(len(row["queries"]) for row in rows),
        "resolved": sum(1 for row in rows if row["outcome"] == "resolved"),
        "nothing": sum(1 for row in rows if row["outcome"] == "nothing"),
        "queries": sorted({q for row in rows for q in row["queries"]}),
        "ids": [str(row["item_id"]) for row in rows],
    }


def _write_evidence_readout(
    root: Path,
    run_id: str,
    records: Sequence[Any],
    dossiers: Sequence[Any],
    tiers: dict[str, str],
) -> None:
    """Kinds, tier caps and lone claims for the run, derived from its own text."""
    from .evidence import entity_evidence, summarize

    by_id = {str(dossier.item_id): dossier for dossier in dossiers}
    per_item: dict[str, Any] = {}
    for record in records:
        item_id = str(record.item_id)
        dossier = by_id.get(item_id)
        claims = record.claims if isinstance(record.claims, dict) else {}
        claimed = claims.get("fit_tier")
        per_item[item_id] = entity_evidence(
            str(getattr(dossier, "text", "") or ""),
            tier=tiers.get(item_id) or (claimed if isinstance(claimed, str) else None),
            source_uri=str(getattr(dossier, "source_uri", "") or ""),
        )
    payload = {
        "run_id": run_id,
        "items": per_item,
        "kind_totals": summarize(read["kinds"] for read in per_item.values()),
        "tier_capped": {
            item_id: read["tier_reasons"]
            for item_id, read in per_item.items()
            if read["tier_capped"]
        },
        "contradictions": {
            item_id: read["contradictions"]
            for item_id, read in per_item.items()
            if read["contradictions"]
        },
    }
    path = Path(root) / "runs" / run_id / "evidence.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # A read-only workspace must not lose the run; the readout is derived.
        pass


def _capped_tier(ceiling: str, text: str, uri: str = "") -> str:
    """The tier the gathered evidence supports, for one entity.

    The uri is part of the evidence: an unsectioned dossier is classified by
    where it came from (a case study page, a hiring board), and the kinds that
    classification yields are what a tier is capped against. Dropping it here
    told an operator to go and gather a kind the run already had.
    """
    if not ceiling:
        return ""
    from .evidence import coverage, enforce_tier

    capped, _missing = enforce_tier(ceiling, coverage(text, uri))
    return str(capped or "")


def _score_rows(
    records: Sequence[Any], keys: dict[str, str], tiers: dict[str, str]
) -> list[dict[str, Any]]:
    """One row per scored firm: the number, the facts, and where they came from."""
    rows: list[dict[str, Any]] = []
    for record in records:
        item_id = str(getattr(record, "item_id", "") or "")
        claims = getattr(record, "claims", None)
        claims = dict(claims) if isinstance(claims, dict) else {}
        answers = claims.get("answers")
        facts: dict[str, Any] = {k: v for k, v in claims.items() if k != "answers"}
        if isinstance(answers, dict):
            facts.update(answers)
        score = 0.0
        for key in ("score", "fit_score", "priority"):
            value = claims.get(key)
            if isinstance(value, int | float):
                score = float(value)
                break
        rows.append({
            "item_id": item_id,
            "candidate": keys.get(item_id, item_id),
            "rung": "score",
            "evidence": FETCHED,
            # Not an outcome: the score stage records a number, a tier and the
            # facts, and admission is the ladder's to decide. Writing the tier
            # here put three vocabularies in one column — a ladder verdict, a
            # tier name, and the word "scored" — and lost the verdict a firm
            # had actually earned.
            "outcome": "",
            "earned": "",
            "because": str(claims.get("reason") or claims.get("identified_gap") or "")[:400],
            "gates": [],
            "surfaces": {},
            "pages": [],
            "score": score,
            "tier": str(tiers.get(item_id) or claims.get("fit_tier") or ""),
            "facts": facts,
            "advances": False,
        })
    return rows


def _execute_score_node(
    node: ScoreNode,
    store: HarnessStore,
    root: Path,
    dag_dir: Path,
    dag_id: str,
    state: dict[str, Any],
    gate_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Judge the population the ladder left, and record what came back."""
    from .bundler import bundle_records

    lane = _lane_for(node.lane, root)
    tables = RungTables(store)
    # Who is standing is a question about every gate in the graph, not about the
    # last one: a candidate killed at the surface rung is in the walk's table
    # (it was walked before it was judged) and must not be scored anyway.
    gates = list(gate_ids) or [node.from_gate]
    standing = population(tables, dag_id, [gate for gate in gates if gate])
    sources: list[Any] = []
    empty_sources = 0
    for source_id in [*(node.from_nodes or []), *([node.from_gate] if node.from_gate else [])]:
        for payload in tables.items(dag_id, source_id):
            sources.append(InputItem.model_validate(payload))
        for row in tables.rows(dag_id, source_id):
            item_id = str(row.get("item_id") or "")
            text = tables.text(dag_id, source_id, item_id) if item_id else ""
            if not item_id or not text.strip():
                # Nothing was read about this one at this node, so it brings no
                # evidence to judge. Counted rather than padded: a placeholder
                # would be a source that does not exist.
                if item_id:
                    empty_sources += 1
                continue
            sources.append(InputItem(
                item_id=item_id,
                text=text,
                source_uri=str(row.get("source_uri") or ""),
                metadata={"node": source_id, "rung": row.get("rung") or ""},
            ))
    if node.from_items:
        items_path = Path(node.from_items)
        if not items_path.is_file():
            items_path = root / node.from_items
        if items_path.is_file():
            for item in load_input_items(items_path):
                if str(getattr(item, "item_id", "")) in standing:
                    sources.append(item)
    wanted = {source_id: None for source_id in standing}
    dossiers = [
        dossier for dossier in bundle_records(
            [item for item in sources if str(getattr(item, "item_id", "")) in wanted]
        )
        if str(dossier.item_id) in standing
    ]
    if node.top:
        dossiers = dossiers[: node.top]
    if not dossiers:
        # Nothing is standing, or nothing was gathered about what is. Spending a
        # campaign on zero dossiers would report a run that scored nothing as if
        # the model had failed, when the answer is that the ladder left nobody —
        # and the node says which of the two it was.
        reason = (
            "the ladder eliminated every candidate"
            if not standing
            else "nothing was gathered about the candidates still standing"
        )
        attempt = tables.write(dag_id, node.id, [], kind="score", lane=node.lane)
        return {
            "kind": "score",
            "lane": node.lane,
            "run_id": node.run_id or "",
            "attempt": attempt,
            "judged": len(standing),
            "empty_sources": empty_sources,
            "dossiers": 0,
            "verified": 0,
            "count": 0,
            "scored": 0,
            "mean_score": 0.0,
            "skipped": reason,
            "ids": [],
        }

    # A research dossier is many walked pages; five of them in one request was
    # 188,236 tokens and no free model would take it. The budget belongs to the
    # call that knows which routes will answer, not to the task's identity.
    node_batch_chars = 48_000
    name = node.task or lane.preset
    try:
        task = _resolve_dag_task(name, store, root)
    except KeyError:
        # A preset is enough for a first run: the task is registered here, which
        # is what the command used to do before the scoring stage was a node.
        task = create_task_from_preset(name, preset_name=name)
        store.register_task(task)
    run_id = node.run_id or f"{dag_id}-{node.id}"
    dossiers_path = dag_dir / node.id / "dossiers.jsonl"
    dossiers_path.parent.mkdir(parents=True, exist_ok=True)
    write_items_jsonl(dossiers, dossiers_path)
    packet = Engine(
        task=task, store=store, policy=node.policy, max_batch_chars=node_batch_chars
    ).run_campaign(
        raw_items=iter(dossiers),
        run_id=run_id,
        input_path=str(dossiers_path),
        concurrency=node.sessions,
        max_attempts=node.max_attempts,
        output_packet_path=dag_dir / node.id / "clean_packet.json",
        policy=node.policy,
    )
    records, _task = verified_records_from_snapshot(store.run_snapshot(run_id))
    ceiling = lane.tier or ""
    keys = {str(dossier.item_id): str(dossier.item_id) for dossier in dossiers}
    text_by_id = {str(dossier.item_id): str(dossier.text or "") for dossier in dossiers}
    uri_by_id = {str(dossier.item_id): str(dossier.source_uri or "") for dossier in dossiers}
    tiers: dict[str, str] = {}
    for record in records:
        item_id = str(record.item_id)
        # A tier is a claim about evidence, so it is capped at what the gathered
        # pages actually support rather than taken from the model's word — and
        # where each page came from is part of what it supports.
        tiers[item_id] = _capped_tier(
            ceiling, text_by_id.get(item_id, ""), uri_by_id.get(item_id, "")
        )
    rows = _score_rows(records, keys, tiers)
    # The run's own evidence readout, written where every reader of a run looks
    # for it (runs/<run_id>/evidence.json). It used to be written only by the
    # research command, so a run scored through the graph — the same scoring
    # stage, the same rule — had no readout for the board or the drawer to read.
    _write_evidence_readout(root, run_id, records, dossiers, tiers)
    attempt = tables.write(dag_id, node.id, rows, kind="score", lane=node.lane)
    Ledger(store).record(
        [{**row, "run_id": run_id} for row in rows],
        dag_id=dag_id, node_id=node.id, run_seq=attempt, lane=node.lane,
    )
    return {
        "kind": "score",
        "lane": node.lane,
        "run_id": run_id,
        "attempt": attempt,
        "skipped": "",
        "task": task.name,
        "dossiers": len(dossiers),
        "judged": len(standing),
        "empty_sources": empty_sources,
        "verified": int(packet.get("total_verified_records") or 0),
        "count": len(rows),
        "scored": sum(1 for row in rows if row["score"]),
        "mean_score": round(
            sum(row["score"] for row in rows) / len(rows), 2
        ) if rows else 0.0,
        "ids": [str(row["item_id"]) for row in rows],
        "dossiers_path": str(dossiers_path),
        "packet": str(dag_dir / node.id / "clean_packet.json"),
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

    _ensure_routes(
        store,
        next(
            (
                node.policy
                for node in spec.nodes
                if isinstance(node, RunNode | ScoreNode)
            ),
            None,
        ),
    )
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
            if resume and isinstance(saved, dict) and saved.get("ids") is not None:
                saved["cached"] = True
                id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
                continue
            saved = _execute_gate_node(node, store, root, state, dag_id)
            state["nodes"][node_id] = saved
            id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
            sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        if isinstance(node, ResolveNode):
            if resume and isinstance(saved, dict) and saved.get("ids") is not None:
                saved["cached"] = True
                id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
                continue
            saved = _execute_resolve_node(node, store, root, dag_id, state)
            state["nodes"][node_id] = saved
            id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
            sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        if isinstance(node, ScoreNode):
            # A score is the thing being tuned, so it is never silently reused:
            # running the graph again is how a lane gets judged twice.
            saved = _execute_score_node(
                node, store, root, dag_dir, dag_id, state,
                gate_ids=[n.id for n in spec.nodes if isinstance(n, GateNode)],
            )
            state["nodes"][node_id] = saved
            id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
            sidecar.write_text(json.dumps(state, indent=2), encoding="utf-8")
            continue
        if isinstance(node, RetrieveNode):
            if resume and isinstance(saved, dict) and saved.get("ids") is not None:
                saved["cached"] = True
                id_sets[node_id] = {str(item) for item in saved.get("ids") or []}
                continue
            saved = _execute_retrieve_node(node, store, root, dag_id, state)
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

"""Unified evaluation and tuning engine for fleet lanes, rungs, filters, and sources.

Provides a single, centralized location to:
1. Diagnose and tune gate filters (kind, size, location, vertical) on any text or domain.
2. Simulate candidate progression through a lane's compiled ladder of rungs.
3. Inspect and verify source URLs, sitemaps, and surface coverage.
4. Audit historical run databases (conversion rates, drop-offs, surface yields, score distribution).
5. Run synthetic filter benchmarks to verify gate precision and recall.
6. Test parameter variations (what-if analysis) on thresholds and vocabularies.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from .discover import discover_sitemap_url, fetch_sitemap_entries
from .enrich import classify_page, load_surfaces, surface_urls
from .gates import (
    DELIVERY_TERMS,
    DIRECTORY_TERMS,
    FETCHED,
    HARD_SOFTWARE_TERMS,
    PRIMARY_SERVICES_TERMS,
    SERVICES_TERMS,
    SNIPPET,
    SOFTWARE_TERMS,
    VERTICAL_GROUPS,
    GateProfile,
    GateResult,
    check_kind,
    check_location,
    check_size,
    check_vertical,
    matches_any,
    read_firmographics,
    read_kind,
    read_location,
    read_size,
    read_verticals,
    run_funnel,
)
from .lanes import shipped_lanes
from .rungs import advances_to, count_rows, gate_rows, lane_spec


# ---------------------------------------------------------------------------
# 1. Gate Diagnostics
# ---------------------------------------------------------------------------

@dataclass
class KindDiagnostic:
    outcome: str
    verdict_kind: str
    reason: str
    software_matches: list[str] = field(default_factory=list)
    hard_software_matches: list[str] = field(default_factory=list)
    services_matches: list[str] = field(default_factory=list)
    primary_services_matches: list[str] = field(default_factory=list)
    directory_matches: list[str] = field(default_factory=list)
    delivery_matches: list[str] = field(default_factory=list)
    dominance_explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SizeDiagnostic:
    outcome: str
    stated_size: int | None
    bounds: tuple[int, int]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "stated_size": self.stated_size,
            "bounds": list(self.bounds),
            "reason": self.reason,
        }


@dataclass
class LocationDiagnostic:
    outcome: str
    stated_location: str
    allowed: list[str]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerticalDiagnostic:
    outcome: str
    found_verticals: list[str]
    wanted: list[str]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GateDiagnosticReport:
    text_length: int
    evidence: str
    kind: KindDiagnostic
    size: SizeDiagnostic
    location: LocationDiagnostic
    vertical: VerticalDiagnostic
    overall_verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "text_length": self.text_length,
            "evidence": self.evidence,
            "kind": self.kind.as_dict(),
            "size": self.size.as_dict(),
            "location": self.location.as_dict(),
            "vertical": self.vertical.as_dict(),
            "overall_verdict": self.overall_verdict,
        }


def diagnose_gates(
    text: str,
    *,
    evidence: str = SNIPPET,
    lane_name: str = "partner",
    profile: dict[str, Any] | None = None,
) -> GateDiagnosticReport:
    """Run full diagnostic breakdown across all four gates on input text."""
    lane = shipped_lanes().get(lane_name)
    prof = GateProfile.from_object(profile if profile is not None else (lane.model_dump() if lane else {}))

    # 1. Kind diagnostic
    soft_matches = matches_any(text, SOFTWARE_TERMS)
    hard_matches = matches_any(text, HARD_SOFTWARE_TERMS)
    serv_matches = matches_any(text, SERVICES_TERMS)
    prim_serv = matches_any(text, PRIMARY_SERVICES_TERMS)
    dir_matches = matches_any(text, DIRECTORY_TERMS)
    del_matches = matches_any(text, DELIVERY_TERMS)

    kind_result = check_kind(text, allows=prof.allows, evidence=evidence)
    v_kind = read_kind(text)

    # Explain dominance rationale
    if dir_matches:
        dom = f"classified as directory due to directory terms: {dir_matches}"
    elif hard_matches and not (len(prim_serv) >= 2 and len(serv_matches) > len(soft_matches)):
        dom = f"hard SaaS terms dominant: {hard_matches}"
    elif soft_matches and not prim_serv:
        dom = f"soft product terms without primary services identity: {soft_matches}"
    elif soft_matches and len(soft_matches) > len(serv_matches):
        dom = f"software terms count ({len(soft_matches)}) exceeds services count ({len(serv_matches)})"
    elif serv_matches:
        if soft_matches:
            dom = f"primary services terms ({len(prim_serv)}) protect against soft product markers {soft_matches}"
        else:
            dom = f"services markers detected: {serv_matches}"
    else:
        dom = "no defining product or services vocabulary found"

    kind_diag = KindDiagnostic(
        outcome=kind_result.outcome,
        verdict_kind=v_kind,
        reason=kind_result.reason,
        software_matches=soft_matches,
        hard_software_matches=hard_matches,
        services_matches=serv_matches,
        primary_services_matches=prim_serv,
        directory_matches=dir_matches,
        delivery_matches=del_matches,
        dominance_explanation=dom,
    )

    # 2. Size diagnostic
    size_num = read_size(text)
    size_result = check_size(size_num, minimum=prof.size_min, maximum=prof.size_max, evidence=evidence)
    size_diag = SizeDiagnostic(
        outcome=size_result.outcome,
        stated_size=size_num,
        bounds=(prof.size_min, prof.size_max),
        reason=size_result.reason,
    )

    # 3. Location diagnostic
    loc_str = read_location(text)
    loc_result = check_location(loc_str, allowed=prof.locations, evidence=evidence)
    loc_diag = LocationDiagnostic(
        outcome=loc_result.outcome,
        stated_location=loc_str,
        allowed=list(prof.locations),
        reason=loc_result.reason,
    )

    # 4. Vertical diagnostic
    verts = read_verticals(text)
    vert_result = check_vertical(verts, wanted=prof.verticals)
    vert_diag = VerticalDiagnostic(
        outcome=vert_result.outcome,
        found_verticals=list(verts),
        wanted=list(prof.verticals),
        reason=vert_result.reason,
    )

    # Overall verdict
    outcomes = [kind_result.outcome, size_result.outcome, loc_result.outcome, vert_result.outcome]
    if "fail" in outcomes:
        overall = "eliminated"
    elif all(o == "pass" for o in outcomes):
        overall = "qualified"
    else:
        overall = "lead"

    return GateDiagnosticReport(
        text_length=len(text or ""),
        evidence=evidence,
        kind=kind_diag,
        size=size_diag,
        location=loc_diag,
        vertical=vert_diag,
        overall_verdict=overall,
    )


@dataclass
class BatchItemResult:
    entity: str
    text: str
    overall_verdict: str
    failed_gate: str | None
    dominant_kind: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchDiagnosticReport:
    total: int
    qualified: int
    lead: int
    eliminated: int
    pass_rate: float
    items: list[BatchItemResult]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "qualified": self.qualified,
            "lead": self.lead,
            "eliminated": self.eliminated,
            "pass_rate": self.pass_rate,
            "items": [item.as_dict() for item in self.items],
        }


def load_batch_file(path: str | Path) -> list[dict[str, Any]]:
    """Load batch candidate items from a JSON, JSONL, or CSV file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"batch file not found: {p}")
    suffix = p.suffix.lower()
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []

    items: list[dict[str, Any]] = []
    if suffix == ".csv":
        import csv
        reader = csv.DictReader(text.splitlines())
        for row in reader:
            items.append(dict(row))
    elif suffix in (".jsonl", ".ndjson") or not text.startswith("["):
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            items.append(json.loads(line))
    else:
        items = json.loads(text)
    return items


def diagnose_gates_batch(
    items: Sequence[dict[str, Any]],
    *,
    evidence: str = SNIPPET,
    lane_name: str = "partner",
    profile: dict[str, Any] | None = None,
) -> BatchDiagnosticReport:
    """Diagnose multiple candidate items in batch."""
    results: list[BatchItemResult] = []
    qualified_cnt = lead_cnt = elim_cnt = 0

    for idx, item in enumerate(items, 1):
        entity = str(item.get("entity") or item.get("entity_id") or item.get("candidate") or item.get("id") or f"item_{idx}")
        text = str(item.get("text") or item.get("snippet") or item.get("page") or "")
        item_evidence = str(item.get("evidence") or evidence)

        diag = diagnose_gates(text, evidence=item_evidence, lane_name=lane_name, profile=profile)
        verdict = diag.overall_verdict
        if verdict == "qualified":
            qualified_cnt += 1
        elif verdict == "lead":
            lead_cnt += 1
        else:
            elim_cnt += 1

        failed_gate = None
        for g_name in ("kind", "size", "location", "vertical"):
            g_diag = getattr(diag, g_name)
            if g_diag.outcome == "fail":
                failed_gate = g_name
                break

        reason = "Passed all live gates"
        if failed_gate:
            reason = getattr(diag, failed_gate).reason

        results.append(BatchItemResult(
            entity=entity,
            text=text,
            overall_verdict=verdict,
            failed_gate=failed_gate,
            dominant_kind=diag.kind.verdict_kind,
            reason=reason,
        ))

    total = len(items)
    passed = qualified_cnt + lead_cnt
    pass_rate = (passed / total) if total > 0 else 0.0

    return BatchDiagnosticReport(
        total=total,
        qualified=qualified_cnt,
        lead=lead_cnt,
        eliminated=elim_cnt,
        pass_rate=pass_rate,
        items=results,
    )


# ---------------------------------------------------------------------------
# 2. Candidate Funnel Simulation
# ---------------------------------------------------------------------------

@dataclass
class SimStep:
    rung: str
    evidence: str
    outcome: str
    reason: str
    gates: list[dict[str, str]]
    advances: bool


@dataclass
class SimReport:
    candidate: str
    lane: str
    steps: list[SimStep]
    final_verdict: str
    standing: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "lane": self.lane,
            "steps": [asdict(s) for s in self.steps],
            "final_verdict": self.final_verdict,
            "standing": self.standing,
        }


def simulate_candidate(
    candidate: str,
    *,
    snippet: str = "",
    page_text: str = "",
    stories_text: str = "",
    rung_texts: dict[str, str] | None = None,
    lane_name: str = "partner",
    profile: dict[str, Any] | None = None,
) -> SimReport:
    """Simulate a candidate stepping through a lane's compiled ladder of rungs."""
    lane = shipped_lanes().get(lane_name)
    if not lane:
        raise ValueError(f"unknown lane '{lane_name}' (available: {list(shipped_lanes())})")

    merged_profile = lane.model_dump()
    if profile:
        merged_profile.update(profile)
        if "funnel" in merged_profile and isinstance(merged_profile["funnel"], dict):
            merged_profile["funnel"].update(profile)

    rungs = lane.funnel.rungs() if lane.funnel else []
    steps: list[SimStep] = []
    standing = True
    final_verdict = "lead"
    accumulated_surfaces: set[str] = set()

    for idx, rung in enumerate(rungs):
        next_rung = rungs[idx + 1] if idx + 1 < len(rungs) else None

        if rung_texts and rung.name in rung_texts:
            text = rung_texts[rung.name]
        elif rung.name == "stories" and stories_text:
            text = f"{snippet}\n{stories_text}".strip()
        elif rung.evidence == SNIPPET:
            text = snippet
        else:
            text = f"{snippet}\n{page_text}".strip()

        report = run_funnel(candidate, snippet=text, profile=merged_profile, evidence=rung.evidence)
        gates_summary = [{"gate": g.gate, "outcome": g.outcome, "reason": g.reason} for g in report.results]

        adv = advances_to(report, next_rung, read_surfaces=accumulated_surfaces) if next_rung else False

        steps.append(SimStep(
            rung=rung.name,
            evidence=rung.evidence,
            outcome=report.verdict,
            reason=report.because(),
            gates=gates_summary,
            advances=adv,
        ))

        accumulated_surfaces.update(rung.surfaces)

        if report.eliminated:
            standing = False
            final_verdict = "eliminated"
            break

        final_verdict = report.verdict

        if not adv:
            break

    return SimReport(
        candidate=candidate,
        lane=lane_name,
        steps=steps,
        final_verdict=final_verdict,
        standing=standing,
    )


# ---------------------------------------------------------------------------
# 3. Source & Surface Evaluator
# ---------------------------------------------------------------------------

@dataclass
class SurfaceDiagnostic:
    target: str
    is_domain: bool
    classified_surface: str | None = None
    sitemap_found: str | None = None
    discovered_surfaces: dict[str, int] = field(default_factory=dict)
    missing_surfaces: list[str] = field(default_factory=list)
    fallback_urls: dict[str, list[str]] = field(default_factory=dict)
    skips: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_surface(
    target: str,
    *,
    lane_name: str = "partner",
    timeout: float = 15.0,
    probe_network: bool = False,
) -> SurfaceDiagnostic:
    """Inspect how a URL is classified or probe a domain's surface coverage."""
    plan = load_surfaces()
    lane = shipped_lanes().get(lane_name)
    needed_surfaces: set[str] = set()
    if lane and lane.funnel:
        for rung in lane.funnel.rungs():
            needed_surfaces |= set(rung.surfaces)

    # Check if target is a path or full URL vs bare domain
    is_url = "://" in target or "/" in target.strip("/")
    if is_url:
        classified = classify_page(target, surfaces=plan)
        return SurfaceDiagnostic(
            target=target,
            is_domain=False,
            classified_surface=classified,
        )

    # Domain evaluation
    domain = target.strip().lower()
    fallback: dict[str, list[str]] = {}
    for s in needed_surfaces:
        urls = surface_urls(s, domain, surfaces=plan)
        if urls:
            fallback[s] = urls

    sitemap_url = None
    discovered: dict[str, int] = {}
    skips: list[dict[str, str]] = []

    if probe_network:
        try:
            sitemap_url = discover_sitemap_url(f"https://{domain}", timeout=timeout)
            if sitemap_url:
                entries = fetch_sitemap_entries(sitemap_url, timeout=timeout, max_urls=200)
                for u, _ in entries:
                    surf = classify_page(u, surfaces=plan)
                    if surf:
                        discovered[surf] = discovered.get(surf, 0) + 1
        except Exception as exc:
            skips.append({"surface": "sitemap", "url": f"https://{domain}", "reason": str(exc)})

    missing = sorted(s for s in needed_surfaces if s not in discovered)

    return SurfaceDiagnostic(
        target=domain,
        is_domain=True,
        sitemap_found=sitemap_url,
        discovered_surfaces=discovered,
        missing_surfaces=missing,
        fallback_urls=fallback,
        skips=skips,
    )


# ---------------------------------------------------------------------------
# 4. Run Quality Auditor
# ---------------------------------------------------------------------------

def audit_database(db: str | Path) -> dict[str, Any]:
    """Audit a run database: node progression, drop-offs, surface yield, scores."""
    db_path = Path(db)
    if db_path.is_dir():
        db_path = db_path / "harness-fleet.db"

    out: dict[str, Any] = {
        "database": str(db_path),
        "exists": False,
        "runs": 0,
        "rung_rows": 0,
        "unique_entities": 0,
        "lanes": {},
    }

    if not db_path.exists():
        out["error"] = f"database does not exist: {db_path}"
        return out

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        out["error"] = str(exc)
        return out

    with conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "rung_rows" not in tables:
            out["error"] = "no rung_rows table in database"
            return out

        # The totals the human-readable report leads with. They live here rather
        # than in the formatter because the formatter has no database to ask: it
        # read keys this function never produced, so it reported "no records"
        # for every database — including ones full of rows — and the text half
        # of this command was unreachable.
        out["exists"] = True
        out["rung_rows"] = int(
            conn.execute("SELECT count(*) FROM rung_rows").fetchone()[0]
        )
        out["unique_entities"] = int(
            conn.execute("SELECT count(DISTINCT item_id) FROM rung_rows").fetchone()[0]
        )
        if "runs" in tables:
            out["runs"] = int(conn.execute("SELECT count(*) FROM runs").fetchone()[0])
        else:
            out["runs"] = int(
                conn.execute("SELECT count(DISTINCT dag_id) FROM rung_rows").fetchone()[0]
            )

        nodes_by_lane: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {
            "rows": 0, "outcomes": defaultdict(int), "advanced": 0, "eliminated": 0,
            "scored": 0, "scores": [], "surfaces": defaultdict(int),
        }))

        for row in conn.execute("SELECT lane, node_id, rung, outcome, advances, score, surfaces FROM rung_rows"):
            lane = str(row["lane"] or "unknown")
            node = nodes_by_lane[lane][str(row["node_id"])]
            node["rows"] += 1
            node["outcomes"][str(row["outcome"] or "-")] += 1
            if str(row["outcome"] or "") == "eliminated":
                node["eliminated"] += 1
            if row["advances"]:
                node["advanced"] += 1
            if row["score"]:
                node["scored"] += 1
                try:
                    node["scores"].append(float(row["score"]))
                except (ValueError, TypeError):
                    pass
            try:
                surfs = json.loads(row["surfaces"] or "{}")
                if isinstance(surfs, dict):
                    for s, count in surfs.items():
                        node["surfaces"][s] += int(count or 0)
            except Exception:
                pass

        for lane, nodes in nodes_by_lane.items():
            lane_summary: dict[str, Any] = {"nodes": []}
            total_scored = 0
            all_scores: list[float] = []
            for node_id, n in sorted(nodes.items()):
                total_scored += n["scored"]
                all_scores.extend(n["scores"])
                lane_summary["nodes"].append({
                    "node": node_id,
                    "rows": n["rows"],
                    "eliminated": n["eliminated"],
                    "advanced": n["advanced"],
                    "outcomes": dict(n["outcomes"]),
                    "surfaces": dict(n["surfaces"]),
                    "scores_recorded": n["scored"],
                })
            lane_summary["total_scored"] = total_scored
            lane_summary["mean_score"] = (sum(all_scores) / len(all_scores)) if all_scores else 0.0
            out["lanes"][lane] = lane_summary

    return out


# ---------------------------------------------------------------------------
# 5. Synthetic Filter Benchmark
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkItem:
    entity_id: str
    expected_kind: str  # "services", "software", "directory", "irrelevant"
    text: str


BENCHMARK_CORPUS: list[BenchmarkItem] = [
    # Positive Integrators / Consultancies
    BenchmarkItem(
        "thoughtworks",
        "services",
        "Thoughtworks is a leading global technology consultancy that integrates strategy, design "
        "and software engineering to drive digital transformation for our clients.",
    ),
    BenchmarkItem(
        "royalcyber",
        "services",
        "Royal Cyber is an IT consulting and systems integrator. Our platform engineering team delivers "
        "cloud migrations and data platforms. Book a demo of our commerce accelerator with our consultants.",
    ),
    BenchmarkItem(
        "ness",
        "services",
        "Ness delivers Confluent implementation for Michelin. Elite Confluent partner. "
        "Our certified engineers run the platform for clients worldwide.",
    ),
    BenchmarkItem(
        "slalom",
        "services",
        "Slalom is a next-generation professional services company focused on strategy, technology, "
        "and business transformation, partnering with AWS, Snowflake, and Salesforce.",
    ),
    # Software / SaaS Vendors
    BenchmarkItem(
        "snowflake",
        "software",
        "Snowflake Data Cloud. Sign up free. Start your free trial. Pricing plans per capacity. Book a demo.",
    ),
    BenchmarkItem(
        "zap_cloud",
        "software",
        "Zap Cloud is a SaaS product for marketing automation. Book a demo of our platform and see "
        "pricing plans for every team.",
    ),
    BenchmarkItem(
        "datadog",
        "software",
        "Datadog cloud monitoring as a service. Free trial, per host per month pricing plans. Download the app.",
    ),
    # Directories
    BenchmarkItem(
        "integratorguide",
        "directory",
        "Search for consulting firms by name, location. Directory of certified system integrators. Claim this profile.",
    ),
    BenchmarkItem(
        "clutch",
        "directory",
        "Directory of top B2B service providers. Compare the best IT companies. Browse by industry. Submit your listing.",
    ),
    # Noise / Irrelevant
    BenchmarkItem(
        "therapist",
        "irrelevant",
        "Individual counselling and psychotherapy for anxiety and depression. Free consultation. Insurance accepted.",
    ),
]


@dataclass
class BenchmarkResult:
    total: int
    passed_expected: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    matrix: dict[str, int]  # TP, FP, TN, FN for services
    failures: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_benchmark_corpus(path: str | Path) -> list[BenchmarkItem]:
    """Load benchmark corpus from a JSON or JSONL file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"benchmark corpus file not found: {p}")
    items: list[BenchmarkItem] = []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        for obj in data:
            entity = obj.get("entity") or obj.get("entity_id") or "test"
            expected = obj.get("expected") or obj.get("expected_kind") or "services"
            txt = obj.get("text") or obj.get("snippet") or ""
            items.append(BenchmarkItem(entity_id=entity, expected_kind=expected, text=txt))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            entity = obj.get("entity") or obj.get("entity_id") or "test"
            expected = obj.get("expected") or obj.get("expected_kind") or "services"
            txt = obj.get("text") or obj.get("snippet") or ""
            items.append(BenchmarkItem(entity_id=entity, expected_kind=expected, text=txt))
    return items


def run_benchmark(
    corpus: Sequence[BenchmarkItem] | None = None,
    *,
    corpus_path: str | Path | None = None,
) -> BenchmarkResult:
    """Run benchmark corpus against current kind gate logic."""
    if corpus_path is not None:
        test_corpus = load_benchmark_corpus(corpus_path)
    elif corpus is not None:
        test_corpus = list(corpus)
    else:
        test_corpus = BENCHMARK_CORPUS

    tp = fp = tn = fn = 0
    failures: list[dict[str, str]] = []

    for item in test_corpus:
        result = check_kind(item.text, allows="services", evidence=FETCHED)
        is_services = (result.outcome == "pass")

        expected_positive = (item.expected_kind == "services")

        if is_services and expected_positive:
            tp += 1
        elif is_services and not expected_positive:
            fp += 1
            failures.append({
                "entity": item.entity_id,
                "expected": item.expected_kind,
                "actual": "services (pass)",
                "reason": result.reason,
            })
        elif not is_services and not expected_positive:
            tn += 1
        elif not is_services and expected_positive:
            fn += 1
            failures.append({
                "entity": item.entity_id,
                "expected": item.expected_kind,
                "actual": f"{result.outcome} ({result.reason})",
                "reason": result.reason,
            })

    total = len(test_corpus)
    correct = tp + tn
    accuracy = correct / total if total else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return BenchmarkResult(
        total=total,
        passed_expected=correct,
        accuracy=accuracy,
        precision=precision,
        recall=recall,
        f1=f1,
        matrix={"TP": tp, "FP": fp, "TN": tn, "FN": fn},
        failures=failures,
    )


# ---------------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------------

def format_gate_report(report: GateDiagnosticReport) -> str:
    """Format gate diagnostic report for clean terminal display."""
    lines = [
        f"=== Gate Diagnostic Report (evidence: {report.evidence}, text: {report.text_length} chars) ===",
        f"Overall Verdict: {report.overall_verdict.upper()}",
        "",
        f"[1] KIND GATE: {report.kind.outcome.upper()}",
        f"    Verdict Kind : {report.kind.verdict_kind or '(none)'}",
        f"    Reason       : {report.kind.reason}",
        f"    Dominance    : {report.kind.dominance_explanation}",
        f"    Software     : {report.kind.software_matches or '[]'}",
        f"    Hard SaaS    : {report.kind.hard_software_matches or '[]'}",
        f"    Services     : {report.kind.services_matches or '[]'}",
        f"    Primary Svc  : {report.kind.primary_services_matches or '[]'}",
        f"    Delivery     : {report.kind.delivery_matches[:5] or '[]'}",
        f"    Directory    : {report.kind.directory_matches or '[]'}",
        "",
        f"[2] SIZE GATE: {report.size.outcome.upper()}",
        f"    Parsed Size  : {report.size.stated_size if report.size.stated_size is not None else 'None'}",
        f"    Bounds [min,max] : {report.size.bounds}",
        f"    Reason       : {report.size.reason}",
        "",
        f"[3] LOCATION GATE: {report.location.outcome.upper()}",
        f"    Parsed Place : {report.location.stated_location or 'None'}",
        f"    Target Place : {report.location.allowed}",
        f"    Reason       : {report.location.reason}",
        "",
        f"[4] VERTICAL GATE: {report.vertical.outcome.upper()}",
        f"    Parsed Verts : {report.vertical.found_verticals or '[]'}",
        f"    Wanted Verts : {report.vertical.wanted}",
        f"    Reason       : {report.vertical.reason}",
    ]
    return "\n".join(lines)


def format_benchmark_result(res: BenchmarkResult) -> str:
    """Format synthetic benchmark results for terminal display."""
    lines = [
        "=== Synthetic Gate Benchmark ===",
        f"Corpus Size : {res.total} test cases",
        f"Accuracy    : {res.accuracy * 100:.1f}% ({res.passed_expected}/{res.total})",
        f"Precision   : {res.precision * 100:.1f}%",
        f"Recall      : {res.recall * 100:.1f}%",
        f"F1 Score    : {res.f1 * 100:.1f}%",
        f"Matrix      : TP={res.matrix['TP']}  FP={res.matrix['FP']}  TN={res.matrix['TN']}  FN={res.matrix['FN']}",
    ]
    if res.failures:
        lines.append("")
        lines.append("Failures:")
        for f in res.failures:
            lines.append(f"  - {f['entity']}: expected {f['expected']}, got {f['actual']}")
    else:
        lines.append("All test entities classified with 100% precision and recall.")
    return "\n".join(lines)


def format_sim_report(sim: SimReport) -> str:
    """Format funnel simulation report for terminal display."""
    lines = [
        f"=== Funnel Simulation: {sim.candidate} in lane '{sim.lane}' ===",
        f"Final Verdict : {sim.final_verdict.upper()} (standing: {sim.standing})",
        "",
        "Ladder Progression:",
    ]
    for idx, s in enumerate(sim.steps, 1):
        status_icon = "✓" if s.outcome in ("qualified", "lead") else "✗"
        lines.append(f"  Step {idx} [Rung: {s.rung} | {s.evidence}] -> {status_icon} {s.outcome.upper()}")
        if s.reason:
            lines.append(f"    Reason   : {s.reason}")
        if s.gates:
            gate_strs = [f"{g['gate']}={g['outcome']}" for g in s.gates]
            lines.append(f"    Gates    : {', '.join(gate_strs)}")
        lines.append(f"    Advances : {s.advances}")
    return "\n".join(lines)


def format_surface_report(surf: SurfaceDiagnostic) -> str:
    """Format surface evaluation for terminal display."""
    if not surf.is_domain:
        return (
            f"Target URL: {surf.target}\n"
            f"Classified Surface: {surf.classified_surface or '(unclassified)'}"
        )
    lines = [
        f"=== Domain Surface Evaluation: {surf.target} ===",
        f"Sitemap Found : {surf.sitemap_found or 'None'}",
    ]
    if surf.discovered_surfaces:
        lines.append("Discovered Surfaces via Sitemap:")
        for s, count in sorted(surf.discovered_surfaces.items()):
            lines.append(f"  - {s:15}: {count} URL(s)")
    if surf.missing_surfaces:
        lines.append("Missing Surfaces (Will probe fallback paths):")
        for s in surf.missing_surfaces:
            paths = surf.fallback_urls.get(s, [])
            lines.append(f"  - {s:15}: fallback paths -> {paths}")
    return "\n".join(lines)


def format_batch_gate_report(report: BatchDiagnosticReport) -> str:
    """Format batch diagnostic report for clean terminal display."""
    lines = [
        "=== Batch Gate Diagnostic Report ===",
        f"Total Candidates : {report.total}",
        f"Passed           : {report.qualified + report.lead} ({report.pass_rate * 100:.1f}%) [qualified={report.qualified}, lead={report.lead}]",
        f"Eliminated       : {report.eliminated} ({(1.0 - report.pass_rate) * 100:.1f}%)",
        "",
        f"{'Entity':<24} {'Verdict':<12} {'Failed Gate':<12} {'Reason'}",
        "-" * 80,
    ]
    for it in report.items:
        failed = it.failed_gate or "-"
        lines.append(f"{it.entity[:23]:<24} {it.overall_verdict:<12} {failed:<12} {it.reason[:40]}")
    return "\n".join(lines)


def format_audit_report(aud: dict[str, Any]) -> str:
    """Format run database audit for clean terminal display."""
    lines = [
        f"=== Run Database Audit: {aud.get('database', '')} ===",
    ]
    if not aud.get("exists", False):
        lines.append("Database does not exist or contains no records.")
        return "\n".join(lines)

    lines.append(
        f"Runs: {aud.get('runs', 0)} | "
        f"Total Rung Rows: {aud.get('rung_rows', 0)} | "
        f"Unique Entities: {aud.get('unique_entities', 0)}"
    )
    lines.append("")

    lanes = aud.get("lanes", {})
    if not lanes:
        lines.append("No lane execution data found in database.")
        return "\n".join(lines)

    for lane_name, lane_data in sorted(lanes.items()):
        scored = lane_data.get("total_scored", 0)
        mean_score = lane_data.get("mean_score", 0.0)
        lines.append(f"Lane: {lane_name.upper()} (Scored Entities: {scored}, Mean Score: {mean_score:.1f})")
        lines.append("  " + f"{'Node':<20} {'Rows':>6} {'Advanced':>9} {'Elim':>6} {'Drop%':>7}  Outcomes")
        lines.append("  " + "-" * 70)

        all_surfaces: dict[str, int] = defaultdict(int)
        for node in lane_data.get("nodes", []):
            node_name = node.get("node", "")
            rows = node.get("rows", 0)
            advanced = node.get("advanced", 0)
            elim = node.get("eliminated", 0)
            drop_pct = (elim / rows * 100) if rows > 0 else 0.0

            outcomes_dict = node.get("outcomes", {})
            outcomes_str = ", ".join(f"{k}:{v}" for k, v in sorted(outcomes_dict.items())) or "none"

            lines.append(
                f"  {node_name:<20} {rows:>6} {advanced:>9} {elim:>6} {drop_pct:>6.1f}%  {outcomes_str}"
            )
            for s, c in node.get("surfaces", {}).items():
                all_surfaces[s] += c

        if all_surfaces:
            lines.append("  Surface Yields:")
            for s, c in sorted(all_surfaces.items()):
                lines.append(f"    - {s:15}: {c} page(s)")
        lines.append("")

    return "\n".join(lines).rstrip()


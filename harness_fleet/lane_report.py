"""The five measurements a lane is tuned by, computed from a finished run.

Tuning without measurement is guessing, so this module is deliberately
read-only: it observes what a run produced and never changes a score, a claim
requirement or a tier. The measurements are the ones the tuning plan names:

* **yield** — what each source actually returned, including the sources that
  returned nothing and why;
* **coverage** — which evidence kinds each entity carries, and how many clear
  the lane's bar;
* **support quality** — which scored claims had a qualifying source and which
  were refused, with the engine's own reasons;
* **truth sample** — a deterministic sample whose quotes are re-checked for
  character-exactness, for still being on the live page, and for actually
  addressing the claim they carry;
* **cost and time** — routes, attempts and wall time.

Fetching is injectable so a caller (or a test) can supply pages without the
network, and a report can be **frozen**: the run's exact input, the registry and
the lane are copied aside so two configurations can be compared on identical
inputs instead of on whatever the web happened to serve that minute.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import contracts, evidence
from .lanes import Lane
from .models import (
    LaneCost,
    LaneCoverage,
    LaneReport,
    LaneSupport,
    LaneTruthQuote,
    LaneYield,
)

DEFAULT_SAMPLE = 5
#: Fixed so two configs sample the same entities: the plan requires comparison
#: on identical inputs, and a per-run random sample would silently break that.
DEFAULT_SEED = 20260101


def _load_input_items(path: str | Path | None) -> dict[str, Any]:
    """The run's own input, by item id: text and metadata."""
    if not path:
        return {}
    from .input_data import iter_input_items

    target = Path(str(path))
    if not target.is_file():
        return {}
    items: dict[str, Any] = {}
    try:
        for item in iter_input_items(target):
            items[str(item.item_id)] = item
    except Exception:
        return {}
    return items


def _section_for_offset(text: str, offset: int) -> tuple[str, str]:
    """The (category, uri) of the section a quote offset falls in."""
    marks = list(evidence.SECTION_RE.finditer(text or ""))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        if mark.end() <= offset < end:
            return mark.group("category").strip().upper(), mark.group("uri").strip()
    return "", ""


def _yield_measurement(
    snapshot: dict[str, Any],
    items: dict[str, Any],
    *,
    run_id: str,
    runs_dir: Path,
    channel_names: set[str],
) -> tuple[list[LaneYield], list[str]]:
    """Captured per source, plus attempted/skipped when the run recorded them."""
    notes: list[str] = []
    captured: dict[str, int] = {}
    for item in items.values():
        metadata = getattr(item, "metadata", None) or {}
        source = str(
            metadata.get("discovery_backend")
            or metadata.get("backend")
            or metadata.get("source")
            or "unknown"
        )
        captured[source] = captured.get(source, 0) + 1

    attempted: dict[str, int | None] = {}
    skipped: dict[str, int] = {}
    sidecar = runs_dir / str(run_id) / "discovery_report.json"
    if sidecar.is_file():
        try:
            report = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
        quality = (report.get("source_quality") or {}) if isinstance(report, dict) else {}
        backends = (quality.get("backends") or {}) if isinstance(quality, dict) else {}
        for name, metrics in backends.items():
            attempted[str(name)] = int(metrics.get("unique_hits") or metrics.get("hits") or 0)
            skipped[str(name)] = int(metrics.get("skipped") or 0)
        reasons: list[str] = []
        for entry in report.get("skipped") or []:
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("backend") or entry.get("source") or "")
            if source and source not in attempted:
                skipped[source] = skipped.get(source, 0) + 1
            # A source that returned nothing says why: a skip count with no
            # reason is the kind of silent zero this fleet does not allow.
            reason = str(entry.get("reason") or "").strip()
            if reason:
                reasons.append(f"{source or 'source'}: {reason[:160]}")
        if reasons:
            notes.append("skipped sources: " + "; ".join(reasons[:5]))
        retried = [entry for entry in (report.get("retried") or []) if isinstance(entry, dict)]
        if retried:
            # A source that only answered after a retry is a fact about this
            # run's breadth: the yield below came from a surface that was not
            # answering first time, and the next run may not be so lucky.
            notes.append(
                "sources that answered only after a retry: "
                + "; ".join(
                    f"{entry.get('backend') or 'source'} took {entry.get('attempts')} attempts"
                    for entry in retried[:5]
                )
            )
    else:
        notes.append(
            "the run did not record a discovery report, so attempted counts are unknown "
            "and only captured counts are shown"
        )

    entries: list[LaneYield] = []
    for source in sorted(set(captured) | set(attempted)):
        entries.append(LaneYield(
            source=source,
            kind="channel" if source in channel_names else "backend",
            captured=int(captured.get(source, 0)),
            attempted=attempted.get(source),
            skipped=int(skipped.get(source, 0)),
        ))
    return entries, notes


def _coverage_measurement(
    records: list[Any], items: dict[str, Any], *, bar_kinds: tuple[str, ...]
) -> tuple[list[LaneCoverage], float, dict[str, int]]:
    """Kinds per entity, the bar each clears, and the kinds seen overall."""
    entries: list[LaneCoverage] = []
    meeting = 0
    kind_totals: dict[str, int] = {}
    for record in records:
        item_id = str(record.item_id)
        item = items.get(item_id)
        text = (getattr(item, "text", "") or "") if item else ""
        uri = str(getattr(record, "source_uri", "") or "")
        kinds = evidence.coverage(text, uri)
        present = sorted(kind for kind, ok in kinds.items() if ok and not kind.startswith("_"))
        claims = record.claims if isinstance(record.claims, dict) else {}
        claimed_tier = claims.get("fit_tier") if isinstance(claims.get("fit_tier"), str) else None
        # The lane's bar decides compliance; without a lane the entity is judged
        # against the tier it claims, which is the honest default.
        required = bar_kinds or (
            contracts.TIER_MINIMUMS.get(claimed_tier, ()) if claimed_tier else ()
        )
        missing = [kind for kind in required if not kinds.get(kind)]
        if not missing:
            meeting += 1
        for kind in present:
            kind_totals[kind] = kind_totals.get(kind, 0) + 1
        entries.append(LaneCoverage(
            item_id=item_id, kinds=present, missing=missing, claimed_tier=claimed_tier
        ))
    share = (meeting / len(records)) if records else 0.0
    return entries, round(share, 4), kind_totals


def _support_measurement(records: list[Any], items: dict[str, Any], task: Any) -> tuple[list[LaneSupport], int, int]:
    """Which scored claims had a qualifying source, and why the rest did not.

    The reasons come from the engine's own support check, so this cannot drift
    from what scoring did: an item with no strength and no note is reported as
    having no supporting quote at all.
    """
    entries: list[LaneSupport] = []
    supported_total = 0
    refused_total = 0
    for record in records:
        item_id = str(record.item_id)
        item = items.get(item_id)
        text = (getattr(item, "text", "") or "") if item else ""
        uri = str(getattr(record, "source_uri", "") or "")
        quotes = list(getattr(record, "quotes", []) or [])
        claims = record.claims if isinstance(record.claims, dict) else {}
        raw_answers = claims.get("checklist")
        answers: dict[str, Any] = raw_answers if isinstance(raw_answers, dict) else {}
        true_items = [name for name, value in answers.items() if value is True]
        strengths = task.support_strengths(
            quotes, uri, getattr(record, "captured_at", None), getattr(record, "scored_at", None), text
        )
        notes = task.support_notes(quotes, text, uri) if true_items else {}
        supported = sorted(name for name in true_items if strengths.get(name, 0.0) > 0)
        refused = sorted(name for name in true_items if name not in supported)
        reasons = [
            f"{name}: {notes.get(name) or 'no quote supports this claim'}" for name in refused
        ]
        supported_total += len(supported)
        refused_total += len(refused)
        entries.append(LaneSupport(
            item_id=item_id, supported=supported, refused=refused, reasons=reasons
        ))
    return entries, supported_total, refused_total


def _default_fetch(url: str) -> str | None:
    """Fetch a page the way the fleet's own discovery does."""
    try:
        import httpx

        from .discover import USER_AGENT, extract_text

        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url)
            if response.status_code != 200:
                return None
            return extract_text(response.text)
    except Exception:
        return None


def _normalized(value: str) -> str:
    return " ".join((value or "").split())


def _truth_measurement(
    records: list[Any],
    items: dict[str, Any],
    task: Any,
    *,
    sample: int,
    seed: int,
    fetch: Callable[[str], str | None],
) -> tuple[list[LaneTruthQuote], int]:
    """Re-check a deterministic sample: exact offsets, live page, addressing."""
    ordered = sorted(records, key=lambda record: str(record.item_id))
    chosen = (
        random.Random(seed).sample(ordered, min(sample, len(ordered))) if ordered and sample > 0 else []
    )
    findings: list[LaneTruthQuote] = []
    for record in chosen:
        item_id = str(record.item_id)
        item = items.get(item_id)
        text = (getattr(item, "text", "") or "") if item else ""
        for quote in list(getattr(record, "quotes", []) or []):
            entry = quote if isinstance(quote, dict) else quote.model_dump(mode="json")
            quote_text = str(entry.get("text") or "")
            start = int(entry.get("start") or 0)
            end = int(entry.get("end") or 0)
            category, uri = _section_for_offset(text, start)
            # A plain document has no section markers, so its source is the
            # record's own URI — the same fallback the engine's support check
            # uses, which keeps the truth sample and scoring in agreement.
            if not uri:
                uri = str(getattr(record, "source_uri", "") or "")
            if not category:
                category = evidence.classify_source_category(uri).upper() if uri else ""
            exact = bool(text) and text[start:end] == quote_text
            addressed, reason = contracts.quote_addresses_claim(
                str((entry.get("supports") or [""])[0]), quote_text, category, task.evidence_terms
            )
            live = "not_checked"
            if uri.startswith("http"):
                page = fetch(uri)
                if page is None:
                    live = "unreachable"
                elif _normalized(quote_text) and _normalized(quote_text) in _normalized(page):
                    live = "confirmed"
                else:
                    live = "missing"
            findings.append(LaneTruthQuote(
                item_id=item_id,
                uri=uri,
                category=category,
                exact=exact,
                live=live,
                addressed=addressed,
                reason="" if addressed else reason,
            ))
    return findings, len(chosen)


def _cost_measurement(snapshot: dict[str, Any]) -> LaneCost:
    receipts = [r for r in (snapshot.get("model_runs") or []) if isinstance(r, dict)]
    routes = sorted({
        str(r.get("requested_route") or r.get("route_id") or "")
        for r in receipts
        if (r.get("requested_route") or r.get("route_id"))
    })
    costs = [float(r["cost"]) for r in receipts if isinstance(r.get("cost"), (int, float))]
    batches = [b for b in (snapshot.get("batches") or {}).values() if isinstance(b, dict)]
    wall: float | None = None
    try:
        created = datetime.fromisoformat(str(snapshot.get("created_at")))
        finished = snapshot.get("finished_at")
        if finished:
            wall = round((datetime.fromisoformat(str(finished)) - created).total_seconds(), 3)
    except (TypeError, ValueError):
        wall = None
    return LaneCost(
        routes=routes,
        attempts=int(snapshot.get("attempts_used") or 0),
        cost=round(sum(costs), 6),
        wall_seconds=wall,
        batches_verified=sum(1 for b in batches if b.get("status") == "verified"),
        batches_failed=sum(1 for b in batches if b.get("status") == "failed"),
    )


def _freeze(
    *,
    freeze_dir: Path,
    snapshot: dict[str, Any],
    workspace_root: Path,
    lane: Lane | None,
    sampled: list[str],
) -> dict[str, str]:
    """Copy the exact inputs aside so two configs can be compared honestly."""
    freeze_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    input_path = snapshot.get("input_path")
    if input_path and Path(str(input_path)).is_file():
        source = Path(str(input_path))
        target = freeze_dir / f"frozen_input{source.suffix or '.csv'}"
        shutil.copy2(source, target)
        copied["input"] = str(target)
        copied["input_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    registry = workspace_root / "source_registry.json"
    if registry.is_file():
        target = freeze_dir / "source_registry.json"
        shutil.copy2(registry, target)
        copied["registry"] = str(target)
    if lane is not None:
        lane_path = workspace_root / "lanes" / f"{lane.name}.json"
        if lane_path.is_file():
            target = freeze_dir / "lane.json"
            shutil.copy2(lane_path, target)
            copied["lane"] = str(target)
        copied["lane_name"] = lane.name
        copied["lane_revision"] = str(lane.revision)
    manifest = {
        "run_id": snapshot.get("run_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sampled_item_ids": sampled,
        **copied,
    }
    (freeze_dir / "frozen_sample.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    copied["manifest"] = str(freeze_dir / "frozen_sample.json")
    return copied


def build_lane_report(
    store: Any,
    run_id: str,
    *,
    workspace_root: str | Path = ".",
    lane: Lane | None = None,
    sample: int = DEFAULT_SAMPLE,
    seed: int = DEFAULT_SEED,
    fetch: Callable[[str], str | None] | None = None,
    freeze_dir: str | Path | None = None,
) -> LaneReport:
    """The five measurements for one finished run."""
    from .channels import load_channels
    from .export import verified_records_from_snapshot

    workspace = Path(workspace_root).expanduser().resolve()
    runs_dir = workspace / "runs"
    snapshot = store.run_snapshot(run_id)
    records, task = verified_records_from_snapshot(snapshot)
    items = _load_input_items(snapshot.get("input_path"))
    try:
        channel_names = set(load_channels(workspace))
    except Exception:
        channel_names = set()

    bar_kinds: tuple[str, ...] = ()
    if lane is not None:
        bar_kinds = tuple(lane.require_kinds) or tuple(contracts.TIER_MINIMUMS.get(lane.tier, ()))

    yield_entries, notes = _yield_measurement(
        snapshot, items, run_id=run_id, runs_dir=runs_dir, channel_names=channel_names
    )
    coverage_entries, meeting_share, _kind_totals = _coverage_measurement(
        records, items, bar_kinds=bar_kinds
    )
    support_entries, supported_total, refused_total = _support_measurement(records, items, task)
    truth, sampled_count = _truth_measurement(
        records, items, task, sample=sample, seed=seed, fetch=fetch or _default_fetch
    )
    cost = _cost_measurement(snapshot)

    frozen: dict[str, str] = {}
    if freeze_dir is not None:
        sampled_ids = sorted({entry.item_id for entry in truth})
        frozen = _freeze(
            freeze_dir=Path(freeze_dir).expanduser(),
            snapshot=snapshot,
            workspace_root=workspace,
            lane=lane,
            sampled=sampled_ids,
        )

    return LaneReport(
        run_id=run_id,
        lane=lane.name if lane else "",
        tier_bar=list(bar_kinds),
        generated_at=datetime.now(timezone.utc).isoformat(),
        records=len(records),
        yield_by_source=yield_entries,
        coverage=coverage_entries,
        coverage_meeting_bar=meeting_share,
        support=support_entries,
        claims_supported=supported_total,
        claims_refused=refused_total,
        truth=truth,
        truth_sampled=sampled_count,
        cost=cost,
        frozen=frozen,
        notes=notes,
    )


def report_lines(report: LaneReport) -> str:
    """The report as a person reads it."""
    lines = [
        f"Lane report: {report.run_id}"
        + (f" (lane '{report.lane}', bar {', '.join(report.tier_bar) or 'by claimed tier'})" if report.lane else ""),
        f"  records: {report.records}",
    ]
    lines.append("  yield:")
    for source_entry in report.yield_by_source:
        attempted = "?" if source_entry.attempted is None else source_entry.attempted
        lines.append(
            f"    {source_entry.source:28} {source_entry.kind:8} captured {source_entry.captured:3}"
            f"  attempted {attempted:>3}  skipped {source_entry.skipped}"
        )
    if not report.yield_by_source:
        lines.append("    (no captured records to attribute)")
    lines.append(f"  coverage: {report.coverage_meeting_bar:.0%} of records meet the bar")
    lines.append(
        f"  support: {report.claims_supported} claim(s) carried by a qualifying source, "
        f"{report.claims_refused} refused"
    )
    for support_entry in report.support:
        if support_entry.refused:
            lines.append(f"    {support_entry.item_id}: {'; '.join(support_entry.reasons[:3])}")
    live = {"confirmed": 0, "missing": 0, "unreachable": 0, "not_checked": 0}
    for truth_entry in report.truth:
        live[truth_entry.live] = live.get(truth_entry.live, 0) + 1
    exact = sum(1 for truth_entry in report.truth if truth_entry.exact)
    addressed = sum(1 for truth_entry in report.truth if truth_entry.addressed)
    lines.append(
        f"  truth sample: {report.truth_sampled} entity(ies), {len(report.truth)} quote(s) — "
        f"exact {exact}, addressed {addressed}, live confirmed {live['confirmed']}, "
        f"missing {live['missing']}, unreachable {live['unreachable']}, not checked {live['not_checked']}"
    )
    for flagged in report.truth:
        if not flagged.exact or flagged.live == "missing" or not flagged.addressed:
            lines.append(
                f"    {flagged.item_id}: exact={flagged.exact} live={flagged.live} "
                f"addressed={flagged.addressed}"
                + (f" ({flagged.reason})" if flagged.reason else "")
                + (f" [{flagged.uri}]" if flagged.uri else "")
            )
    lines.append(
        f"  cost: {report.cost.cost} over {len(report.cost.routes)} route(s) "
        f"({', '.join(report.cost.routes) or 'none'}), {report.cost.attempts} attempt(s), "
        f"batches {report.cost.batches_verified} verified / {report.cost.batches_failed} failed, "
        f"wall {report.cost.wall_seconds if report.cost.wall_seconds is not None else '?'}s"
    )
    if report.frozen:
        lines.append(f"  frozen: {report.frozen.get('manifest', '')}")
    for note in report.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)

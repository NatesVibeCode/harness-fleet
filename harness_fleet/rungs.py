"""A lane's ladder is a DAG, and every rung is a table.

A ladder already *is* a pipeline: a rung names the evidence it reads, the gates
it is entitled to settle, and the pages it may fetch to settle them. Compiled
into a graph, each rung becomes a node — a ``gate`` node that puts this rung's
gates to the population the rung above it left alive, and a ``retrieve`` node
that spends the page visits the rung declared. Each node writes the table it
produced, so the population is inspectable at every step instead of only at the
end: who is still alive, what this rung put to them, what they earned, and —
for the ones that died — which gate killed them and on what text.

The data rungs through. A candidate eliminated at the first rung is not in the
second rung's table, is never fetched, and cannot reappear later; a candidate
the ladder says is settled is not walked again just because a rung remains. Two
things make that true and both are stated once, here: ``gate_rows`` decides who
stays, and ``advances_to`` decides who is owed the next rung's evidence.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .gates import (
    DEFAULT_LADDER,
    SNIPPET,
    FunnelReport,
    GateProfile,
    LadderRung,
    run_funnel,
)

#: The spine every rung table shares, so two rungs' tables can be read side by
#: side: the same candidate is the same row, and the columns say what this rung
#: saw. Anything a node knows beyond this is appended rather than folded in.
TABLE_COLUMNS: tuple[str, ...] = (
    "item_id",
    "candidate",
    "rung",
    "evidence",
    "outcome",
    "earned",
    "because",
    "gates",
    "surfaces",
    "pages",
    "text_path",
    "source_uri",
)


def table_path(directory: str | Path) -> Path:
    """Where a node writes its table, given the node's own directory."""
    return Path(directory) / "table.csv"


def write_table(path: str | Path, rows: Sequence[dict[str, Any]]) -> int:
    """Write rows as a CSV table and return how many were written.

    Columns are the spine in order, then any extra key the rows carry, in first
    appearance order. Values that are not scalars are written as JSON so a
    reader gets the whole verdict rather than ``[object Object]``; the file is
    one flat table either way.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = list(TABLE_COLUMNS)
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in columns})
    return len(rows)


def read_table(path: str | Path) -> list[dict[str, str]]:
    """Read a rung table back, for a downstream node or a person."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return json.dumps(value, sort_keys=True)


def record_text(record: Any) -> str:
    """The prose a gate reads for one record.

    A run's record carries the text it was verified against; a record that only
    has quotes is read through them, in order, because that is still the part of
    the page the run is standing on. An empty string is a real answer here — it
    is what makes a gate come back ``unknown`` instead of guessed.
    """
    text = getattr(record, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    quotes = getattr(record, "quotes", None) or []
    return " ".join(
        str(getattr(quote, "text", "") or "") for quote in quotes
    ).strip()


def candidate_name(record: Any, item_id: str) -> str:
    """What this row is *about*: the attributed entity, else the item id.

    Attribution is the walk's job and it has already happened by the time a
    record exists, so the name here is read, not derived — ``entity``/``domain``
    when the record carries one, the canonical form of its own URI when it does
    not, and the item id as the last honest answer.
    """
    metadata = getattr(record, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("entity", "domain"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    uri = getattr(record, "source_uri", None)
    if isinstance(uri, str) and uri.strip():
        from .sources import entity_key_for

        key = entity_key_for(uri, record_text(record))
        if key:
            return key
    return item_id


def gate_rows(
    records: Sequence[Any],
    *,
    rung: LadderRung,
    ladder: Sequence[LadderRung],
    profile: GateProfile | None,
    evidence: str = SNIPPET,
    text_for: Any = None,
    tables: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[FunnelReport]]:
    """Run this rung's gates over the population and return its table.

    One row per candidate the rung was given — not one per survivor. A table
    that only holds winners cannot show where the world shrank, and the whole
    point of a rung is that it shrinks the world, so the row states the verdict
    and the gate that produced it. The reports come back with the rows because
    the counts a run reports are the same verdicts, aggregated.
    """
    rows: list[dict[str, Any]] = []
    reports: list[FunnelReport] = []
    for record in records:
        item_id = str(getattr(record, "item_id", "") or "")
        text = str(text_for(record) if text_for else record_text(record))
        candidate = candidate_name(record, item_id)
        # A rung gates on its own gates, but the *profile* decides which of them
        # it can put at all: a lane asking the kind question of a services
        # population, with nothing authored about size or place, has one gate
        # live and the rest idle. run_funnel owns that rule; this only reports
        # what it decided.
        report = run_funnel(
            candidate,
            snippet=text,
            profile=profile,
            evidence=evidence,
            ladder=ladder,
        )
        reports.append(report)
        rows.append(
            {
                "item_id": item_id,
                "candidate": candidate,
                "rung": rung.name,
                "evidence": evidence,
                "outcome": report.verdict,
                "earned": report.earned,
                "because": report.because(),
                "gates": [result.as_dict() for result in report.results],
                "surfaces": [],
                "pages": [],
                "text_path": "",
                "source_uri": str(getattr(record, "source_uri", "") or ""),
                "exhausted": report.exhausted,
            }
        )
    return rows, reports


def advances_to(report: FunnelReport, rung: LadderRung | None) -> bool:
    """Whether this candidate is owed the next rung's evidence.

    Two ways to be owed it, and both are the same question asked twice. The
    ladder has already named the rung this report earned — that is the answer,
    and it is why ``earned`` is on the row. Failing that, the report is a lead
    still holding something open that the next rung's gates would settle, which
    happens when the rung above it was not the one the walk was standing on.

    An eliminated candidate is never owed anything, and a qualified one has
    nothing left to buy: walking it would spend a page visit on a question that
    is already answered.
    """
    if report.eliminated or report.qualified or rung is None:
        return False
    if report.earned and report.earned == rung.name:
        return True
    return any(gate in rung.gates for gate in report.unresolved)


def surviving_ids(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The item ids the rung above left alive: everything it did not eliminate."""
    return [str(row["item_id"]) for row in rows if row.get("outcome") != "eliminated"]


def count_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The shape of one rung's table, for a run to report instead of imply."""
    counts: dict[str, Any] = {"candidates": len(rows), "verdicts": {}}
    for row in rows:
        verdict = str(row.get("outcome") or "unknown")
        counts["verdicts"][verdict] = counts["verdicts"].get(verdict, 0) + 1
        if verdict != "eliminated":
            continue
        for result in row.get("gates") or []:
            if result.get("outcome") == "fail":
                counts.setdefault("eliminated_at", {})
                counts["eliminated_at"][result["gate"]] = (
                    counts["eliminated_at"].get(result["gate"], 0) + 1
                )
                break
    return counts


def resolved_gate_profile(
    lane: Any,
    *,
    workspace: str | Path = ".",
    profile_path: str | Path | None = None,
) -> GateProfile:
    """The gates this lane puts to a candidate, from the lane and the profile.

    Both bind: a profile says what this engagement needs, a lane says what the
    product accepts, and the intersection is what a run may actually gate on. No
    profile authored yet is not an error — the lane's own gates still apply,
    which is what lets a first run eliminate the obviously wrong companies
    before anyone has written anything down.
    """
    root = Path(workspace).expanduser()
    lane_gates = GateProfile.from_object(lane.funnel.gate_profile() if lane else None)
    explicit = Path(profile_path).expanduser() if profile_path else None
    candidates = [explicit] if explicit else [
        root / "ideal_partner_profile.json",
        root / "profiles" / "ideal_partner_profile.json",
    ]
    for path in candidates:
        if path is None or not path.is_file():
            continue
        try:
            from .partner import IdealPartnerProfile

            profile = IdealPartnerProfile.load(path)
        except Exception:
            continue
        return lane_gates.intersect(GateProfile.from_object(profile))
    if explicit is not None:
        raise ValueError(f"profile not found: {explicit}")
    return lane_gates


def _node_slug(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(name).strip())
    return cleaned.strip("-") or "rung"


def lane_spec(
    lane: Any,
    *,
    from_run: str | None = None,
    from_items: str | None = None,
    name: str | None = None,
    workspace: str | Path = ".",
    profile_path: str | Path | None = None,
    max_pages: int = 8,
    per_surface: int = 3,
    timeout: float = 20.0,
    delay: float = 1.0,
    respect_robots: bool = True,
    vendor_stories: bool = True,
    gates: bool = True,
    walk: bool = True,
) -> dict[str, Any]:
    """Compile a lane's ladder into a DAG: one gate per rung, one walk between.

    The chain reads top to bottom exactly as the ladder does: the discovery
    run's records are gated on the rung that reads search results, the survivors
    have that rung's pages fetched, the fetched text is gated on the next rung,
    and so on. Nothing here decides who advances — every node asks
    ``advances_to``, so the graph and a single-entity walk answer the same
    question the same way.

    A fetched rung that declares no surfaces still gets a gate node: it is
    re-put to the population on the text already in hand, which is how a ladder
    says two sets of gates have to hold. What it does not get is a walk.
    """
    if (from_run is None) == (from_items is None):
        raise ValueError("a lane graph starts from exactly one of 'from_run' or 'from_items'")
    rungs = list(lane.funnel.rungs() or DEFAULT_LADDER)
    if not rungs:
        raise ValueError(f"lane '{getattr(lane, 'name', '')}' declares no rungs to run")
    nodes: list[dict[str, Any]] = []
    previous_gate: str | None = None
    for index, rung in enumerate(rungs):
        if not gates and index == 0:
            # A run that asked for no gates still walks: the ladder's surfaces
            # are how it knows where to look, and the walks stay nodes.
            first_rung = next((r for r in rungs if r.surfaces), None)
            if not walk or first_rung is None:
                break
            nodes.append({
                "kind": "retrieve",
                "id": f"r{index}-{_node_slug(first_rung.name)}",
                "lane": lane.name,
                "rung": first_rung.name,
                "from_items": from_items,
                "max_pages": max_pages,
                "per_surface": per_surface,
                "timeout": timeout,
                "delay": delay,
                "respect_robots": respect_robots,
                "vendor_stories": vendor_stories,
            })
            break
        gate_id = f"g{index}-{_node_slug(rung.name)}"
        source: dict[str, Any]
        if index == 0:
            source = (
                {"from_items": from_items} if from_items is not None else {"from_run": from_run}
            )
        elif not walk:
            source = {"from_gate": previous_gate}
        elif previous_gate is not None and rung.surfaces:
            retrieve_id = f"r{index}-{_node_slug(rung.name)}"
            nodes.append(
                {
                    "kind": "retrieve",
                    "id": retrieve_id,
                    "lane": lane.name,
                    "rung": rung.name,
                    "from_gate": previous_gate,
                    "max_pages": max_pages,
                    "per_surface": per_surface,
                    "timeout": timeout,
                    "delay": delay,
                    "respect_robots": respect_robots,
                    "vendor_stories": vendor_stories,
                }
            )
            source = {"from_retrieve": retrieve_id}
        elif previous_gate is not None:
            source = {"from_gate": previous_gate}
        else:  # pragma: no cover - index 0 is handled above
            raise ValueError(f"ladder rung '{rung.name}' has nowhere to read from")
        nodes.append(
            {
                "kind": "gate",
                "id": gate_id,
                "lane": lane.name,
                "rung": rung.name,
                **({"profile": str(profile_path)} if profile_path else {}),
                **source,
            }
        )
        previous_gate = gate_id
    return {
        "name": name or f"lane-{getattr(lane, 'name', 'lane')}",
        "nodes": nodes,
    }


def next_rung(ladder: Sequence[LadderRung], rung: LadderRung) -> LadderRung | None:
    """The rung above this one, or None when this is the top of the ladder."""
    for index, candidate in enumerate(ladder):
        if candidate.name == rung.name:
            return ladder[index + 1] if index + 1 < len(ladder) else None
    return None

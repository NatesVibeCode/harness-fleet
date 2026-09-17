"""Read-only results board for a finished run.

Serves one run's verified records as a localhost-only web page: every attribute
the task asked for, the checklist breakdown behind the score, the provenance of
each record (which route produced it, what it cost), and the verbatim quotes
that support every claim.

Two properties matter more than the visuals:

* **Read-only.** Nothing here writes to SQLite, spends budget, or starts work.
  The page is a viewer; discovery, scoring, and export stay with the CLI.
* **Schema-driven.** Labels, types, and the attribute list come from the run's
  own ``TaskSpec``, never from a hardcoded partner vocabulary. Partner research
  is the showcase today, but any task renders with its own fields.
"""
from __future__ import annotations

import argparse
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .export import _build_item_route_map, verified_records_from_snapshot
from .models import TIER_BY_SCORE, ProviderReceipt
from .store import HarnessStore

# Packaged UI. Everything served comes from here, so the repo is the single
# source of truth for what an operator sees.
BOARD_DIR = Path(__file__).resolve().parent / "resources" / "board"
BOARD_HTML_PATH = BOARD_DIR / "index.html"

# Localhost-only guard: refuse requests whose Host header claims another site,
# or whose Origin is somewhere else. This is how a DNS-rebinding page would try
# to read the run database through the user's browser.
_ALLOWED_HOSTS = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _humanize(item_id: str) -> str:
    """q1_billable_delivery -> 'Billable delivery'; identified_practice -> 'Identified practice'."""
    name = re.sub(r"^q\d+_", "", str(item_id))
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else str(item_id)


def _lane_from_report(runs_dir: Path, run_id: Any) -> str | None:
    """The lane a run recorded for itself, from its own discovery report.

    This is the authoritative answer: ``research --lane X`` writes the lane next
    to the run, so the board names what the run was for instead of inferring a
    product from a path or a task name.
    """
    if not run_id:
        return None
    report = runs_dir / str(run_id) / "discovery_report.json"
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lane = payload.get("lane") if isinstance(payload, dict) else None
    return str(lane) if lane else None


def _properties_map(task: Any, *path: str) -> dict[str, Any]:
    """The property map at ``path`` inside the task's claims schema.

    ``()`` returns the top-level claim fields; ``("answers",)`` returns the
    fields of the nested answers object. Missing or malformed schema returns an
    empty map rather than raising, so a hand-written TaskSpec still renders.
    """
    schema = getattr(task, "claims_schema", None)
    if not isinstance(schema, dict):
        return {}
    node: Any = schema.get("properties")
    for key in path:
        if not isinstance(node, dict):
            return {}
        child: Any = node.get(key)
        node = child.get("properties") if isinstance(child, dict) else None
    return node if isinstance(node, dict) else {}


def _field_specs(task: Any, claims_schema_path: tuple[str, ...], values: Any) -> list[dict[str, Any]]:
    """Describe the attributes under one claims object so the UI can render them."""
    values = values if isinstance(values, dict) else {}
    properties = _properties_map(task, *claims_schema_path)
    if not properties:
        return []
    specs: list[dict[str, Any]] = []
    for key, raw in properties.items():
        schema: dict[str, Any] = raw if isinstance(raw, dict) else {}
        kind = schema.get("type") or "string"
        if kind == "array":
            kind = "list"
        specs.append({
            "key": key,
            "label": _humanize(key),
            "type": kind,
            "description": schema.get("description") or "",
            "choices": list(schema.get("enum") or []),
            "present": key in values,
        })
    return specs


def _batches_by_item(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map item_id -> {route, provider, cost, duration, status, attempts}."""
    out: dict[str, dict[str, Any]] = {}
    for batch in (snapshot.get("batches") or {}).values():
        if not isinstance(batch, dict):
            continue
        receipt = batch.get("receipt") or {}
        result = batch.get("result")
        items: list[Any] = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict) and isinstance(result.get("items"), list):
            items = result["items"]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("item_id")
            if not item_id:
                continue
            out[str(item_id)] = {
                "route": receipt.get("requested_route") or receipt.get("route_id"),
                "provider": receipt.get("provider"),
                "cost": receipt.get("cost"),
                "cost_status": receipt.get("cost_status"),
                "duration_seconds": receipt.get("duration_seconds"),
                "status": batch.get("status"),
                "attempts": batch.get("attempts"),
            }
    return out


def _receipts(snapshot: dict[str, Any]) -> list[ProviderReceipt]:
    raw = snapshot.get("model_runs")
    if isinstance(raw, list) and raw:
        receipts = []
        for entry in raw:
            try:
                receipts.append(ProviderReceipt.model_validate(entry))
            except Exception:  # a foreign receipt must not break the board
                continue
        return receipts
    receipts = []
    for batch in (snapshot.get("batches") or {}).values():
        if isinstance(batch, dict) and batch.get("receipt"):
            try:
                receipts.append(ProviderReceipt.model_validate(batch["receipt"]))
            except Exception:
                continue
    return receipts


def _facet_label(value: Any) -> str | None:
    """A facet value a human can read, or None when there is nothing to show.

    A nested object (commercial terms, for instance) collapses to its stated
    parts — "employees: 100+ skilled engineers" — instead of raw JSON, and an
    object whose every part is blank is not a value at all.
    """
    if isinstance(value, dict):
        parts = [
            f"{key.replace('_', ' ')}: {_facet_label(item)}"
            for key, item in value.items()
            if _facet_label(item) is not None
        ]
        return " · ".join(parts) if parts else None
    if isinstance(value, (list, tuple)):
        parts = [item for item in (_facet_label(entry) for entry in value) if item is not None]
        return " · ".join(parts) if parts else None
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _counter(values: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = _facet_label(value)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _load_evidence(runs_dir: Path | str | None, run_id: str | None) -> dict[str, Any]:
    """The run's evidence readout, when one was written next to its packet.

    Board rendering must not depend on it: a run scored before evidence reads
    existed, or one whose workspace has moved, simply has no evidence block.
    """
    if not runs_dir or not run_id:
        return {}
    path = Path(runs_dir) / str(run_id) / "evidence.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def ledger_view(store: HarnessStore, *, limit: int = 200, trend: int = 15) -> dict[str, Any]:
    """The running list, as the page reads it: firms, counts, and who moved."""
    from .ledger import Ledger

    ledger = Ledger(store)
    return {
        "entities": ledger.entities(limit=limit),
        "counts": ledger.counts(),
        "movers": ledger.movers(limit=trend),
    }


def build_board_payload(
    db_path: Path | str,
    run_id: str | None = None,
    runs_dir: Path | str | None = None,
    ledger_limit: int = 200,
) -> dict[str, Any]:
    """Assemble everything the page needs for one run.

    Raises ``KeyError`` when the run does not exist and ``ValueError`` when the
    database holds no runs at all, so the CLI can explain either case plainly.
    """
    store = HarnessStore(db_path)
    # The running list is about every run, not this one, so it is read before
    # the run is picked: a board that only knew the current run would lose the
    # firms it looked at last week.
    ledger_payload = ledger_view(store, limit=ledger_limit)
    snapshot = store.run_snapshot(run_id) if run_id else store.run_snapshot(latest_run_id(store))
    records, task = verified_records_from_snapshot(snapshot)
    evidence_run = _load_evidence(runs_dir, snapshot.get("run_id"))
    raw_items = evidence_run.get("items")
    evidence_items: dict[str, Any] = raw_items if isinstance(raw_items, dict) else {}
    route_by_item = _build_item_route_map(snapshot)
    provenance_by_item = _batches_by_item(snapshot)
    receipts = _receipts(snapshot)

    checklist_points: dict[str, int] = dict(getattr(task, "checklist", None) or {})
    checklist_total = sum(checklist_points.values())
    answers_spec = _field_specs(task, ("answers",), records[0].claims.get("answers", {}) if records else {})
    generic_spec = _field_specs(task, (), records[0].claims if records else {})

    partners: list[dict[str, Any]] = []
    for record in records:
        claims = record.claims if isinstance(record.claims, dict) else {}
        answers = claims.get("answers") if isinstance(claims.get("answers"), dict) else {}
        raw_checklist = claims.get("checklist")
        raw_checklist = raw_checklist if isinstance(raw_checklist, dict) else {}
        score = claims.get("score")
        score = score if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        quotes: list[dict[str, Any]] = []
        for quote in getattr(record, "quotes", []) or []:
            entry = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else dict(quote)
            quotes.append({
                "text": entry.get("text"),
                "supports": list(entry.get("supports") or []),
                "slice_id": entry.get("slice_id"),
                "start": entry.get("start"),
                "end": entry.get("end"),
            })
        provenance = provenance_by_item.get(str(record.item_id), {})
        if not provenance.get("route"):
            provenance = {**provenance, "route": route_by_item.get(str(record.item_id))}
        # Two things called a tier, and they answer different questions. The
        # band is where the score falls; the supported tier is what the gathered
        # evidence actually carries, capped by the run's own rule. The board
        # filters by band — that is what the chips below are — so `tier` stays
        # the band, and the capped tier travels beside it rather than being
        # mistaken for it.
        evidence_item = evidence_items.get(str(record.item_id)) or {}
        if not isinstance(evidence_item, dict):
            evidence_item = {}
        partners.append({
            "id": str(record.item_id),
            "name": str(record.item_id),
            "score": score,
            "tier": claims.get("fit_tier"),
            "tier_supported": evidence_item.get("tier_supported") or "",
            "tier_capped": bool(evidence_item.get("tier_capped")),
            "checklist": {item: bool(raw_checklist.get(item)) for item in checklist_points},
            "checklist_points": checklist_points,
            "earned_points": sum(points for item, points in checklist_points.items() if raw_checklist.get(item)),
            "practice": claims.get("identified_practice"),
            "reasoning": claims.get("reasoning"),
            "answers": answers,
            "claims": claims,
            "quotes": quotes,
            "source": {
                "uri": record.source_uri,
                "digest": record.source_digest,
                "content_type": record.content_type,
                "captured_at": record.captured_at,
                "scored_at": record.scored_at,
            },
            "provenance": provenance,
            "diversity": claims.get("source_diversity_count"),
            "evidence": evidence_item,
        })

    partners.sort(key=lambda p: (p["score"] is None, -(p["score"] or 0), p["id"]))

    # Facets are computed over the whole set so the filters can describe it.
    facets: dict[str, dict[str, int]] = {}
    for spec in answers_spec:
        key = spec["key"]
        if spec["type"] == "list":
            facets[key] = _counter([v for p in partners for v in (p["answers"].get(key) or [])])
        else:
            facets[key] = _counter([p["answers"].get(key) for p in partners])
    scores = [p["score"] for p in partners if isinstance(p["score"], (int, float))]
    costs = [r.cost for r in receipts if r.cost is not None]
    # run_snapshot carries batches, not an audit block (that is built at export
    # time), so count the batch outcomes here rather than reporting None.
    batches = [b for b in (snapshot.get("batches") or {}).values() if isinstance(b, dict)]
    batches_verified = sum(1 for b in batches if b.get("status") == "verified")
    batches_failed = sum(1 for b in batches if b.get("status") == "failed")

    return {
        "run": {
            "run_id": snapshot.get("run_id"),
            "status": snapshot.get("status"),
            "created_at": snapshot.get("created_at"),
            "finished_at": snapshot.get("finished_at"),
            "total_items": snapshot.get("total_items"),
            "attempts_used": snapshot.get("attempts_used"),
            "input_path": snapshot.get("input_path"),
            "task": getattr(task, "name", None),
            "lane": _lane_from_report(Path(runs_dir), snapshot.get("run_id")) if runs_dir else None,
            "task_revision": snapshot.get("task_revision_id"),
            "instructions": getattr(task, "instructions", None),
        },
        "stats": {
            "records": len(partners),
            "scored": len(scores),
            "unscored": len(partners) - len(scores),
            "average_score": round(sum(scores) / len(scores), 1) if scores else None,
            "best_score": max(scores) if scores else None,
            "tier_counts": _counter([p["tier"] for p in partners]),
            "quote_count": sum(len(p["quotes"]) for p in partners),
            "records_with_quotes": sum(1 for p in partners if p["quotes"]),
            "routes": sorted({p["provenance"].get("route") for p in partners if p["provenance"].get("route")}),
            "cost_reported": round(sum(costs), 6) if costs else 0.0,
            "batches_verified": batches_verified,
            "batches_failed": batches_failed,
            "batches_total": len(batches),
            "attempts_used": snapshot.get("attempts_used"),
            "tier_capped": len(evidence_run.get("tier_capped") or {}),
            "evidence_kinds": evidence_run.get("kind_totals") or {},
            "lone_claims": len(evidence_run.get("contradictions") or {}),
        },
        "checklist": [
            {
                "item_id": item,
                "label": _humanize(item),
                "points": points,
                "description": (_properties_map(task, "checklist").get(item) or {}).get("description", ""),
                "passed": sum(1 for p in partners if p["checklist"].get(item)),
            }
            for item, points in checklist_points.items()
        ],
        "checklist_total": checklist_total,
        "tiers": [{"tier": tier, "min_score": threshold} for threshold, tier in TIER_BY_SCORE],
        "attributes": answers_spec,
        "claim_fields": [spec for spec in generic_spec if spec["key"] not in ("answers", "checklist")],
        "facets": facets,
        "partners": partners,
        "ledger": ledger_payload,
    }


def latest_run_id(store: HarnessStore) -> str:
    """Most recently created run, or a clear error when the database is empty."""
    with store.connect() as connection:
        row = connection.execute(
            "SELECT run_id FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise ValueError("this database has no runs yet; run a campaign first (see `harness-fleet run --help`)")
    return str(row["run_id"])


def list_runs(store: HarnessStore, limit: int = 25) -> list[dict[str, Any]]:
    """Recent runs, newest first, so the page can offer a picker."""
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT run_id,status,total_items,attempts_used,created_at,finished_at "
            "FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _request_allowed(handler: BaseHTTPRequestHandler) -> bool:
    port = int(handler.server.server_port)  # type: ignore[attr-defined]
    host = (handler.headers.get("Host") or "").strip()
    if host and not _ALLOWED_HOSTS.match(host):
        return False
    origin = (handler.headers.get("Origin") or "").strip().rstrip("/")
    if not origin:
        return True
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
    return origin.lower() in allowed


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class BoardHandler(BaseHTTPRequestHandler):
    """Read-only: every route is a GET, and none of them touch the database twice."""

    server_version = "HarnessFleetBoard/1.0"
    db_path: Path | None = None
    run_id: str | None = None
    runs_dir: Path | str | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        return

    def do_GET(self) -> None:  # noqa: N802
        if not _request_allowed(self):
            _send_json(self, 403, {"error": "request rejected: board is localhost-only"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            try:
                page = BOARD_HTML_PATH.read_bytes()
            except OSError as exc:
                _send_json(self, 500, {"error": f"board page missing: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        if path == "/api/board":
            # ?run=<id> lets the page switch runs without restarting the server.
            requested = (params.get("run") or [""])[0] or self.run_id or None
            try:
                payload = build_board_payload(self.db_path, requested, self.runs_dir)  # type: ignore[arg-type]
            except KeyError as exc:
                _send_json(self, 404, {"error": str(exc)})
                return
            except ValueError as exc:
                _send_json(self, 409, {"error": str(exc)})
                return
            except Exception as exc:  # a broken run must not kill the server
                _send_json(self, 500, {"error": str(exc)})
                return
            _send_json(self, 200, payload)
            return
        if path == "/api/ledger":
            # The running list on its own, for a page that wants to poll it
            # without rebuilding a run's whole payload.
            try:
                _send_json(self, 200, ledger_view(HarnessStore(self.db_path)))
            except Exception as exc:
                _send_json(self, 500, {"error": str(exc)})
            return
        if path == "/api/runs":
            runs = list_runs(HarnessStore(self.db_path))
            requested = (params.get("run") or [""])[0] or self.run_id or None
            current = requested or (runs[0]["run_id"] if runs else None)
            _send_json(self, 200, {"runs": runs, "current": current})
            return
        _send_json(self, 404, {"error": f"no route for {path}"})


def run_board_server(
    db_path: Path | str,
    run_id: str | None = None,
    port: int = 8100,
    host: str = "127.0.0.1",
    open_browser: bool = False,
    runs_dir: Path | str | None = None,
) -> None:
    """Serve the board until interrupted."""
    BoardHandler.db_path = Path(db_path)
    BoardHandler.run_id = run_id
    BoardHandler.runs_dir = runs_dir
    server = ThreadingHTTPServer((host, port), BoardHandler)
    url = f"http://{host}:{server.server_port}/"
    payload = build_board_payload(db_path, run_id, runs_dir)  # fail before opening a page
    print(f"Harness Fleet board: {url}")
    print(f"  run: {payload['run']['run_id']} ({payload['run']['status']})")
    print(f"  {payload['stats']['records']} records · {payload['stats']['quote_count']} quotes · "
          f"{len(payload['stats']['routes'])} route(s)")
    print("  Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="harness-fleet.db", help="SQLite control-plane path")
    parser.add_argument("--run-id", default=None, help="Run to show (default: the most recent)")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", help="Open the page in a browser")
    args = parser.parse_args(argv)
    run_board_server(args.db, args.run_id, port=args.port, host=args.host, open_browser=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

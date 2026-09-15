"""Local harness studio: stdlib-only HTTP UI over the fleet control plane.

No third-party web dependencies and no build step: one ``ThreadingHTTPServer``
serving JSON endpoints plus a single static page. Steps are sequenced runs --
each step carries its own task, explicit route picks, budgets, and an
optional paid opt-in with a recorded trust note -- chained by parent run id
so rescoring lineage stays queryable. Paid routes never run implicitly: the
engine admits them only through ``allowed_routes``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from .catalog import RouteCatalog
from .engine import Engine
from .input_data import InputItem
from .models import RoutePolicy
from .store import HarnessStore
from .task import (
    PRESETS,
    apply_scoring_edit,
    create_task_from_preset,
    duplicate_spec,
    scoring_view,
)

STUDIO_DIR = Path(__file__).resolve().parent / "resources" / "studio"


def _studio_build() -> str:
    """Content hash of the studio page, so an open tab can detect a new build."""
    try:
        page = (STUDIO_DIR / "index.html").read_bytes()
    except OSError:
        return "unknown"
    return hashlib.sha256(page).hexdigest()[:12]


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


MAX_BODY_BYTES = 1_000_000
MAX_DRAIN_BYTES = 8_000_000


def _drain(handler: BaseHTTPRequestHandler, length: int) -> None:
    """Consume an unread request body so the client can read our response.

    Bounded so a lying Content-Length cannot pin a worker thread indefinitely.
    """
    remaining = min(length, MAX_DRAIN_BYTES)
    while remaining > 0:
        chunk = handler.rfile.read(min(65_536, remaining))
        if not chunk:
            return
        remaining -= len(chunk)


def _read_json(handler: BaseHTTPRequestHandler) -> Any:
    raw_length = handler.headers.get("Content-Length")
    if raw_length in (None, ""):
        return {}
    try:
        length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise ValueError("Content-Length must be an integer") from exc
    if length <= 0:
        return {}
    if length > MAX_BODY_BYTES:
        _drain(handler, length)
        raise ValueError(f"request body is too large (limit {MAX_BODY_BYTES} bytes)")
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"request body is not JSON: {exc}") from exc


def _step_int(step: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = step.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not low <= number <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return number


def _store(handler: BaseHTTPRequestHandler) -> HarnessStore:
    return HarnessStore(Path(handler.server.db_path))  # type: ignore[attr-defined]


def _harness_cards() -> list[dict[str, Any]]:
    from .providers.registry import HARNESS_SPECS

    cards = []
    for spec in HARNESS_SPECS:
        path = shutil.which(spec.binary)
        cards.append({
            "name": spec.name,
            "binary": spec.binary,
            "available": path is not None,
            "detail": path or f"{spec.binary} not found in PATH",
        })
    return cards


def _scoring_payload(store: HarnessStore, name: str) -> dict[str, Any]:
    """Read model for one task's scoring contract, tagged with its revision."""
    spec = store.get_task(name)
    view = scoring_view(spec)
    view["revision_id"] = store.current_task_revision(name)
    return view


def _task_subpath(path: str, suffix: str) -> str | None:
    """Extract <name> from /api/tasks/<name><suffix>; None when it is not one."""
    prefix = "/api/tasks/"
    if path.startswith(prefix) and path.endswith(suffix):
        name = path[len(prefix):-len(suffix)]
        if name and "/" not in name:
            return name
    return None


def _scoring_task_name(path: str) -> str | None:
    """Extract <name> from /api/tasks/<name>/scoring; None when it is not one."""
    return _task_subpath(path, "/scoring")


def _task_exists(store: HarnessStore, name: str) -> bool:
    try:
        store.current_task_revision(name)
        return True
    except KeyError:
        return False


def _request_allowed(handler: BaseHTTPRequestHandler) -> bool:
    """Reject browser requests from other origins (localhost CSRF / DNS rebinding).

    The studio spends model budget and mutates task contracts, so a page the
    user happens to visit must not be able to drive it. Absent headers are
    allowed so plain CLI and HTTP clients keep working.
    """
    port = int(handler.server.server_port)  # type: ignore[attr-defined]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    host = (handler.headers.get("Host") or "").strip().lower()
    if host and host not in hosts:
        return False
    origin = (handler.headers.get("Origin") or "").strip().rstrip("/").lower()
    return not origin or origin in {f"http://{name}" for name in hosts}


def _validation_message(exc: ValidationError) -> str:
    """Flatten a pydantic error into one readable line naming the bad fields."""
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "task"
        parts.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "invalid task spec — " + "; ".join(parts)


def _run_step(
    store: HarnessStore,
    workspace: Path,
    step: dict[str, Any],
    parent_run_id: str | None,
) -> dict[str, Any]:
    """Execute one studio step: task + explicit routes + budgets, fail-closed."""
    if not isinstance(step, dict):
        raise ValueError("each step must be an object")
    raw_items = step.get("items") or []
    items = [InputItem.model_validate(item) for item in raw_items]
    if not items:
        raise ValueError("each step needs at least one {item_id, text} item")
    route_ids = [str(route) for route in (step.get("routes") or [])]
    if not route_ids:
        raise ValueError("each step needs at least one route id")
    task_name = str(step.get("task") or "")
    preset = step.get("preset")
    if preset:
        spec = create_task_from_preset(task_name or f"studio-{uuid.uuid4().hex[:8]}", preset_name=str(preset))
        store.register_task(spec)
    elif task_name:
        spec = store.get_task(task_name)
    else:
        raise ValueError("each step needs a task name or a preset")
    allow_paid = bool(step.get("allow_paid", False))
    policy = RoutePolicy(
        allowed_routes=route_ids,
        free_only=not allow_paid,
        note=(str(step.get("paid_note")) if step.get("paid_note") else None),
        max_request_cost=step.get("max_request_cost"),
    )
    run_id = str(step.get("run_id") or f"studio-{uuid.uuid4().hex[:8]}")
    engine = Engine(
        task=spec,
        store=store,
        policy=policy,
        max_attempts_per_batch=_step_int(step, "max_per_batch", 3, 1, 100),
    )
    packet = engine.run_campaign(
        raw_items=items,
        run_id=run_id,
        input_path="studio",
        concurrency=_step_int(step, "concurrency", 2, 1, 64),
        max_attempts=_step_int(step, "max_attempts", 20, 1, 1_000_000),
        output_packet_path=workspace / "runs" / run_id / "clean_packet.json",
        parent_run_id=parent_run_id,
    )
    snapshot = store.run_snapshot(run_id)
    audit = packet.get("audit", {})
    return {
        "run_id": run_id,
        "status": snapshot.get("status"),
        "task": spec.name,
        "routes": route_ids,
        "allow_paid": allow_paid,
        "paid_note": policy.note,
        "verified": packet.get("total_verified_records", 0),
        "tokens": audit.get("total_tokens_consumed", 0),
        "cost": audit.get("total_cost_reported", 0.0),
        "receipts": len(packet.get("receipts", [])),
    }


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "HarnessStudio/0.3"

    def log_message(self, *args: Any) -> None:  # keep studio output machine-clean
        pass

    def _workspace(self) -> Path:
        return Path(self.server.workspace_root)  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not _request_allowed(self):
                _send_json(self, 403, {"error": "request rejected: studio is localhost-only"})
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                page = (STUDIO_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/harnesses":
                _send_json(self, 200, {"harnesses": _harness_cards()})
            elif path == "/api/routes":
                catalog = RouteCatalog(db_path=_store(self).path)
                _send_json(self, 200, {"routes": catalog.get_routes(free_only=False, include_disabled=True)})
            elif path == "/api/presets":
                _send_json(self, 200, {"presets": sorted(PRESETS)})
            elif path == "/api/meta":
                store = _store(self)
                _send_json(self, 200, {
                    "studio": self.server_version,
                    "build": _studio_build(),
                    "workspace": str(self._workspace()),
                    "database": str(store.path),
                    "schema_version": store.schema_version(),
                })
            elif path == "/api/settings":
                _send_json(self, 200, {"settings": _store(self).get_studio_selection()})
            elif path == "/api/tasks":
                _send_json(self, 200, {"tasks": _store(self).list_tasks()})
            elif (scoring_name := _scoring_task_name(path)) is not None:
                _send_json(self, 200, {"scoring": _scoring_payload(_store(self), scoring_name)})
            elif path.startswith("/api/runs/"):
                run_id = path[len("/api/runs/"):]
                _send_json(self, 200, _store(self).run_snapshot(run_id))
            else:
                _send_json(self, 404, {"error": f"unknown path: {path}"})
        except KeyError as exc:
            _send_json(self, 404, {"error": str(exc)})
        except Exception as exc:
            _send_json(self, 500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not _request_allowed(self):
                _send_json(self, 403, {"error": "request rejected: studio is localhost-only"})
                return
            path = urlparse(self.path).path
            if path == "/api/routes/refresh":
                catalog = RouteCatalog(db_path=_store(self).path)
                body = _read_json(self)
                provider = str(body.get("provider") or "").strip() if isinstance(body, dict) else ""
                if not provider:
                    _send_json(self, 200, {"refresh": catalog.refresh_all()})
                    return
                # Per-harness refresh: each harness owns its own model list via
                # its discovery command, so refresh exactly that one.
                from .providers.registry import HARNESS_SPECS

                spec_for_harness = next(
                    (candidate for candidate in HARNESS_SPECS if candidate.name == provider), None
                )
                if spec_for_harness is None:
                    raise ValueError(f"unknown harness: {provider}")
                try:
                    _send_json(self, 200, {"refresh": {provider: catalog.refresh_from_harness(spec_for_harness)}})
                except Exception as exc:
                    _send_json(self, 200, {"refresh": {f"{provider}_error": str(exc)}})
            elif path == "/api/tasks":
                body = _read_json(self)
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("body needs a task name")
                preset = str(body.get("preset") or "score").strip()
                store = _store(self)
                try:
                    existing = store.current_task_revision(name)
                except KeyError:
                    existing = None
                if existing is not None:
                    _send_json(self, 409, {
                        "error": (
                            f"task '{name}' already exists (revision {existing[:12]}); "
                            "load and edit it, or choose a new name"
                        ),
                    })
                    return
                spec = create_task_from_preset(name, preset_name=preset)
                view = scoring_view(spec)
                view["revision_id"] = store.register_task(spec)
                _send_json(self, 200, {"scoring": view})
            elif (source_name := _task_subpath(path, "/duplicate")) is not None:
                body = _read_json(self)
                new_name = str(body.get("name") or "").strip()
                if not new_name:
                    raise ValueError("body needs the new task name")
                store = _store(self)
                if not _task_exists(store, source_name):
                    _send_json(self, 404, {"error": f"task not found: {source_name}"})
                    return
                if _task_exists(store, new_name):
                    _send_json(self, 409, {"error": f"task '{new_name}' already exists; choose another name"})
                    return
                spec = duplicate_spec(store.get_task(source_name), new_name)
                view = scoring_view(spec)
                view["revision_id"] = store.register_task(spec)
                _send_json(self, 200, {"scoring": view})
            elif path == "/api/runs":
                body = _read_json(self)
                steps = body.get("steps") or []
                if not steps:
                    raise ValueError("body needs a non-empty steps list")
                store = _store(self)
                workspace = self._workspace()
                parent: str | None = None
                results = []
                for step in steps:
                    result = _run_step(store, workspace, step, parent)
                    parent = result["run_id"]
                    results.append(result)
                _send_json(self, 200, {"steps": results})
            else:
                _send_json(self, 404, {"error": f"unknown path: {path}"})
        except ValidationError as exc:
            _send_json(self, 400, {"error": _validation_message(exc)})
        except (ValueError, KeyError) as exc:
            _send_json(self, 400, {"error": str(exc)})
        except Exception as exc:
            _send_json(self, 500, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        try:
            if not _request_allowed(self):
                _send_json(self, 403, {"error": "request rejected: studio is localhost-only"})
                return
            path = urlparse(self.path).path
            if path == "/api/settings":
                body = _read_json(self)
                if not isinstance(body, dict):
                    raise ValueError("settings must be an object")
                unknown = sorted(set(body) - {"mode", "providers", "routes"})
                if unknown:
                    raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
                mode = str(body.get("mode") or "free")
                if mode not in {"free", "specific"}:
                    raise ValueError("mode must be 'free' or 'specific'")
                selection = {
                    "mode": mode,
                    "providers": sorted(str(p) for p in body.get("providers") or []),
                    "routes": sorted(str(r) for r in body.get("routes") or []),
                }
                if mode == "free":
                    selection["routes"] = []
                store = _store(self)
                # An empty selection used to clear what was saved and answer 200,
                # so the studio's autosave (which fires while nothing is ticked)
                # silently wiped a real selection. Refuse it instead; clearing is
                # explicit: `harness-fleet settings --clear`.
                if not selection["providers"]:
                    raise ValueError(
                        "no harness selected; pick at least one, or clear the saved selection "
                        "with `harness-fleet settings --clear`"
                    )
                _send_json(self, 200, {
                    "settings": selection,
                    "revision": store.save_studio_selection(selection),
                })
                return
            scoring_name = _scoring_task_name(path)
            if scoring_name is None:
                _send_json(self, 404, {"error": f"unknown path: {path}"})
                return
            body = _read_json(self)
            store = _store(self)
            spec = store.get_task(scoring_name)
            previous = store.current_task_revision(scoring_name)
            # Optimistic concurrency: a stale tab must not overwrite a newer
            # revision it never saw. "*" and an absent header keep the plain
            # CLI/HTTP path working.
            expected = (self.headers.get("If-Match") or "").strip().strip('"')
            if expected and expected != "*" and expected != previous:
                _send_json(self, 412, {
                    "error": (
                        f"task '{scoring_name}' changed elsewhere (now {previous[:12]}); "
                        "reload it before saving"
                    ),
                })
                return
            revised = apply_scoring_edit(spec, body)
            revision_id = store.register_task(revised)
            view = scoring_view(revised)
            view["revision_id"] = revision_id
            view["previous_revision_id"] = previous
            _send_json(self, 200, {"scoring": view})
        except ValidationError as exc:
            _send_json(self, 400, {"error": _validation_message(exc)})
        except KeyError as exc:
            _send_json(self, 404, {"error": str(exc)})
        except ValueError as exc:
            _send_json(self, 400, {"error": str(exc)})
        except Exception as exc:
            _send_json(self, 500, {"error": str(exc)})


def run_studio_server(
    workspace_root: str | Path = ".",
    db_path: str | Path | None = None,
    port: int = 8080,
) -> None:
    """Serve the studio UI on localhost until interrupted."""
    import os as _os

    workspace = Path(workspace_root).expanduser().resolve()
    if db_path is None:
        configured = _os.environ.get("HARNESS_FLEET_DB")
        resolved = Path(configured).expanduser() if configured else workspace / "harness-fleet.db"
    else:
        resolved = Path(db_path).expanduser()
    server = ThreadingHTTPServer(("127.0.0.1", port), StudioHandler)
    server.workspace_root = str(workspace)  # type: ignore[attr-defined]
    server.db_path = str(resolved)  # type: ignore[attr-defined]
    print(f"harness studio at http://127.0.0.1:{server.server_port} (workspace {workspace})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

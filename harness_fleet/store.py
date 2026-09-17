"""SQLite control plane adapted from the proven career research worker queue."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    ID_PATTERN,
    BatchStatusCounts,
    ExtractedItem,
    PackedBatch,
    ProviderReceipt,
    RouteInfo,
    RoutePolicy,
    RouteStatusSummary,
    RunStatusReport,
    TaskSpec,
    WorkerSessionRecord,
)

SCHEMA_VERSION = "5"
STREAMING_INPUT_DIGEST = "__streaming_input_pending__"
MAX_NON_COUNTING_RETRIES = 12

SCHEMA_PATH = Path(__file__).resolve().parent / "migrations" / "001_control_plane.sql"
SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")
MIGRATION_002_PATH = Path(__file__).resolve().parent / "migrations" / "002_intelligence_and_policy.sql"
MIGRATION_003_PATH = Path(__file__).resolve().parent / "migrations" / "003_profiles.sql"
MIGRATION_004_PATH = Path(__file__).resolve().parent / "migrations" / "004_studio_settings.sql"
#: Widens `profile_kind` on databases built before a partner profile had a kind
#: of its own. SQLite cannot alter a CHECK, so this rebuilds and carries rows;
#: migrate() applies it only when the live DDL predates the widening.
MIGRATION_005_PATH = Path(__file__).resolve().parent / "migrations" / "005_profile_kinds.sql"

# Tables added after the numbered migrations were frozen. They are defined here
# rather than inline in migrate() so that migrate() and
# get_database_schema_sql() read the SAME text: an introspecting agent must see
# every table that actually exists, and duplicating the DDL is how
# score_history/route_claim_bias went missing from the advertised schema.
EXTRA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS score_history(
    run_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    score REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_history_entity ON score_history(entity, created_at);
CREATE TABLE IF NOT EXISTS route_claim_bias(
    task_name TEXT NOT NULL,
    route_id TEXT NOT NULL,
    bias REAL NOT NULL DEFAULT 0.0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_name, route_id)
);
"""


def get_database_schema_sql() -> str:
    parts = [SCHEMA_SQL]
    if MIGRATION_002_PATH.is_file():
        parts.append(MIGRATION_002_PATH.read_text(encoding="utf-8"))
    if MIGRATION_003_PATH.is_file():
        parts.append(MIGRATION_003_PATH.read_text(encoding="utf-8"))
    if MIGRATION_004_PATH.is_file():
        parts.append(MIGRATION_004_PATH.read_text(encoding="utf-8"))
    parts.append(EXTRA_SCHEMA_SQL)
    return "\n".join(parts)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_iso_ts(value: Any) -> float | None:
    """Parse ISO-8601 / SQLite datetime text to epoch seconds. Returns None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # SQLite CURRENT_TIMESTAMP style "YYYY-MM-DD HH:MM:SS" has no tz; assume UTC.
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _decay_weight(ts: float | None, now_ts: float, half_life_hours: float) -> float:
    if half_life_hours <= 0 or ts is None:
        return 1.0
    age_hours = max(0.0, (now_ts - ts) / 3600.0)
    return 0.5 ** (age_hours / half_life_hours)


def default_db_path() -> Path:
    configured = os.environ.get("HARNESS_FLEET_DB")
    return Path(configured).expanduser() if configured else Path.cwd() / "harness-fleet.db"


class ManagedConnection(sqlite3.Connection):
    """SQLite connection subclass that guarantees file descriptor and lock release on context exit."""
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()


class _StreamingRunWriter:
    """Keep run creation and streamed batch ingestion in one SQLite transaction."""

    def __init__(
        self,
        store: HarnessStore,
        *,
        run_id: str,
        task_revision_id: str,
        input_path: str,
        input_digest: str,
        total_items: int,
        max_attempts: int,
        batch_size: int,
        output_path: str,
        policy: RoutePolicy | None,
        profile_revision_id: str | None,
        parent_run_id: str | None = None,
    ):
        self.store = store
        self.run_id = run_id
        self.task_revision_id = task_revision_id
        self.input_path = input_path
        self.input_digest = input_digest
        self.total_items = total_items
        self.max_attempts = max_attempts
        self.batch_size = batch_size
        self.output_path = output_path
        self.policy = policy
        self.profile_revision_id = profile_revision_id
        self.parent_run_id = parent_run_id
        self.connection: sqlite3.Connection | None = None
        self.finalized = input_digest != STREAMING_INPUT_DIGEST

    def __enter__(self) -> _StreamingRunWriter:
        self.connection = self.store.connect()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.store._create_run_connection(
                self.connection,
                run_id=self.run_id,
                task_revision_id=self.task_revision_id,
                input_path=self.input_path,
                input_digest=self.input_digest,
                total_items=self.total_items,
                max_attempts=self.max_attempts,
                batch_size=self.batch_size,
                output_path=self.output_path,
                policy=self.policy,
                profile_revision_id=self.profile_revision_id,
                parent_run_id=self.parent_run_id,
            )
            return self
        except Exception:
            self.connection.rollback()
            self.connection.close()
            self.connection = None
            raise

    def enqueue_batch(self, batch: dict[str, Any], position: int, max_attempts_per_batch: int) -> None:
        if self.connection is None:
            raise RuntimeError("streaming run is not open")
        self.store._enqueue_batch_connection(
            self.connection,
            self.run_id,
            batch,
            max_attempts_per_batch,
            position,
        )

    def finalize(self, input_digest: str, total_items: int) -> None:
        if self.connection is None:
            raise RuntimeError("streaming run is not open")
        self.store._complete_streaming_input_connection(
            self.connection, self.run_id, input_digest, total_items
        )
        self.finalized = True

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:  # type: ignore[exit-return]
        if self.connection is None:
            return False
        try:
            if exc_type is None and self.finalized:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
            self.connection = None
        return False


#: The onboarding documents the store keeps, one active revision per kind. Every
#: kind is first-class: a partner profile is stored as a partner profile rather
#: than flattened into an ICP, so the fields its own lane's questions need —
#: the target ecosystem, the service models, the delivery roles — survive the
#: round trip. A kind is what a lane points at, not a filename.
PROFILE_KINDS = ("ideal_company", "ideal_partner", "ideal_employer")


def check_profile_kind(kind: str) -> str:
    """The kind, or the error naming the kinds that exist."""
    if kind not in PROFILE_KINDS:
        raise ValueError(
            f"unsupported profile kind: {kind} (have: {', '.join(PROFILE_KINDS)})"
        )
    return kind


class HarnessStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0, factory=ManagedConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            # R21 forward-only cutover: rename the legacy meta table before
            # the fresh DDL runs so the renamed table (with its rows) survives
            # CREATE TABLE IF NOT EXISTS harness_meta as a no-op.
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "bulk_meta" in tables and "harness_meta" not in tables:
                connection.execute("ALTER TABLE bulk_meta RENAME TO harness_meta")
                tables.discard("bulk_meta")
                tables.add("harness_meta")
            connection.executescript(SCHEMA_SQL)
            if MIGRATION_002_PATH.is_file():
                connection.executescript(MIGRATION_002_PATH.read_text(encoding="utf-8"))
            if MIGRATION_003_PATH.is_file():
                connection.executescript(MIGRATION_003_PATH.read_text(encoding="utf-8"))
            if MIGRATION_004_PATH.is_file():
                connection.executescript(MIGRATION_004_PATH.read_text(encoding="utf-8"))
            # Widen `profile_kind` so a partner profile is stored as itself. The
            # live DDL says which kinds the table accepts, so it decides whether
            # the rebuild is owed: a database built from the widened 003 already
            # names `ideal_partner` and skips this entirely, which keeps the
            # rewrite off the path of every command.
            profile_ddl_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='profile_revisions'"
            ).fetchone()
            profile_ddl = str(profile_ddl_row["sql"] or "") if profile_ddl_row else ""
            if (
                profile_ddl
                and "ideal_partner" not in profile_ddl
                and MIGRATION_005_PATH.is_file()
            ):
                connection.executescript(MIGRATION_005_PATH.read_text(encoding="utf-8"))
            cols = [row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()]
            if "policy_json" not in cols:
                connection.execute("ALTER TABLE runs ADD COLUMN policy_json TEXT")
            if "profile_revision_id" not in cols:
                connection.execute("ALTER TABLE runs ADD COLUMN profile_revision_id TEXT")
            batch_cols = [row["name"] for row in connection.execute("PRAGMA table_info(batches)").fetchall()]
            if "non_counting_attempts" not in batch_cols:
                connection.execute(
                    "ALTER TABLE batches ADD COLUMN non_counting_attempts INTEGER NOT NULL DEFAULT 0"
                )
            # Calibration columns are nullable so pre-upgrade rows read back
            # as default calibration, which normalizes to the legacy digest.
            task_cols = [row["name"] for row in connection.execute("PRAGMA table_info(task_revisions)").fetchall()]
            for column, ddl in (
                ("checklist_json", "ALTER TABLE task_revisions ADD COLUMN checklist_json TEXT"),
                ("pass_score", "ALTER TABLE task_revisions ADD COLUMN pass_score INTEGER"),
                ("evidence_terms_json", "ALTER TABLE task_revisions ADD COLUMN evidence_terms_json TEXT"),
                ("candidate_top_n", "ALTER TABLE task_revisions ADD COLUMN candidate_top_n INTEGER"),
                ("source_weights_json", "ALTER TABLE task_revisions ADD COLUMN source_weights_json TEXT"),
                ("default_source_weight", "ALTER TABLE task_revisions ADD COLUMN default_source_weight REAL"),
                ("recency_half_lives_json", "ALTER TABLE task_revisions ADD COLUMN recency_half_lives_json TEXT"),
            ):
                if column not in task_cols:
                    connection.execute(ddl)
            run_cols = [row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()]
            if "parent_run_id" not in run_cols:
                connection.execute("ALTER TABLE runs ADD COLUMN parent_run_id TEXT")
            # Score history prices every verified record per run so rescore
            # rounds form a queryable trajectory per entity across runs.
            # Shared with get_database_schema_sql() via EXTRA_SCHEMA_SQL.
            connection.executescript(EXTRA_SCHEMA_SQL)
            # R21 cutover, part 2: a pre-rename database can reach this point
            # with BOTH tables (fresh harness_meta created above alongside the
            # legacy bulk_meta). Carry the version row forward, then drop the
            # legacy table. Guarded and idempotent: no-ops on new databases.
            legacy = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bulk_meta'"
            ).fetchone()
            if legacy is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO harness_meta(key,value) "
                    "SELECT key,value FROM bulk_meta WHERE key='schema_version'"
                )
                connection.execute("DROP TABLE bulk_meta")
            # R21 cutover, part 3: rewrite legacy task format values and
            # rebuild the task_revisions CHECK to the single new value.
            # Guarded and idempotent: skipped when the live DDL already
            # carries only the new value and no legacy rows remain.
            task_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_revisions'"
            ).fetchone()
            task_sql = str(task_sql_row["sql"]) if task_sql_row and task_sql_row["sql"] else ""
            needs_check_rebuild = (
                "free_fleet_task_v1" in task_sql or "bulk_lanes_task_v1" in task_sql
            )
            legacy_rows = connection.execute(
                "SELECT count(*) FROM task_revisions WHERE format_version IN "
                "('free_fleet_task_v1','bulk_lanes_task_v1')"
            ).fetchone()[0]
            if needs_check_rebuild:
                new_sql = re.sub(
                    r"CHECK\s*\(\s*format_version\s+IN\s*\([^)]*\)\s*\)",
                    "CHECK(format_version IN ('harness_fleet_task_v1'))",
                    task_sql,
                    count=1,
                )
                # Retarget the DDL at a build table; the live table keeps its
                # name until the rebuilt copy is verified.
                new_sql = re.sub(
                    r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?task_revisions\b",
                    "CREATE TABLE task_revisions_new",
                    new_sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
                # A transaction is already open (see the legacy bulk_meta DML
                # above), and SQLite ignores PRAGMA foreign_keys changes while
                # one is pending. defer_foreign_keys is valid in-transaction:
                # it moves the runs->task_revisions check to COMMIT, by which
                # point the rebuilt table exists again.
                connection.execute("PRAGMA defer_foreign_keys=ON")
                # SAVEPOINT (not BEGIN: a transaction may already be open) so
                # the DROP->RENAME window is atomic: any failure below leaves
                # the live task_revisions untouched.
                connection.execute("SAVEPOINT harness_cutover")
                try:
                    connection.execute("DROP TABLE IF EXISTS task_revisions_new")
                    connection.execute(new_sql)
                    cols = [
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(task_revisions)"
                        ).fetchall()
                    ]
                    col_list = ",".join(f'"{name}"' for name in cols)
                    select_list = ",".join(
                        "CASE WHEN \"format_version\" IN "
                        "('free_fleet_task_v1','bulk_lanes_task_v1') "
                        "THEN 'harness_fleet_task_v1' ELSE \"format_version\" END"
                        if name == "format_version"
                        else f'"{name}"'
                        for name in cols
                    )
                    connection.execute(
                        f'INSERT INTO task_revisions_new({col_list}) SELECT {select_list} FROM task_revisions'
                    )
                    connection.execute("DROP TABLE task_revisions")
                    connection.execute("ALTER TABLE task_revisions_new RENAME TO task_revisions")
                    connection.execute("RELEASE harness_cutover")
                except Exception:
                    connection.execute("ROLLBACK TO harness_cutover")
                    connection.execute("RELEASE harness_cutover")
                    raise
                finally:
                    connection.execute("PRAGMA defer_foreign_keys=OFF")
                # Re-create the append-only triggers dropped with the old table.
                connection.execute(
                    "CREATE TRIGGER IF NOT EXISTS task_revisions_no_update "
                    "BEFORE UPDATE ON task_revisions BEGIN SELECT RAISE(ABORT, 'task revisions are append-only'); END"
                )
                connection.execute(
                    "CREATE TRIGGER IF NOT EXISTS task_revisions_no_delete "
                    "BEFORE DELETE ON task_revisions BEGIN SELECT RAISE(ABORT, 'task revisions are append-only'); END"
                )
            elif legacy_rows:
                connection.execute(
                    "UPDATE task_revisions SET format_version='harness_fleet_task_v1' "
                    "WHERE format_version IN ('free_fleet_task_v1','bulk_lanes_task_v1')"
                )
            connection.execute(
                "INSERT INTO harness_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )

    def schema_version(self) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM harness_meta WHERE key='schema_version'").fetchone()
        return str(row["value"])

    def register_task(self, spec: TaskSpec) -> str:
        revision_id = digest_json(spec.revision_payload())
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO task_revisions(
                    revision_id,task_name,format_version,instructions,batch_size,max_slice_chars,
                    min_quote_chars,claims_schema_json,checklist_json,pass_score,
                    evidence_terms_json,candidate_top_n,source_weights_json,
                    default_source_weight,recency_half_lives_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    revision_id, spec.name, spec.format_version, spec.instructions, spec.batch_size,
                    spec.max_slice_chars, spec.min_quote_chars,
                    json.dumps(spec.claims_schema, separators=(",", ":")),
                    json.dumps(spec.checklist, separators=(",", ":")) if spec.checklist is not None else None,
                    spec.pass_score,
                    json.dumps(spec.evidence_terms, separators=(",", ":")) if spec.evidence_terms else None,
                    spec.candidate_top_n,
                    json.dumps(spec.source_weights, separators=(",", ":")) if spec.source_weights else None,
                    spec.default_source_weight,
                    json.dumps(spec.recency_half_lives, separators=(",", ":")) if spec.recency_half_lives else None,
                    now_iso(),
                ),
            )
            connection.execute(
                "INSERT INTO current_tasks(task_name,revision_id) VALUES(?,?) "
                "ON CONFLICT(task_name) DO UPDATE SET revision_id=excluded.revision_id",
                (spec.name, revision_id),
            )
        return revision_id

    def get_task(self, name: str) -> TaskSpec:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT r.* FROM current_tasks c JOIN task_revisions r ON r.revision_id=c.revision_id WHERE c.task_name=?",
                (name,),
            ).fetchone()
        if row is None:
            raise KeyError(f"task not found: {name}")
        return self._task_from_row(row)

    def get_task_revision(self, revision_id: str) -> TaskSpec:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            raise KeyError(f"task revision not found: {revision_id}")
        return self._task_from_row(row)

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskSpec:
        columns = set(row.keys())
        checklist_json = row["checklist_json"] if "checklist_json" in columns else None
        evidence_json = row["evidence_terms_json"] if "evidence_terms_json" in columns else None
        weights_json = row["source_weights_json"] if "source_weights_json" in columns else None
        default_weight = row["default_source_weight"] if "default_source_weight" in columns else None
        halves_json = row["recency_half_lives_json"] if "recency_half_lives_json" in columns else None
        return TaskSpec.model_validate({
            "format_version": row["format_version"],
            "name": row["task_name"],
            "instructions": row["instructions"],
            "batch_size": row["batch_size"],
            "max_slice_chars": row["max_slice_chars"],
            "min_quote_chars": row["min_quote_chars"],
            "claims_schema": json.loads(row["claims_schema_json"]),
            # Pre-upgrade rows predate calibration: NULLs rebuild as the
            # defaults whose normalized digest equals the legacy revision.
            "checklist": json.loads(checklist_json) if checklist_json else None,
            "pass_score": row["pass_score"] if "pass_score" in columns else None,
            "evidence_terms": json.loads(evidence_json) if evidence_json else [],
            "candidate_top_n": (
                row["candidate_top_n"]
                if "candidate_top_n" in columns and row["candidate_top_n"]
                else TaskSpec.model_fields["candidate_top_n"].default
            ),
            "source_weights": json.loads(weights_json) if weights_json else {},
            "default_source_weight": (
                float(default_weight)
                if default_weight is not None
                else TaskSpec.model_fields["default_source_weight"].default
            ),
            "recency_half_lives": json.loads(halves_json) if halves_json else {},
        })

    def record_score_history(self, run_id: str, item_id: str, entity: str, score: float) -> None:
        """Price one verified record into the entity score trajectory."""
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO score_history(run_id,item_id,entity,score,created_at) VALUES(?,?,?,?,?)",
                (run_id, item_id, entity, float(score), now_iso()),
            )

    def get_entity_history(self, entity: str) -> list[dict[str, Any]]:
        """Score trajectory for one entity across runs, oldest first."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT h.run_id,h.item_id,h.entity,h.score,h.created_at,r.parent_run_id
                   FROM score_history h LEFT JOIN runs r ON r.run_id=h.run_id
                   WHERE h.entity=? ORDER BY h.created_at ASC, h.rowid ASC""",
                (entity,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run_task(self, run_id: str) -> TaskSpec:
        with self.connect() as connection:
            row = connection.execute("SELECT task_revision_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return self.get_task_revision(str(row["task_revision_id"]))

    def current_task_revision(self, name: str) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT revision_id FROM current_tasks WHERE task_name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"task not found: {name}")
        return str(row["revision_id"])

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.task_name,c.revision_id,r.created_at,"
                "(r.checklist_json IS NOT NULL) AS scorable "
                "FROM current_tasks c "
                "JOIN task_revisions r ON r.revision_id=c.revision_id ORDER BY c.task_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_studio_selection(self, selection: dict[str, Any]) -> str:
        """Persist the studio's harness/model selection and make it active.

        A single row in studio_settings; the revision id is a content digest so
        a run can be audited against the exact selection that produced it.
        """
        mode = str(selection.get("mode") or "free")
        if mode not in {"free", "specific"}:
            raise ValueError("mode must be 'free' or 'specific'")
        providers = sorted(str(p) for p in selection.get("providers") or [])
        routes = sorted(str(r) for r in selection.get("routes") or []) if mode == "specific" else []
        revision_id = digest_json({"mode": mode, "providers": providers, "routes": routes})
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO studio_settings(id,mode,providers_json,routes_json,revision_id,updated_at)
                   VALUES(1,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     mode=excluded.mode, providers_json=excluded.providers_json,
                     routes_json=excluded.routes_json, revision_id=excluded.revision_id,
                     updated_at=excluded.updated_at""",
                (mode, json.dumps(providers), json.dumps(routes), revision_id, now_iso()),
            )
        return revision_id

    def get_studio_selection(self) -> dict[str, Any] | None:
        """The studio's persisted selection, or None when it has never been set."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT mode,providers_json,routes_json,revision_id,updated_at FROM studio_settings WHERE id=1"
            ).fetchone()
        if row is None:
            return None
        try:
            providers = json.loads(row["providers_json"])
            routes = json.loads(row["routes_json"])
        except json.JSONDecodeError:
            return None
        return {
            "mode": row["mode"],
            "providers": [str(p) for p in providers] if isinstance(providers, list) else [],
            "routes": [str(r) for r in routes] if isinstance(routes, list) else [],
            "revision": row["revision_id"],
            "updated_at": row["updated_at"],
        }

    def clear_studio_selection(self) -> None:
        """Forget the studio's persisted selection."""
        with self.connect() as connection:
            connection.execute("DELETE FROM studio_settings WHERE id=1")

    def get_studio_revision(self) -> str | None:
        """Content digest of the active studio selection, if any."""
        selection = self.get_studio_selection()
        return str(selection["revision"]) if selection else None

    def save_profile(self, profile: Any, profile_kind: str | None = None) -> str:
        """Persist an immutable profile revision and make it active."""
        kind = profile_kind or getattr(profile, "profile_kind", "ideal_company")
        check_profile_kind(kind)
        payload = profile.model_dump(mode="json", by_alias=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        revision_id = digest_json({"profile_kind": kind, "profile": payload})
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO profile_revisions(
                    revision_id,profile_kind,profile_name,profile_version,profile_json,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    revision_id,
                    kind,
                    str(getattr(profile, "profile_name", kind)),
                    str(getattr(profile, "version", "1.0.0")),
                    encoded,
                    now_iso(),
                ),
            )
            connection.execute(
                """INSERT INTO active_profiles(profile_kind,revision_id,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(profile_kind) DO UPDATE SET
                       revision_id=excluded.revision_id,
                       updated_at=excluded.updated_at""",
                (kind, revision_id, now_iso()),
            )
        return revision_id

    def load_profile(self, profile_kind: str = "ideal_company") -> Any | None:
        """Load the active typed profile for a profile kind, if one exists."""
        check_profile_kind(profile_kind)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.profile_json
                   FROM active_profiles a
                   JOIN profile_revisions r ON r.revision_id=a.revision_id
                   WHERE a.profile_kind=?""",
                (profile_kind,),
            ).fetchone()
        return self._profile_from_row(row, profile_kind)

    def load_profile_revision(self, revision_id: str, profile_kind: str = "ideal_company") -> Any | None:
        """Load one immutable profile revision by ID for run resumption."""
        check_profile_kind(profile_kind)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT profile_json FROM profile_revisions WHERE revision_id=? AND profile_kind=?",
                (revision_id, profile_kind),
            ).fetchone()
        return self._profile_from_row(row, profile_kind)

    @staticmethod
    def _profile_from_row(row: Any, profile_kind: str) -> Any | None:
        if row is None:
            return None
        # Each kind loads back as its own contract, so a caller that stored an
        # IPP reads IPP fields rather than a flattened ICP.
        if profile_kind == "ideal_company":
            from .profile import IdealCompanyProfile

            return IdealCompanyProfile.model_validate(json.loads(row["profile_json"]))
        if profile_kind == "ideal_partner":
            from .partner import IdealPartnerProfile

            return IdealPartnerProfile.model_validate(json.loads(row["profile_json"]))
        from career_fleet.profile import IdealEmployerProfile

        return IdealEmployerProfile.model_validate(json.loads(row["profile_json"]))

    def active_profile_revision_id(self, profile_kind: str = "ideal_company") -> str | None:
        """Return the active profile revision selected for a profile kind."""
        check_profile_kind(profile_kind)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT revision_id FROM active_profiles WHERE profile_kind=?",
                (profile_kind,),
            ).fetchone()
        return str(row["revision_id"]) if row else None

    def upsert_route(self, route: RouteInfo) -> None:
        value = route.model_dump(mode="json")
        observed_at = now_iso()
        observation_id = digest_json({"route": value, "observed_at": observed_at})
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO route_observations(
                    observation_id,route_id,route_json,price_state,enabled,provider,observed_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    observation_id, route.id, json.dumps(value, separators=(",", ":")), route.price_state,
                    int(route.enabled), route.provider, observed_at,
                ),
            )
            connection.execute(
                """INSERT INTO current_routes(route_id,observation_id) VALUES(?,?)
                   ON CONFLICT(route_id) DO UPDATE SET observation_id=excluded.observation_id""",
                (route.id, observation_id),
            )
    def route_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT count(*) FROM current_routes").fetchone()[0])

    def list_routes(
        self,
        provider: str | None = None,
        observed_zero_only: bool = True,
        include_disabled: bool = False,
    ) -> list[RouteInfo]:
        query = """SELECT o.route_json FROM current_routes c
                   JOIN route_observations o ON o.observation_id=c.observation_id WHERE 1=1"""
        params: list[Any] = []
        if not include_disabled:
            query += " AND o.enabled=1"
        if provider:
            query += " AND o.provider=?"
            params.append(provider)
        if observed_zero_only:
            query += " AND o.price_state='price_observed_zero'"
        query += " ORDER BY o.route_id"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [RouteInfo.model_validate_json(row["route_json"]) for row in rows]

    def create_run(
        self,
        run_id: str,
        task_revision_id: str,
        input_path: str,
        input_digest: str,
        total_items: int,
        max_attempts: int,
        batch_size: int,
        output_path: str,
        policy: RoutePolicy | None = None,
        profile_revision_id: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._create_run_connection(
                connection,
                run_id=run_id,
                task_revision_id=task_revision_id,
                input_path=input_path,
                input_digest=input_digest,
                total_items=total_items,
                max_attempts=max_attempts,
                batch_size=batch_size,
                output_path=output_path,
                policy=policy,
                profile_revision_id=profile_revision_id,
            )

    def _create_run_connection(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        task_revision_id: str,
        input_path: str,
        input_digest: str,
        total_items: int,
        max_attempts: int,
        batch_size: int,
        output_path: str,
        policy: RoutePolicy | None = None,
        profile_revision_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        if not re.fullmatch(ID_PATTERN, run_id) or len(run_id) > 128:
            raise ValueError("run_id must use 1-128 letters, numbers, dots, underscores, or hyphens")
        if parent_run_id is not None and (
            not re.fullmatch(ID_PATTERN, parent_run_id) or len(parent_run_id) > 128
        ):
            raise ValueError("parent_run_id must use 1-128 letters, numbers, dots, underscores, or hyphens")
        policy_json = json.dumps(policy.model_dump(mode="json"), separators=(",", ":")) if policy else None
        if profile_revision_id is not None:
            profile_row = connection.execute(
                "SELECT profile_kind FROM profile_revisions WHERE revision_id=?",
                (profile_revision_id,),
            ).fetchone()
            if profile_row is None:
                raise ValueError(f"profile revision does not exist: {profile_revision_id}")
            if profile_row["profile_kind"] != "ideal_company":
                raise ValueError(f"profile revision is not an Ideal Company Profile: {profile_revision_id}")
        existing = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            expected = (task_revision_id, profile_revision_id, input_digest, total_items, max_attempts, batch_size, parent_run_id)
            actual = (
                existing["task_revision_id"], existing["profile_revision_id"], existing["input_digest"], existing["total_items"],
                existing["max_attempts"], existing["batch_size"],
                existing["parent_run_id"] if "parent_run_id" in existing.keys() else None,
            )
            if actual != expected:
                raise ValueError(f"run_id already exists with different inputs or limits: {run_id}")
            return
        connection.execute(
            """INSERT INTO runs(
                run_id,task_revision_id,profile_revision_id,input_path,input_digest,status,total_items,max_attempts,
                attempts_used,batch_size,output_path,policy_json,parent_run_id,created_at
            ) VALUES(?,?,?,?,?,'running',?,?,0,?,?,?,?,?)""",
            (run_id, task_revision_id, profile_revision_id, input_path, input_digest, total_items, max_attempts, batch_size, output_path, policy_json, parent_run_id, now_iso()),
        )

    def streaming_run(
        self,
        *,
        run_id: str,
        task_revision_id: str,
        input_path: str,
        input_digest: str,
        total_items: int,
        max_attempts: int,
        batch_size: int,
        output_path: str,
        policy: RoutePolicy | None = None,
        profile_revision_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> _StreamingRunWriter:
        """Return an atomic writer for bounded, durable batch ingestion."""
        return _StreamingRunWriter(
            self,
            run_id=run_id,
            task_revision_id=task_revision_id,
            input_path=input_path,
            input_digest=input_digest,
            total_items=total_items,
            max_attempts=max_attempts,
            batch_size=batch_size,
            output_path=output_path,
            policy=policy,
            profile_revision_id=profile_revision_id,
            parent_run_id=parent_run_id,
        )

    @staticmethod
    def _complete_streaming_input_connection(
        connection: sqlite3.Connection,
        run_id: str,
        input_digest: str,
        total_items: int,
    ) -> None:
        row = connection.execute(
            "SELECT input_digest,total_items FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        if row["input_digest"] != STREAMING_INPUT_DIGEST:
            if row["input_digest"] == input_digest and row["total_items"] == total_items:
                return
            raise ValueError(f"run {run_id} already has a finalized input manifest")
        connection.execute(
            "UPDATE runs SET input_digest=?,total_items=? WHERE run_id=?",
            (input_digest, total_items, run_id),
        )

    def enqueue_batches(
        self,
        run_id: str,
        batches: Iterable[dict[str, Any]],
        max_attempts_per_batch: int,
        position_offset: int = 0,
    ) -> None:
        if max_attempts_per_batch <= 0:
            raise ValueError("max_attempts_per_batch must be greater than 0")
        if position_offset < 0:
            raise ValueError("position_offset must be non-negative")
        with self.connect() as connection:
            for position, raw_batch in enumerate(batches, start=position_offset):
                self._enqueue_batch_connection(
                    connection, run_id, raw_batch, max_attempts_per_batch, position
                )

    @staticmethod
    def _enqueue_batch_connection(
        connection: sqlite3.Connection,
        run_id: str,
        raw_batch: dict[str, Any],
        max_attempts_per_batch: int,
        position: int,
    ) -> None:
        batch = PackedBatch.model_validate(raw_batch).model_dump(mode="json")
        connection.execute(
            """INSERT OR IGNORE INTO batches(
                run_id,batch_id,position,item_ids_json,payload_json,status,attempts,max_attempts
            ) VALUES(?,?,?,?,?,'pending',0,?)""",
            (
                run_id, batch["batch_id"], position,
                json.dumps([item["item_id"] for item in batch["items"]]),
                json.dumps(batch, ensure_ascii=False, separators=(",", ":")), max_attempts_per_batch,
            ),
        )

    def lease_batch(self, run_id: str, worker_id: str, lease_timeout_seconds: int = 300) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT max_attempts,attempts_used FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            if run["attempts_used"] >= run["max_attempts"]:
                connection.commit()
                return None
            row = connection.execute(
                """SELECT * FROM batches
                   WHERE run_id=? AND attempts - non_counting_attempts < max_attempts
                     AND non_counting_attempts < ?
                     AND (status='pending' OR (status='leased' AND leased_at < datetime('now', ?)))
                   ORDER BY position LIMIT 1""",
                (run_id, MAX_NON_COUNTING_RETRIES, f"-{lease_timeout_seconds} seconds"),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            attempt_number = int(row["attempts"]) + 1
            attempt_id = f"{run_id}:{row['batch_id']}:{attempt_number}"
            connection.execute(
                """UPDATE batches SET status='leased',attempts=?,lease_owner=?,leased_at=CURRENT_TIMESTAMP,error=NULL
                   WHERE run_id=? AND batch_id=?""",
                (attempt_number, worker_id, run_id, row["batch_id"]),
            )
            connection.execute("UPDATE runs SET attempts_used=attempts_used+1,status='running' WHERE run_id=?", (run_id,))
            connection.execute(
                """INSERT OR REPLACE INTO batch_attempts(
                    attempt_id,run_id,batch_id,attempt_number,worker_id,status,started_at
                ) VALUES(?,?,?,?,?,'leased',?)""",
                (attempt_id, run_id, row["batch_id"], attempt_number, worker_id, now_iso()),
            )
            connection.commit()
            return {
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "batch": json.loads(row["payload_json"]),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: ProviderReceipt,
        run_id: str | None,
        batch_id: str | None,
    ) -> None:
        payload = receipt.model_dump(mode="json")
        connection.execute(
            """INSERT OR IGNORE INTO model_runs(
                receipt_id,run_id,batch_id,provider,requested_route,status,cost,cost_status,usage_json,
                error,duration_seconds,receipt_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                receipt.id, run_id, batch_id, receipt.provider, receipt.requested_route, receipt.status,
                receipt.cost, receipt.cost_status, json.dumps(payload["usage"]), receipt.error,
                receipt.duration_seconds, json.dumps(payload, separators=(",", ":")), now_iso(),
            ),
        )

    def complete_batch(
        self,
        run_id: str,
        attempt_id: str,
        worker_id: str,
        results: list[dict[str, Any]],
        receipt: ProviderReceipt,
    ) -> None:
        batch_id = attempt_id.rsplit(":", 2)[-2]
        validated_results = [ExtractedItem.model_validate(result).model_dump(mode="json") for result in results]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT b.lease_owner,b.status,b.item_ids_json,t.* FROM batches b
                   JOIN runs r ON r.run_id=b.run_id
                   JOIN task_revisions t ON t.revision_id=r.task_revision_id
                   WHERE b.run_id=? AND b.batch_id=?""",
                (run_id, batch_id),
            ).fetchone()
            if row is None or row["status"] != "leased" or row["lease_owner"] != worker_id:
                raise ValueError("batch lease is not owned by this worker")
            if [result["item_id"] for result in validated_results] != json.loads(row["item_ids_json"]):
                raise ValueError("verified result IDs or order do not match the leased batch")
            task = self._task_from_row(row)
            for result in validated_results:
                task.validate_extracted_item(result)
            self._insert_receipt(connection, receipt, run_id, batch_id)
            completed = now_iso()
            result_id = digest_json({"run_id": run_id, "batch_id": batch_id, "results": validated_results, "receipt_id": receipt.id})
            connection.execute(
                """INSERT OR IGNORE INTO batch_results(
                    result_id,run_id,batch_id,result_json,receipt_id,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (result_id, run_id, batch_id, json.dumps(validated_results, ensure_ascii=False, separators=(",", ":")), receipt.id, completed),
            )
            connection.execute(
                """INSERT INTO current_batch_results(run_id,batch_id,result_id) VALUES(?,?,?)
                   ON CONFLICT(run_id,batch_id) DO UPDATE SET result_id=excluded.result_id""",
                (run_id, batch_id, result_id),
            )
            connection.execute(
                """UPDATE batches SET status='verified',error=NULL,non_counting_attempts=0,completed_at=?
                   WHERE run_id=? AND batch_id=?""",
                (completed, run_id, batch_id),
            )
            connection.execute(
                """UPDATE batch_attempts SET status='verified',receipt_id=?,completed_at=? WHERE attempt_id=?""",
                (receipt.id, completed, attempt_id),
            )

    def fail_batch(
        self,
        run_id: str,
        attempt_id: str,
        worker_id: str,
        error: str,
        receipt: ProviderReceipt | None,
    ) -> None:
        batch_id = attempt_id.rsplit(":", 2)[-2]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts,non_counting_attempts,max_attempts,lease_owner,status FROM batches WHERE run_id=? AND batch_id=?",
                (run_id, batch_id),
            ).fetchone()
            if row is None or row["status"] != "leased" or row["lease_owner"] != worker_id:
                raise ValueError("batch lease is not owned by this worker")
            if receipt is not None:
                self._insert_receipt(connection, receipt, run_id, batch_id)
            counting_attempts = int(row["attempts"]) - int(row["non_counting_attempts"])
            next_status = "failed" if counting_attempts >= row["max_attempts"] else "pending"
            completed = now_iso()
            connection.execute(
                """UPDATE batches SET status=?,error=?,lease_owner=NULL,leased_at=NULL,
                   completed_at=CASE WHEN ?='failed' THEN ? ELSE NULL END WHERE run_id=? AND batch_id=?""",
                (next_status, error, next_status, completed, run_id, batch_id),
            )
            connection.execute(
                """UPDATE batch_attempts SET status='failed',receipt_id=?,error=?,completed_at=? WHERE attempt_id=?""",
                (receipt.id if receipt else None, error, completed, attempt_id),
            )

    def save_sessions(self, run_id: str, sessions: dict[str, dict[str, Any]]) -> None:
        with self.connect() as connection:
            for session_id, raw_payload in sessions.items():
                payload = WorkerSessionRecord.model_validate(raw_payload).model_dump(mode="json")
                if payload["session_id"] != session_id:
                    raise ValueError("worker session key does not match session_id")
                connection.execute(
                    """INSERT INTO worker_sessions(run_id,session_id,session_json) VALUES(?,?,?)
                       ON CONFLICT(run_id,session_id) DO UPDATE SET session_json=excluded.session_json""",
                    (run_id, session_id, json.dumps(payload, separators=(",", ":"))),
                )

    def finalize_run(self, run_id: str) -> str:
        with self.connect() as connection:
            counts = {row["status"]: row["count"] for row in connection.execute(
                "SELECT status,count(*) AS count FROM batches WHERE run_id=? GROUP BY status", (run_id,)
            )}
            run = connection.execute("SELECT max_attempts,attempts_used FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if counts.get("pending", 0) or counts.get("leased", 0):
                status = "budget_exhausted" if run["attempts_used"] >= run["max_attempts"] else "completed_with_failures"
            elif counts.get("failed", 0):
                status = "completed_with_failures"
            else:
                status = "completed"
            connection.execute("UPDATE runs SET status=?,finished_at=? WHERE run_id=?", (status, now_iso(), run_id))
        return status

    def run_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            batch_rows = connection.execute("SELECT * FROM batches WHERE run_id=? ORDER BY position", (run_id,)).fetchall()
            result_rows = connection.execute(
                """SELECT c.batch_id,r.result_json,r.receipt_id FROM current_batch_results c
                   JOIN batch_results r ON r.result_id=c.result_id WHERE c.run_id=?""",
                (run_id,),
            ).fetchall()
            sessions = connection.execute("SELECT session_id,session_json FROM worker_sessions WHERE run_id=?", (run_id,)).fetchall()
            receipts = {
                row["receipt_id"]: json.loads(row["receipt_json"])
                for row in connection.execute(
                    "SELECT receipt_id,receipt_json FROM model_runs WHERE run_id=? ORDER BY created_at,receipt_id",
                    (run_id,),
                )
            }
        current_results = {row["batch_id"]: row for row in result_rows}
        batches = {}
        for row in batch_rows:
            current_result = current_results.get(row["batch_id"])
            receipt = receipts.get(current_result["receipt_id"]) if current_result else None
            batches[row["batch_id"]] = {
                "batch_id": row["batch_id"],
                "item_ids": json.loads(row["item_ids_json"]),
                "status": row["status"],
                "attempts": row["attempts"],
                "result": json.loads(current_result["result_json"]) if current_result else None,
                "error": row["error"],
                "receipt": receipt,
                "completed_at": row["completed_at"],
            }
        task = self.get_task_revision(str(run["task_revision_id"]))
        policy_data = json.loads(run["policy_json"]) if "policy_json" in run.keys() and run["policy_json"] else None
        return {
            "format_version": "harness_fleet_run_v1",
            "run_id": run["run_id"], "created_at": run["created_at"], "finished_at": run["finished_at"],
            "task": task.model_dump(mode="json", by_alias=True),
            "task_revision": run["task_revision_id"],
            "profile_revision_id": run["profile_revision_id"] if "profile_revision_id" in run.keys() else None,
            "parent_run_id": run["parent_run_id"] if "parent_run_id" in run.keys() else None,
            "status": run["status"], "total_items": run["total_items"], "max_attempts": run["max_attempts"],
            "attempts_used": run["attempts_used"], "input_path": run["input_path"],
            "input_digest": run["input_digest"], "output_path": run["output_path"], "batches": batches,
            "policy": policy_data,
            "sessions": {row["session_id"]: json.loads(row["session_json"]) for row in sessions},
            "model_runs": list(receipts.values()),
        }

    def run_exists(self, run_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None

    def model_run_count(self, run_id: str) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT count(*) FROM model_runs WHERE run_id=?", (run_id,)).fetchone()[0])

    def release_lease(self, run_id: str, attempt_id: str, worker_id: str, reason: str = "") -> None:
        """Release a leased batch back to pending without consuming an attempt."""
        batch_id = attempt_id.rsplit(":", 2)[-2]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts,non_counting_attempts,lease_owner,status FROM batches WHERE run_id=? AND batch_id=?",
                (run_id, batch_id),
            ).fetchone()
            if row is None or row["status"] != "leased" or row["lease_owner"] != worker_id:
                return
            next_non_counting_attempts = int(row["non_counting_attempts"]) + 1
            exhausted = next_non_counting_attempts >= MAX_NON_COUNTING_RETRIES
            next_status = "failed" if exhausted else "pending"
            next_error = (
                f"{reason or 'Non-counting retry'}; retry cap reached ({MAX_NON_COUNTING_RETRIES})"
                if exhausted else reason
            )
            connection.execute(
                """UPDATE batches SET status=?,lease_owner=NULL,leased_at=NULL,error=?,
                   non_counting_attempts=?,completed_at=CASE WHEN ?='failed' THEN ? ELSE NULL END
                   WHERE run_id=? AND batch_id=?""",
                (next_status, next_error or None, next_non_counting_attempts, next_status, now_iso(), run_id, batch_id),
            )
            connection.execute("UPDATE runs SET attempts_used=max(0, attempts_used-1) WHERE run_id=?", (run_id,))
            connection.execute(
                """UPDATE batch_attempts SET status='failed',error=?,completed_at=? WHERE attempt_id=?""",
                (next_error or "Lease released", now_iso(), attempt_id),
            )

    def reset_leased_batches(self, run_id: str, lease_timeout_seconds: int = 300) -> int:
        """Recover expired leases without taking work away from active workers."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT batch_id, attempts FROM batches WHERE run_id=? AND status='leased'
                   AND leased_at < datetime('now', ?)""", (run_id, f"-{lease_timeout_seconds} seconds")
            ).fetchall()
            count = len(rows)
            for row in rows:
                connection.execute(
                    """UPDATE batches SET status='pending', lease_owner=NULL, leased_at=NULL
                       WHERE run_id=? AND batch_id=?""",
                    (run_id, row["batch_id"]),
                )
                connection.execute(
                    """UPDATE batch_attempts SET status='failed', error='Session interrupted or abandoned', completed_at=?
                       WHERE run_id=? AND batch_id=? AND status='leased'""",
                    (now_iso(), run_id, row["batch_id"]),
                )
            return count

    def set_cooldown(self, route_id: str, cooldown_until: float, reason: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO route_cooldowns(route_id, cooldown_until, reason, created_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(route_id) DO UPDATE SET
                   cooldown_until=excluded.cooldown_until, reason=excluded.reason, created_at=excluded.created_at""",
                (route_id, cooldown_until, reason, now_iso()),
            )

    def get_active_cooldowns(self) -> dict[str, float]:
        now = time.time()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT route_id, cooldown_until FROM route_cooldowns WHERE cooldown_until > ?",
                (now,),
            ).fetchall()
        return {row["route_id"]: float(row["cooldown_until"]) for row in rows}

    def is_route_cooled_down(self, route_id: str) -> bool:
        now = time.time()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM route_cooldowns WHERE route_id=? AND cooldown_until > ?",
                (route_id, now),
            ).fetchone()
        return row is not None

    def clear_expired_cooldowns(self) -> int:
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM route_cooldowns WHERE cooldown_until <= ?", (now,))
            return cursor.rowcount

    def get_consecutive_rate_limits(self, route_id: str) -> int:
        """Count consecutive recent rate limits for a route before a verified success."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT outcome, verified, error_type
                   FROM inference_attempts
                   WHERE route_id = ?
                   ORDER BY created_at DESC, rowid DESC
                   LIMIT 20""",
                (route_id,),
            ).fetchall()
        consecutive = 0
        for r in rows:
            if r["error_type"] == "rate_limit" or r["outcome"] == "rate_limited":
                consecutive += 1
            elif r["verified"] == 1 or r["outcome"] == "verified":
                break
        return consecutive

    @staticmethod
    def backoff_delay(
        consecutive: int,
        base_delays: list[float] | None = None,
        max_delay: float = 600.0,
    ) -> float:
        """Pure backoff step: deterministic and unit-testable (jitter excluded).

        Default progression: 30s -> 60s -> 180s -> 600s. Saturates at the
        table end, then clamps to max_delay for custom tables.
        """
        delays = base_delays or [30.0, 60.0, 180.0, 600.0]
        step = min(max(consecutive, 1) - 1, len(delays) - 1)
        return min(delays[step], max_delay)

    def record_rate_limit_with_adaptive_backoff(
        self,
        route_id: str,
        reason: str = "",
        base_delays: list[float] | None = None,
        max_delay: float = 600.0,
        jitter: bool = True,
    ) -> float:
        """Escalate cooldown using exponential backoff based on consecutive rate limits.

        Default progression: 30s -> 60s -> 180s -> 600s + jitter.
        Returns the computed cooldown expiry timestamp.
        """
        consecutive = self.get_consecutive_rate_limits(route_id) + 1
        delay = self.backoff_delay(consecutive, base_delays, max_delay)
        if jitter:
            delay += random.uniform(0.0, 5.0)

        cooldown_until = time.time() + delay
        reason_msg = reason or f"Rate limited: 429 (consecutive: {consecutive})"
        self.set_cooldown(route_id, cooldown_until, reason_msg)
        return cooldown_until

    def get_active_cooldown_details(self) -> list[dict[str, Any]]:
        """Return detailed records of all currently cooling routes."""
        self.clear_expired_cooldowns()
        now = time.time()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT route_id, cooldown_until, reason, created_at
                   FROM route_cooldowns
                   WHERE cooldown_until > ?
                   ORDER BY cooldown_until ASC""",
                (now,),
            ).fetchall()
        return [
            {
                "route_id": r["route_id"],
                "cooldown_until": float(r["cooldown_until"]),
                "remaining_seconds": round(float(r["cooldown_until"]) - now, 1),
                "reason": r["reason"] or "Rate limited",
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def clear_cooldowns(self, route_id: str | None = None) -> int:
        """Clear active cooldown for a specific route or all routes."""
        with self.connect() as connection:
            if route_id:
                cur = connection.execute("DELETE FROM route_cooldowns WHERE route_id = ?", (route_id,))
            else:
                cur = connection.execute("DELETE FROM route_cooldowns")
            connection.commit()
            return cur.rowcount

    def record_route_eval(self, eval_data: dict[str, Any], *, keep_latest: int = 100) -> None:
        eval_id = eval_data.get("eval_id") or hashlib.sha256(f"{eval_data['task_name']}:{eval_data['route_id']}:{now_iso()}".encode()).hexdigest()[:16]
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO route_evaluations(
                    eval_id, task_name, route_id, provider, total_samples, schema_pass_count,
                    grounding_pass_count, correct_count, error_count, rate_limit_count,
                    avg_latency_seconds, composite_score, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eval_id,
                    eval_data["task_name"],
                    eval_data["route_id"],
                    eval_data.get("provider", "unknown"),
                    eval_data["total_samples"],
                    eval_data["schema_pass_count"],
                    eval_data["grounding_pass_count"],
                    eval_data.get("correct_count"),
                    eval_data["error_count"],
                    eval_data["rate_limit_count"],
                    eval_data["avg_latency_seconds"],
                    eval_data["composite_score"],
                    eval_data.get("created_at") or now_iso(),
                ),
            )
            # Bound table growth: scoring only reads the latest eval per
            # route, so retain the newest keep_latest rows per (task, route).
            if keep_latest is not None and keep_latest >= 0:
                connection.execute(
                    """DELETE FROM route_evaluations
                       WHERE task_name=? AND route_id=? AND eval_id NOT IN (
                           SELECT eval_id FROM route_evaluations
                           WHERE task_name=? AND route_id=?
                           ORDER BY created_at DESC, rowid DESC LIMIT ?
                       )""",
                    (
                        eval_data["task_name"], eval_data["route_id"],
                        eval_data["task_name"], eval_data["route_id"],
                        keep_latest,
                    ),
                )

    def get_route_evals(self, task_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM route_evaluations"
        params = []
        if task_name:
            query += " WHERE task_name=?"
            params.append(task_name)
        query += " ORDER BY created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_earliest_cooldown_expiry(self, route_ids: list[str] | None = None) -> float | None:
        now = time.time()
        query = "SELECT MIN(cooldown_until) FROM route_cooldowns WHERE cooldown_until > ?"
        params: list[Any] = [now]
        if route_ids:
            placeholders = ",".join("?" * len(route_ids))
            query += f" AND route_id IN ({placeholders})"
            params.extend(route_ids)
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
            val = row[0] if row else None
        return float(val) if val is not None else None

    def record_inference_attempt(self, attempt: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO inference_attempts(
                    attempt_id,run_id,batch_id,lease_attempt_number,route_id,provider,
                    task_name,started_at,duration_seconds,transport_status,parse_status,
                    schema_status,grounding_status,outcome,verified,error_type,
                    error_message,retry_after,cost,cost_status,usage_json,
                    counts_against_budget,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt["attempt_id"],
                    attempt.get("run_id"),
                    attempt.get("batch_id"),
                    attempt.get("lease_attempt_number"),
                    attempt["route_id"],
                    attempt.get("provider", "unknown"),
                    attempt.get("task_name"),
                    attempt.get("started_at", now_iso()),
                    attempt.get("duration_seconds"),
                    attempt.get("transport_status", "success"),
                    attempt.get("parse_status", "success"),
                    attempt.get("schema_status", "success"),
                    attempt.get("grounding_status", "success"),
                    attempt.get("outcome", "verified" if attempt.get("verified") else "failed"),
                    1 if attempt.get("verified") else 0,
                    attempt.get("error_type"),
                    attempt.get("error_message"),
                    attempt.get("retry_after"),
                    attempt.get("cost"),
                    attempt.get("cost_status"),
                    json.dumps(attempt.get("usage")) if attempt.get("usage") else None,
                    attempt.get("counts_against_budget", 1),
                    attempt.get("created_at", now_iso()),
                ),
            )

    def get_route_history_stats(
        self,
        task_name: str | None = None,
        half_life_hours: float = 72.0,
        now: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return time-decayed route stats.

        Weight per attempt is 0.5**(age_hours/half_life_hours). half_life_hours<=0
        disables decay (weight=1). Totals are effective sample sizes (floats).
        total_cost is a raw undecayed sum (accounting must not decay).
        Default 72h discounts stale free-tier behavior without regressing
        week-old batches to prior on every run.
        """
        now_ts = now if now is not None else time.time()
        with self.connect() as connection:
            has_attempts = connection.execute("SELECT count(*) FROM inference_attempts").fetchone()[0]
            if has_attempts > 0:
                query = """
                    SELECT route_id, provider, outcome, parse_status, schema_status,
                           grounding_status, transport_status, error_type,
                           duration_seconds, cost, started_at, created_at, task_name
                    FROM inference_attempts
                """
                params: list[Any] = []
                if task_name:
                    query += " WHERE task_name=?"
                    params.append(task_name)
                query += " ORDER BY created_at ASC"
                rows = connection.execute(query, params).fetchall()
                stats: dict[str, dict[str, Any]] = {}
                for r in rows:
                    rid = r["route_id"]
                    ts = _parse_iso_ts(r["started_at"]) or _parse_iso_ts(r["created_at"])
                    w = _decay_weight(ts, now_ts, half_life_hours)
                    entry = stats.get(rid)
                    if entry is None:
                        entry = {
                            "route_id": rid,
                            "provider": r["provider"],
                            "total": 0.0,
                            "completed": 0.0,
                            "malformed": 0.0,
                            "schema_violations": 0.0,
                            "grounding_failures": 0.0,
                            "rate_limits": 0.0,
                            "duration_weighted_sum": 0.0,
                            "duration_weight": 0.0,
                            "total_cost": 0.0,
                        }
                        stats[rid] = entry
                    entry["provider"] = r["provider"]
                    entry["total"] += w
                    if r["outcome"] == "verified":
                        entry["completed"] += w
                    if r["parse_status"] == "malformed_json":
                        entry["malformed"] += w
                    if r["schema_status"] == "schema_violation":
                        entry["schema_violations"] += w
                    if r["grounding_status"] == "grounding_failed":
                        entry["grounding_failures"] += w
                    if r["transport_status"] == "rate_limit" or r["error_type"] == "rate_limit":
                        entry["rate_limits"] += w
                    if r["duration_seconds"] is not None:
                        try:
                            dur = float(r["duration_seconds"])
                        except (TypeError, ValueError):
                            dur = 0.0
                        entry["duration_weighted_sum"] += w * max(0.0, dur)
                        entry["duration_weight"] += w
                    if r["cost"] is not None:
                        try:
                            entry["total_cost"] += float(r["cost"])
                        except (TypeError, ValueError):
                            pass
                for entry in stats.values():
                    dw = entry.pop("duration_weight")
                    dsum = entry.pop("duration_weighted_sum")
                    entry["avg_duration"] = float(dsum / dw) if dw > 0 else 0.0
                    entry["total_cost"] = float(entry["total_cost"])
                return stats

            # Fallback to model_runs for legacy data
            query = """
                SELECT requested_route, provider, status, error,
                       duration_seconds, cost, created_at, run_id
                FROM model_runs
            """
            params = []
            if task_name:
                query += " WHERE run_id IN (SELECT r.run_id FROM runs r JOIN task_revisions t ON t.revision_id=r.task_revision_id WHERE t.task_name=?)"
                params.append(task_name)
            query += " ORDER BY created_at ASC"
            rows = connection.execute(query, params).fetchall()
            stats = {}
            for r in rows:
                rid = r["requested_route"]
                w = _decay_weight(_parse_iso_ts(r["created_at"]), now_ts, half_life_hours)
                entry = stats.get(rid)
                if entry is None:
                    entry = {
                        "route_id": rid,
                        "provider": r["provider"],
                        "total": 0.0,
                        "completed": 0.0,
                        "malformed": 0.0,
                        "schema_violations": 0.0,
                        "grounding_failures": 0.0,
                        "rate_limits": 0.0,
                        "duration_weighted_sum": 0.0,
                        "duration_weight": 0.0,
                        "total_cost": 0.0,
                    }
                    stats[rid] = entry
                entry["provider"] = r["provider"]
                entry["total"] += w
                err = str(r["error"] or "")
                # Legacy rows lack failure-mode columns: a "complete" row that
                # still carries error text must not count as verified.
                if r["status"] == "complete" and not err.strip():
                    entry["completed"] += w
                if "429" in err or "rate limit" in err.lower():
                    entry["rate_limits"] += w
                if r["duration_seconds"] is not None:
                    try:
                        dur = float(r["duration_seconds"])
                    except (TypeError, ValueError):
                        dur = 0.0
                    entry["duration_weighted_sum"] += w * max(0.0, dur)
                    entry["duration_weight"] += w
                if r["cost"] is not None:
                    try:
                        entry["total_cost"] += float(r["cost"])
                    except (TypeError, ValueError):
                        pass
            for entry in stats.values():
                dw = entry.pop("duration_weight")
                dsum = entry.pop("duration_weighted_sum")
                entry["avg_duration"] = float(dsum / dw) if dw > 0 else 0.0
                entry["total_cost"] = float(entry["total_cost"])
            return stats

    def get_route_history_stats_pair(
        self,
        task_name: str,
        half_life_hours: float = 72.0,
        now: float | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Return (task_stats, global_stats) from a single history scan.

        Prefer this over two get_route_history_stats calls when both sides are
        needed (e.g. shrinkage): one DB pass instead of two.
        """
        now_ts = now if now is not None else time.time()

        def _new_entry(rid: str, provider: Any) -> dict[str, Any]:
            return {
                "route_id": rid,
                "provider": provider,
                "total": 0.0,
                "completed": 0.0,
                "malformed": 0.0,
                "schema_violations": 0.0,
                "grounding_failures": 0.0,
                "rate_limits": 0.0,
                "duration_weighted_sum": 0.0,
                "duration_weight": 0.0,
                "total_cost": 0.0,
            }

        def _add_inference(entry: dict[str, Any], r: Any, w: float) -> None:
            entry["provider"] = r["provider"]
            entry["total"] += w
            if r["outcome"] == "verified":
                entry["completed"] += w
            if r["parse_status"] == "malformed_json":
                entry["malformed"] += w
            if r["schema_status"] == "schema_violation":
                entry["schema_violations"] += w
            if r["grounding_status"] == "grounding_failed":
                entry["grounding_failures"] += w
            if r["transport_status"] == "rate_limit" or r["error_type"] == "rate_limit":
                entry["rate_limits"] += w
            if r["duration_seconds"] is not None:
                try:
                    dur = float(r["duration_seconds"])
                except (TypeError, ValueError):
                    dur = 0.0
                entry["duration_weighted_sum"] += w * max(0.0, dur)
                entry["duration_weight"] += w
            if r["cost"] is not None:
                try:
                    entry["total_cost"] += float(r["cost"])
                except (TypeError, ValueError):
                    pass

        def _finalize(stats: dict[str, dict[str, Any]]) -> None:
            for entry in stats.values():
                dw = entry.pop("duration_weight")
                dsum = entry.pop("duration_weighted_sum")
                entry["avg_duration"] = float(dsum / dw) if dw > 0 else 0.0
                entry["total_cost"] = float(entry["total_cost"])

        with self.connect() as connection:
            has_attempts = connection.execute("SELECT count(*) FROM inference_attempts").fetchone()[0]
            if has_attempts > 0:
                rows = connection.execute(
                    """SELECT route_id, provider, outcome, parse_status, schema_status,
                              grounding_status, transport_status, error_type,
                              duration_seconds, cost, started_at, created_at, task_name
                       FROM inference_attempts ORDER BY created_at ASC"""
                ).fetchall()
                task_stats: dict[str, dict[str, Any]] = {}
                global_stats: dict[str, dict[str, Any]] = {}
                for r in rows:
                    ts = _parse_iso_ts(r["started_at"]) or _parse_iso_ts(r["created_at"])
                    w = _decay_weight(ts, now_ts, half_life_hours)
                    rid = r["route_id"]
                    g = global_stats.get(rid)
                    if g is None:
                        g = _new_entry(rid, r["provider"])
                        global_stats[rid] = g
                    _add_inference(g, r, w)
                    if r["task_name"] == task_name:
                        t = task_stats.get(rid)
                        if t is None:
                            t = _new_entry(rid, r["provider"])
                            task_stats[rid] = t
                        _add_inference(t, r, w)
                _finalize(task_stats)
                _finalize(global_stats)
                return task_stats, global_stats

            # Legacy fallback: one joined scan, partitioned in Python.
            rows = connection.execute(
                """SELECT mr.requested_route, mr.provider, mr.status, mr.error,
                          mr.duration_seconds, mr.cost, mr.created_at, t.task_name
                   FROM model_runs mr
                   LEFT JOIN runs r ON r.run_id = mr.run_id
                   LEFT JOIN task_revisions t ON t.revision_id = r.task_revision_id
                   ORDER BY mr.created_at ASC"""
            ).fetchall()
            task_stats = {}
            global_stats = {}
            for r in rows:
                rid = r["requested_route"]
                w = _decay_weight(_parse_iso_ts(r["created_at"]), now_ts, half_life_hours)
                g = global_stats.get(rid)
                if g is None:
                    g = _new_entry(rid, r["provider"])
                    global_stats[rid] = g
                g["provider"] = r["provider"]
                g["total"] += w
                err = str(r["error"] or "")
                # Legacy rows can carry status='complete' together with error
                # text; the single-scan path already excludes those.
                if r["status"] == "complete" and not err.strip():
                    g["completed"] += w
                if "429" in err or "rate limit" in err.lower():
                    g["rate_limits"] += w
                if r["duration_seconds"] is not None:
                    try:
                        dur = float(r["duration_seconds"])
                    except (TypeError, ValueError):
                        dur = 0.0
                    g["duration_weighted_sum"] += w * max(0.0, dur)
                    g["duration_weight"] += w
                if r["cost"] is not None:
                    try:
                        g["total_cost"] += float(r["cost"])
                    except (TypeError, ValueError):
                        pass
                if r["task_name"] == task_name:
                    t = task_stats.get(rid)
                    if t is None:
                        t = _new_entry(rid, r["provider"])
                        task_stats[rid] = t
                    t["provider"] = r["provider"]
                    t["total"] += w
                    if r["status"] == "complete" and not err.strip():
                        t["completed"] += w
                    if "429" in err or "rate limit" in err.lower():
                        t["rate_limits"] += w
                    if r["duration_seconds"] is not None:
                        try:
                            dur = float(r["duration_seconds"])
                        except (TypeError, ValueError):
                            dur = 0.0
                        t["duration_weighted_sum"] += w * max(0.0, dur)
                        t["duration_weight"] += w
                    if r["cost"] is not None:
                        try:
                            t["total_cost"] += float(r["cost"])
                        except (TypeError, ValueError):
                            pass
            _finalize(task_stats)
            _finalize(global_stats)
            return task_stats, global_stats

    def update_route_claim_bias(
        self, task_name: str, route_id: str, errors: list[float]
    ) -> dict[str, Any]:
        """Update running mean bias (predicted-actual) for a route/task.

        errors: per-sample predicted minus actual in claim-score units (0-100 scale).
        Returns the updated {route_id, bias, sample_count}.
        """
        if not errors:
            raise ValueError("errors must be non-empty")
        return self.update_route_claim_bias_stats(
            task_name,
            route_id,
            sum(float(e) for e in errors),
            len(errors),
        )

    def update_route_claim_bias_stats(
        self,
        task_name: str,
        route_id: str,
        error_sum: float,
        sample_count: int,
    ) -> dict[str, Any]:
        """Merge an aggregate bias batch without retaining every sample error."""
        if sample_count <= 0:
            raise ValueError("sample_count must be greater than 0")
        batch_mean = float(error_sum) / sample_count
        batch_n = int(sample_count)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT bias, sample_count FROM route_claim_bias WHERE task_name=? AND route_id=?",
                (task_name, route_id),
            ).fetchone()
            if row is None:
                bias, count = batch_mean, batch_n
            else:
                prev_n = int(row["sample_count"] or 0)
                prev_bias = float(row["bias"] or 0.0)
                count = prev_n + batch_n
                bias = (prev_bias * prev_n + batch_mean * batch_n) / count if count else batch_mean
            connection.execute(
                """INSERT INTO route_claim_bias(task_name, route_id, bias, sample_count, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(task_name, route_id) DO UPDATE SET
                   bias=excluded.bias, sample_count=excluded.sample_count, updated_at=excluded.updated_at""",
                (task_name, route_id, bias, count, now_iso()),
            )
            return {"task_name": task_name, "route_id": route_id, "bias": bias, "sample_count": count}

    def get_route_claim_bias(self, task_name: str | None = None) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            if task_name:
                rows = connection.execute(
                    "SELECT * FROM route_claim_bias WHERE task_name=?", (task_name,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM route_claim_bias").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = f"{r['task_name']}:{r['route_id']}" if task_name is None else str(r["route_id"])
            out[key] = {
                "task_name": r["task_name"],
                "route_id": r["route_id"],
                "bias": float(r["bias"]),
                "sample_count": int(r["sample_count"]),
                "updated_at": r["updated_at"],
            }
        return out

    def get_run_status(self, run_id: str) -> RunStatusReport:
        with self.connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            task = connection.execute(
                "SELECT task_name FROM task_revisions WHERE revision_id=?",
                (run["task_revision_id"],),
            ).fetchone()
            task_name = task["task_name"] if task else "unknown"

            batch_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, count(*) as count FROM batches WHERE run_id=? GROUP BY status",
                    (run_id,),
                ).fetchall()
            }
            res_rows = connection.execute(
                """SELECT r.result_json FROM current_batch_results c
                   JOIN batch_results r ON r.result_id=c.result_id WHERE c.run_id=?""",
                (run_id,),
            ).fetchall()
            verified_items = 0
            for res in res_rows:
                try:
                    items = json.loads(res["result_json"])
                    if isinstance(items, list):
                        verified_items += len(items)
                    elif isinstance(items, dict) and "items" in items:
                        verified_items += len(items["items"])
                except Exception:
                    pass

            has_inf = connection.execute(
                "SELECT count(*) FROM inference_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if has_inf > 0:
                route_rows = connection.execute(
                    """SELECT route_id as requested_route, provider, count(*) as attempts,
                              sum(case when outcome='verified' then 1 else 0 end) as verified,
                              sum(case when transport_status='rate_limit' or error_type='rate_limit' then 1 else 0 end) as rate_limits,
                              avg(duration_seconds) as avg_duration,
                              sum(case when cost is not null then cost else 0 end) as total_cost
                       FROM inference_attempts WHERE run_id=? GROUP BY route_id, provider""",
                    (run_id,),
                ).fetchall()
            else:
                route_rows = connection.execute(
                    """SELECT requested_route, provider, count(*) as attempts,
                              sum(case when status='complete' then 1 else 0 end) as verified,
                              sum(case when error like '%429%' or error like '%rate limit%' then 1 else 0 end) as rate_limits,
                              avg(duration_seconds) as avg_duration,
                              sum(case when cost is not null then cost else 0 end) as total_cost
                       FROM model_runs WHERE run_id=? GROUP BY requested_route, provider""",
                    (run_id,),
                ).fetchall()

            active_workers_row = connection.execute(
                "SELECT count(distinct lease_owner) FROM batches WHERE run_id=? AND status='leased' AND lease_owner IS NOT NULL",
                (run_id,),
            ).fetchone()
            active_workers = int(active_workers_row[0]) if active_workers_row else 0

            err_rows = connection.execute(
                "SELECT distinct error FROM batch_attempts WHERE run_id=? AND error IS NOT NULL ORDER BY started_at DESC LIMIT 5",
                (run_id,),
            ).fetchall()

        total_batches = sum(batch_counts.values())
        batches_summary = BatchStatusCounts(
            total=total_batches,
            verified=batch_counts.get("verified", 0),
            pending=batch_counts.get("pending", 0),
            leased=batch_counts.get("leased", 0),
            failed=batch_counts.get("failed", 0),
        )

        routes_summary = []
        total_rate_limits = 0
        for r in route_rows:
            attempts = r["attempts"]
            verified = r["verified"] or 0
            rl = r["rate_limits"] or 0
            total_rate_limits += rl
            rate = (verified / attempts) if attempts > 0 else 0.0
            routes_summary.append(
                RouteStatusSummary(
                    route_id=r["requested_route"],
                    provider=r["provider"],
                    attempts=attempts,
                    verified=verified,
                    rate_limits=rl,
                    success_rate=round(rate, 3),
                    avg_latency_seconds=round(float(r["avg_duration"] or 0.0), 2),
                    reported_cost=round(float(r["total_cost"] or 0.0), 6),
                )
            )

        return RunStatusReport(
            run_id=run_id,
            status=run["status"],
            task_name=task_name,
            profile_revision_id=run["profile_revision_id"] if "profile_revision_id" in run.keys() else None,
            total_items=run["total_items"],
            verified_items=verified_items,
            batches=batches_summary,
            attempts_used=run["attempts_used"],
            max_attempts=run["max_attempts"],
            rate_limits_encountered=total_rate_limits,
            routes=routes_summary,
            active_workers=active_workers,
            recent_errors=[r["error"] for r in err_rows if r["error"]],
        )

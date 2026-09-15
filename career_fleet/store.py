"""Local SQLite store for career discovery, triage, and evaluated dossiers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def normalize_domain(value: str | None) -> str | None:
    """Return one stable hostname for company/domain matching."""
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
    host = parsed.hostname
    if not host:
        return candidate.lower().rstrip(".") or None
    host = host.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT,
    stage TEXT,
    headcount INT,
    hq_location TEXT,
    timezone TEXT,
    ats_provider TEXT,
    ats_token TEXT,
    website_url TEXT,
    careers_url TEXT,
    source_class TEXT,
    primary_contact_email TEXT,
    primary_contact_name TEXT,
    status TEXT DEFAULT 'discovered',  -- discovered, triaged, qualified, disqualified
    disqualification_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain) WHERE domain IS NOT NULL AND domain != '';
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);

CREATE TABLE IF NOT EXISTS profile_revisions (
    revision_id TEXT PRIMARY KEY,
    profile_kind TEXT NOT NULL CHECK(profile_kind IN ('ideal_company', 'ideal_employer')),
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS active_profiles (
    profile_kind TEXT PRIMARY KEY CHECK(profile_kind IN ('ideal_company', 'ideal_employer')),
    revision_id TEXT NOT NULL REFERENCES profile_revisions(revision_id),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_postings (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    location TEXT,
    timezone TEXT,
    is_remote BOOLEAN DEFAULT 0,
    job_url TEXT,
    raw_text TEXT NOT NULL,
    source_type TEXT,
    source_class TEXT,
    apply_email TEXT,
    contact_name TEXT,
    contact_title TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posting_status TEXT DEFAULT 'active',
    compensation_text TEXT,
    min_comp REAL,
    max_comp REAL,
    currency TEXT DEFAULT 'USD',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON job_postings(company_id);

CREATE TABLE IF NOT EXISTS community_signals (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    relevance_score REAL NOT NULL DEFAULT 0.0,
    signal_types_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    company_id TEXT REFERENCES companies(id) ON DELETE SET NULL,
    profile_revision_id TEXT REFERENCES profile_revisions(revision_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_community_source ON community_signals(source_type, source_key);
CREATE INDEX IF NOT EXISTS idx_community_company ON community_signals(company_id);

CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    lane TEXT NOT NULL,             -- lane2_triage, lane3_systems, lane4_culture
    status TEXT NOT NULL,           -- triaged, qualified, disqualified, marginal, error
    score REAL DEFAULT 0.0,
    verdict TEXT,
    rationale TEXT,
    quotes_json TEXT DEFAULT '[]',
    model_used TEXT,
    profile_revision_id TEXT REFERENCES profile_revisions(revision_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_evals_company ON evaluations(company_id);
CREATE INDEX IF NOT EXISTS idx_evals_lane ON evaluations(lane);
"""


class CareerStore:
    """Manages SQLite database for career intelligence."""

    def __init__(self, db_path: Path | str = "career_fleet.db"):
        db_value = str(db_path)
        self.db_path = Path(db_path).expanduser()
        self._memory_uri: str | None = None
        self._keepalive: sqlite3.Connection | None = None
        if db_value == ":memory:":
            # Each sqlite3.connect(":memory:") call creates a different
            # database. A shared in-memory URI plus one keepalive connection
            # makes the store API behave as callers expect.
            self._memory_uri = f"file:career_fleet_{id(self)}?mode=memory&cache=shared"
            self._keepalive = sqlite3.connect(self._memory_uri, uri=True)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self._memory_uri, uri=True) if self._memory_uri else sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
        finally:
            con.close()

    def close(self) -> None:
        """Release the keepalive connection used by an in-memory store."""
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    def _init_db(self) -> None:
        with self.connect() as con:
            with con:
                con.executescript(SCHEMA)
                # Keep existing user databases usable when new metadata fields
                # are added in a later package version.
                for table in ("companies", "job_postings", "evaluations", "community_signals"):
                    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
                    if table in ("companies", "job_postings") and "timezone" not in columns:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN timezone TEXT")
                    if table == "companies":
                        if "careers_url" not in columns:
                            con.execute("ALTER TABLE companies ADD COLUMN careers_url TEXT")
                        if "source_class" not in columns:
                            con.execute("ALTER TABLE companies ADD COLUMN source_class TEXT")
                        if "primary_contact_email" not in columns:
                            con.execute("ALTER TABLE companies ADD COLUMN primary_contact_email TEXT")
                        if "primary_contact_name" not in columns:
                            con.execute("ALTER TABLE companies ADD COLUMN primary_contact_name TEXT")
                    if table == "job_postings":
                        if "source_type" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN source_type TEXT")
                        if "source_class" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN source_class TEXT")
                        if "apply_email" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN apply_email TEXT")
                        if "contact_name" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN contact_name TEXT")
                        if "contact_title" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN contact_title TEXT")
                        if "first_seen_at" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                        if "last_seen_at" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                        if "posting_status" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN posting_status TEXT DEFAULT 'active'")
                        if "compensation_text" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN compensation_text TEXT")
                        if "min_comp" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN min_comp REAL")
                        if "max_comp" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN max_comp REAL")
                        if "currency" not in columns:
                            con.execute("ALTER TABLE job_postings ADD COLUMN currency TEXT DEFAULT 'USD'")
                    if table == "evaluations" and "profile_revision_id" not in columns:
                        con.execute("ALTER TABLE evaluations ADD COLUMN profile_revision_id TEXT")
                    if table == "community_signals" and "profile_revision_id" not in columns:
                        con.execute("ALTER TABLE community_signals ADD COLUMN profile_revision_id TEXT")
                con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posting_status ON job_postings(posting_status)")

    def upsert_company(
        self,
        company_id: str,
        name: str,
        domain: str | None = None,
        stage: str | None = None,
        headcount: int | None = None,
        hq_location: str | None = None,
        ats_provider: str | None = None,
        ats_token: str | None = None,
        website_url: str | None = None,
        status: str = "discovered",
        timezone: str | None = None,
        careers_url: str | None = None,
        source_class: str | None = None,
        primary_contact_email: str | None = None,
        primary_contact_name: str | None = None,
    ) -> str:
        domain = normalize_domain(domain)
        with self.connect() as con:
            with con:
                canonical_id = company_id
                if domain:
                    existing = con.execute(
                        "SELECT id FROM companies WHERE domain = ? AND id != ? LIMIT 1",
                        (domain, company_id),
                    ).fetchone()
                    if existing:
                        # A source can identify the same company by a YC ID,
                        # ATS token, or domain. Reuse the existing canonical
                        # row instead of failing on the unique-domain index.
                        canonical_id = existing["id"]
                con.execute(
                    """
                    INSERT INTO companies (
                        id, name, domain, stage, headcount, hq_location, timezone,
                        ats_provider, ats_token, website_url, careers_url, source_class,
                        primary_contact_email, primary_contact_name, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        domain = COALESCE(excluded.domain, companies.domain),
                        stage = COALESCE(excluded.stage, companies.stage),
                        headcount = COALESCE(excluded.headcount, companies.headcount),
                        hq_location = COALESCE(excluded.hq_location, companies.hq_location),
                        timezone = COALESCE(excluded.timezone, companies.timezone),
                        ats_provider = COALESCE(excluded.ats_provider, companies.ats_provider),
                        ats_token = COALESCE(excluded.ats_token, companies.ats_token),
                        website_url = COALESCE(excluded.website_url, companies.website_url),
                        careers_url = COALESCE(excluded.careers_url, companies.careers_url),
                        source_class = COALESCE(excluded.source_class, companies.source_class),
                        primary_contact_email = COALESCE(excluded.primary_contact_email, companies.primary_contact_email),
                        primary_contact_name = COALESCE(excluded.primary_contact_name, companies.primary_contact_name),
                        status = excluded.status,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        canonical_id, name, domain, stage, headcount, hq_location, timezone,
                        ats_provider, ats_token, website_url, careers_url, source_class,
                        primary_contact_email, primary_contact_name, status
                    ),
                )
                return canonical_id

    def reset_company_pipeline(
        self,
        company_id: str,
        *,
        clear_postings: bool = False,
        source_type: str | None = None,
    ) -> None:
        """Reset derived funnel state before replacing a company's source data.

        Discovery is a refresh operation. Old postings and lane results must
        not continue to influence the new snapshot.
        """
        with self.connect() as con:
            with con:
                if clear_postings:
                    if source_type:
                        con.execute(
                            "DELETE FROM job_postings WHERE company_id = ? AND source_type = ?",
                            (company_id, source_type),
                        )
                    else:
                        con.execute("DELETE FROM job_postings WHERE company_id = ?", (company_id,))
                con.execute("DELETE FROM evaluations WHERE company_id = ?", (company_id,))
                con.execute(
                    "UPDATE companies SET status = 'discovered', disqualification_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (company_id,),
                )

    def replace_company_source_snapshot(
        self,
        company_id: str,
        source_type: str,
        postings: list[dict[str, Any]],
    ) -> bool:
        """Replace one source's postings in a single transaction.

        Empty snapshots are treated as an unavailable/ambiguous fetch and do
        not erase the last known source data. All rows are validated before
        deleting the existing snapshot, so a bad row cannot leave partial data.
        """
        if not source_type:
            raise ValueError("source_type is required for a source snapshot")
        if not postings:
            return False

        rows = []
        for posting in postings:
            job_id = posting.get("id")
            raw_text = posting.get("raw_text")
            if not job_id or not raw_text:
                raise ValueError("source snapshot postings require non-empty id and raw_text")
            rows.append(
                (
                    str(job_id),
                    str(posting.get("company_id") or company_id),
                    str(posting.get("title") or "Untitled"),
                    posting.get("location"),
                    posting.get("timezone"),
                    1 if posting.get("is_remote") else 0,
                    posting.get("job_url"),
                    str(raw_text),
                    source_type,
                )
            )

        with self.connect() as con:
            with con:
                con.execute(
                    "DELETE FROM job_postings WHERE company_id = ? AND source_type = ?",
                    (company_id, source_type),
                )
                con.execute("DELETE FROM evaluations WHERE company_id = ?", (company_id,))
                con.execute(
                    "UPDATE companies SET status = 'discovered', disqualification_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (company_id,),
                )
                con.executemany(
                    """
                    INSERT INTO job_postings (
                        id, company_id, title, location, timezone, is_remote, job_url, raw_text, source_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        company_id = excluded.company_id,
                        title = excluded.title,
                        location = excluded.location,
                        timezone = excluded.timezone,
                        is_remote = excluded.is_remote,
                        job_url = excluded.job_url,
                        raw_text = excluded.raw_text,
                        source_type = excluded.source_type
                    """,
                    rows,
                )
        return True

    def list_companies(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as con:
            if status:
                rows = con.execute("SELECT * FROM companies WHERE status = ? ORDER BY name ASC", (status,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM companies ORDER BY name ASC").fetchall()
            return [dict(r) for r in rows]

    def get_company_by_domain(self, domain: str) -> dict[str, Any] | None:
        """Return the canonical company row for a normalized domain."""
        normalized = normalize_domain(domain)
        if not normalized:
            return None
        with self.connect() as con:
            row = con.execute("SELECT * FROM companies WHERE domain = ?", (normalized,)).fetchone()
            return dict(row) if row else None

    def save_profile(self, profile: Any) -> str:
        """Persist an immutable IEP revision and activate it."""
        payload = profile.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        revision_id = hashlib.sha256(
            json.dumps(
                {"profile_kind": "ideal_employer", "profile": payload},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        with self.connect() as con:
            with con:
                con.execute(
                    """
                    INSERT OR IGNORE INTO profile_revisions (
                        revision_id, profile_kind, profile_name, profile_version, profile_json
                    ) VALUES (?, 'ideal_employer', ?, ?, ?)
                    """,
                    (revision_id, profile.profile_name, profile.version, encoded),
                )
                con.execute(
                    """
                    INSERT INTO active_profiles (profile_kind, revision_id, updated_at)
                    VALUES ('ideal_employer', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(profile_kind) DO UPDATE SET
                        revision_id = excluded.revision_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (revision_id,),
                )
        return revision_id

    def load_profile(self) -> Any | None:
        """Load the active IEP from SQLite, if one has been stored."""
        with self.connect() as con:
            row = con.execute(
                """
                SELECT r.profile_json
                FROM active_profiles a
                JOIN profile_revisions r ON r.revision_id = a.revision_id
                WHERE a.profile_kind = 'ideal_employer'
                """
            ).fetchone()
        if not row:
            return None
        from career_fleet.profile import IdealEmployerProfile

        return IdealEmployerProfile.model_validate(json.loads(row["profile_json"]))

    def active_profile_revision_id(self) -> str | None:
        """Return the active IEP revision used for new evaluations."""
        with self.connect() as con:
            row = con.execute(
                "SELECT revision_id FROM active_profiles WHERE profile_kind = 'ideal_employer'"
            ).fetchone()
        return str(row["revision_id"]) if row else None

    @staticmethod
    def _validate_profile_revision(con: sqlite3.Connection, revision_id: str | None) -> None:
        """Keep legacy databases from accepting an unresolvable profile ID."""
        if revision_id is None:
            return
        row = con.execute(
            "SELECT profile_kind FROM profile_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"profile revision does not exist: {revision_id}")
        if row["profile_kind"] != "ideal_employer":
            raise ValueError(f"profile revision is not an Ideal Employer Profile: {revision_id}")

    def add_job_posting(
        self,
        job_id: str,
        company_id: str,
        title: str,
        raw_text: str,
        location: str | None = None,
        is_remote: bool = False,
        job_url: str | None = None,
        timezone: str | None = None,
        source_type: str | None = None,
        source_class: str | None = None,
        apply_email: str | None = None,
        contact_name: str | None = None,
        contact_title: str | None = None,
        first_seen_at: str | None = None,
        last_seen_at: str | None = None,
        posting_status: str = "active",
        compensation_text: str | None = None,
        min_comp: float | None = None,
        max_comp: float | None = None,
        currency: str = "USD",
    ) -> None:
        with self.connect() as con:
            with con:
                con.execute(
                    """
                    INSERT INTO job_postings (
                        id, company_id, title, location, timezone, is_remote, job_url, raw_text,
                        source_type, source_class, apply_email, contact_name, contact_title,
                        first_seen_at, last_seen_at, posting_status, compensation_text,
                        min_comp, max_comp, currency, created_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        COALESCE(?, CURRENT_TIMESTAMP),
                        COALESCE(?, CURRENT_TIMESTAMP),
                        ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        company_id = excluded.company_id,
                        title = excluded.title,
                        location = excluded.location,
                        timezone = excluded.timezone,
                        is_remote = excluded.is_remote,
                        job_url = excluded.job_url,
                        raw_text = excluded.raw_text,
                        source_type = COALESCE(excluded.source_type, job_postings.source_type),
                        source_class = COALESCE(excluded.source_class, job_postings.source_class),
                        apply_email = COALESCE(excluded.apply_email, job_postings.apply_email),
                        contact_name = COALESCE(excluded.contact_name, job_postings.contact_name),
                        contact_title = COALESCE(excluded.contact_title, job_postings.contact_title),
                        last_seen_at = CURRENT_TIMESTAMP,
                        posting_status = excluded.posting_status,
                        compensation_text = COALESCE(excluded.compensation_text, job_postings.compensation_text),
                        min_comp = COALESCE(excluded.min_comp, job_postings.min_comp),
                        max_comp = COALESCE(excluded.max_comp, job_postings.max_comp),
                        currency = COALESCE(excluded.currency, job_postings.currency)
                    """,
                    (
                        job_id, company_id, title, location, timezone, 1 if is_remote else 0,
                        job_url, raw_text, source_type, source_class, apply_email,
                        contact_name, contact_title, first_seen_at, last_seen_at,
                        posting_status, compensation_text, min_comp, max_comp, currency,
                    ),
                )

    def mark_job_status(self, job_id: str, status: str) -> bool:
        """Update the temporal posting_status (e.g. 'active', 'stale_unconfirmed', 'closed')."""
        with self.connect() as con:
            with con:
                cur = con.execute(
                    "UPDATE job_postings SET posting_status = ?, last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, job_id),
                )
                return cur.rowcount > 0

    def list_active_jobs(self, company_id: str | None = None) -> list[dict[str, Any]]:
        """Return all active job postings, optionally filtered by company."""
        with self.connect() as con:
            if company_id:
                rows = con.execute(
                    "SELECT * FROM job_postings WHERE company_id = ? AND posting_status = 'active' ORDER BY created_at DESC",
                    (company_id,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM job_postings WHERE posting_status = 'active' ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def replace_community_source_snapshot(
        self,
        source_type: str,
        source_key: str,
        signals: list[dict[str, Any]],
        *,
        profile_revision_id: str | None = None,
    ) -> dict[str, int]:
        """Replace one career-focused community snapshot atomically.

        Community records may not identify a company.  Unlinked records live
        in ``community_signals`` for human review; linked records also become
        ordinary postings so the existing Career lanes can screen them.
        Empty fetches are preserved as unavailable snapshots, matching the
        refresh semantics of ATS and site sources.
        """
        source_type = str(source_type or "").strip()
        source_key = str(source_key or "").strip()
        if not source_type or not source_key:
            raise ValueError("community source_type and source_key are required")
        rows: list[tuple[Any, ...]] = []
        posting_rows: list[tuple[Any, ...]] = []
        new_company_ids: set[str] = set()
        seen_ids: set[str] = set()
        for signal in signals:
            signal_id = str(signal.get("id") or "").strip()
            title = str(signal.get("title") or "Community career signal").strip()
            source_uri = str(signal.get("source_uri") or "").strip()
            raw_text = str(signal.get("raw_text") or "").strip()
            if not signal_id or not source_uri or not raw_text:
                raise ValueError("community signals require non-empty id, source_uri, and raw_text")
            if signal_id in seen_ids:
                raise ValueError(f"duplicate community signal id: {signal_id}")
            seen_ids.add(signal_id)
            try:
                relevance_score = float(signal.get("relevance_score", 0.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid community relevance score for {signal_id}") from exc
            if not 0.0 <= relevance_score <= 1.0:
                raise ValueError(f"community relevance score out of range for {signal_id}")
            signal_types = signal.get("signal_types") or []
            if not isinstance(signal_types, list):
                raise ValueError(f"community signal_types must be a list for {signal_id}")
            metadata = signal.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise ValueError(f"community metadata must be an object for {signal_id}")
            company_id = str(signal.get("company_id") or "").strip() or None
            if company_id:
                new_company_ids.add(company_id)
            rows.append(
                (
                    signal_id,
                    source_type,
                    source_key,
                    title,
                    source_uri,
                    raw_text,
                    relevance_score,
                    json.dumps(signal_types, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    company_id,
                    profile_revision_id,
                )
            )
            if company_id:
                posting_rows.append(
                    (
                        signal_id,
                        company_id,
                        title,
                        metadata.get("location"),
                        metadata.get("timezone"),
                        1 if metadata.get("is_remote") else 0,
                        source_uri,
                        raw_text,
                        source_type,
                    )
                )

        if not rows:
            return {"signals_added": 0, "linked_postings_added": 0, "linked_companies": 0}

        with self.connect() as con:
            with con:
                self._validate_profile_revision(con, profile_revision_id)
                old = con.execute(
                    "SELECT id, company_id FROM community_signals WHERE source_type = ? AND source_key = ?",
                    (source_type, source_key),
                ).fetchall()
                old_ids = [str(row["id"]) for row in old]
                affected_company_ids = {str(row["company_id"]) for row in old if row["company_id"]}
                affected_company_ids.update(new_company_ids)
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    con.execute(
                        f"DELETE FROM job_postings WHERE source_type = ? AND id IN ({placeholders})",
                        [source_type, *old_ids],
                    )
                con.execute(
                    "DELETE FROM community_signals WHERE source_type = ? AND source_key = ?",
                    (source_type, source_key),
                )
                for company_id in affected_company_ids:
                    con.execute("DELETE FROM evaluations WHERE company_id = ?", (company_id,))
                    con.execute(
                        "UPDATE companies SET status = 'discovered', disqualification_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (company_id,),
                    )
                con.executemany(
                    """
                    INSERT INTO community_signals (
                        id, source_type, source_key, title, source_uri, raw_text,
                        relevance_score, signal_types_json, metadata_json, company_id,
                        profile_revision_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        source_type = excluded.source_type,
                        source_key = excluded.source_key,
                        title = excluded.title,
                        source_uri = excluded.source_uri,
                        raw_text = excluded.raw_text,
                        relevance_score = excluded.relevance_score,
                        signal_types_json = excluded.signal_types_json,
                        metadata_json = excluded.metadata_json,
                        company_id = excluded.company_id,
                        profile_revision_id = excluded.profile_revision_id
                    """,
                    rows,
                )
                con.executemany(
                    """
                    INSERT INTO job_postings (
                        id, company_id, title, location, timezone, is_remote,
                        job_url, raw_text, source_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        company_id = excluded.company_id,
                        title = excluded.title,
                        location = excluded.location,
                        timezone = excluded.timezone,
                        is_remote = excluded.is_remote,
                        job_url = excluded.job_url,
                        raw_text = excluded.raw_text,
                        source_type = excluded.source_type
                    """,
                    posting_rows,
                )
        return {
            "signals_added": len(rows),
            "linked_postings_added": len(posting_rows),
            "linked_companies": len(new_company_ids),
        }

    def list_community_signals(
        self,
        source_type: str | None = None,
        linked: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List durable community leads with decoded metadata."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("s.source_type = ?")
            params.append(source_type)
        if linked is True:
            clauses.append("s.company_id IS NOT NULL")
        elif linked is False:
            clauses.append("s.company_id IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as con:
            records = con.execute(
                f"""
                SELECT s.*, c.name AS company_name
                FROM community_signals s
                LEFT JOIN companies c ON c.id = s.company_id
                {where}
                ORDER BY s.relevance_score DESC, s.created_at DESC, s.id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        decoded: list[dict[str, Any]] = []
        for record in records:
            row = dict(record)
            try:
                row["signal_types"] = json.loads(row.pop("signal_types_json") or "[]")
            except (TypeError, ValueError):
                row["signal_types"] = []
            try:
                row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                row["metadata"] = {}
            decoded.append(row)
        return decoded

    def record_evaluation(
        self,
        eval_id: str,
        company_id: str,
        lane: str,
        status: str,
        score: float,
        verdict: str,
        rationale: str,
        quotes: list[str] | None = None,
        model_used: str | None = None,
        profile_revision_id: str | None = None,
    ) -> None:
        quotes_json = json.dumps(quotes or [])
        with self.connect() as con:
            with con:
                self._validate_profile_revision(con, profile_revision_id)
                if lane == "lane2_triage":
                    # A changed hard-filter profile invalidates every later
                    # lane. Keep one current result per lane, not stale gates.
                    con.execute(
                        "DELETE FROM evaluations WHERE company_id = ? AND lane IN ('lane3_systems', 'lane4_culture')",
                        (company_id,),
                    )
                elif lane == "lane3_systems":
                    # Culture depends on the current systems result.
                    con.execute(
                        "DELETE FROM evaluations WHERE company_id = ? AND lane = 'lane4_culture'",
                        (company_id,),
                    )
                con.execute(
                    """
                    INSERT INTO evaluations (
                        id, company_id, lane, status, score, verdict, rationale, quotes_json,
                        model_used, profile_revision_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        company_id = excluded.company_id,
                        lane = excluded.lane,
                        status = excluded.status,
                        score = excluded.score,
                        verdict = excluded.verdict,
                        rationale = excluded.rationale,
                        quotes_json = excluded.quotes_json,
                        model_used = excluded.model_used,
                        profile_revision_id = excluded.profile_revision_id
                    """,
                    (
                        eval_id, company_id, lane, status, score, verdict, rationale,
                        quotes_json, model_used, profile_revision_id,
                    ),
                )
                # Advance the company through the funnel without allowing a
                # later lane to hide a disqualification.
                if status == "disqualified":
                    con.execute("UPDATE companies SET status = 'disqualified', disqualification_reason = ? WHERE id = ?", (rationale, company_id))
                elif status == "triaged" and lane == "lane2_triage":
                    con.execute(
                        "UPDATE companies SET status = 'triaged', disqualification_reason = NULL WHERE id = ?",
                        (company_id,),
                    )
                elif lane == "lane3_systems":
                    # Lane 3 is a gate, not the final qualification. Keep the
                    # company in the triaged pool until Lane 4 also passes.
                    con.execute(
                        "UPDATE companies SET status = 'triaged' WHERE id = ? AND status != 'disqualified'",
                        (company_id,),
                    )
                elif lane == "lane4_culture" and status == "qualified":
                    con.execute("UPDATE companies SET status = 'qualified' WHERE id = ? AND status != 'disqualified'", (company_id,))
                elif lane == "lane4_culture" and status != "qualified":
                    con.execute(
                        "UPDATE companies SET status = 'triaged' WHERE id = ? AND status != 'disqualified'",
                        (company_id,),
                    )

    def get_company_dossier(self, company_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            comp = con.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
            if not comp:
                return None
            jobs = con.execute(
                "SELECT * FROM job_postings WHERE company_id = ?",
                (company_id,),
            ).fetchall()
            evals = con.execute("SELECT * FROM evaluations WHERE company_id = ? ORDER BY created_at ASC", (company_id,)).fetchall()
            dossier = dict(comp)
            dossier["jobs"] = [dict(j) for j in jobs]
            signals = con.execute(
                "SELECT * FROM community_signals WHERE company_id = ? ORDER BY created_at ASC",
                (company_id,),
            ).fetchall()
            dossier["community_signals"] = [dict(s) for s in signals]
            dossier["evaluations"] = [dict(e) for e in evals]
            return dossier

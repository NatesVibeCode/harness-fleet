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
    FETCHED,
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
    "source_uri",
    "queries",
    "advances",
    "next_rung",
)


#: Every row a node writes lands in one table, keyed by the run it belongs to.
#: A rung table is state: the next rung queries it, a report counts it, and a
#: person reads it by asking a question of it. That is a table in the database
#: the run already has, not a file per node that something has to remember to
#: find, parse and keep in step.
ROWS_TABLE = "rung_rows"
TEXT_TABLE = "rung_text"
#: What a walk gathered, one row per item, in the same keyed shape as everything
#: else. This used to be a table per node whose name was built by joining the
#: dag and node ids — which meant two different nodes could produce the same
#: name ("a-b"+"c" and "a"+"b-c" both become rung_items_a_b_c) and quietly write
#: into each other's table. A key is a key.
ITEMS_TABLE = "rung_items"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ROWS_TABLE} (
    dag_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    run_seq INTEGER NOT NULL DEFAULT 1,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    lane TEXT NOT NULL DEFAULT '',
    rung TEXT NOT NULL DEFAULT '',
    item_id TEXT NOT NULL,
    candidate TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    earned TEXT NOT NULL DEFAULT '',
    because TEXT NOT NULL DEFAULT '',
    gates TEXT NOT NULL DEFAULT '[]',
    surfaces TEXT NOT NULL DEFAULT '{{}}',
    pages TEXT NOT NULL DEFAULT '[]',
    queries TEXT NOT NULL DEFAULT '[]',
    advances INTEGER NOT NULL DEFAULT 0,
    next_rung TEXT NOT NULL DEFAULT '',
    source_uri TEXT NOT NULL DEFAULT '',
    visited INTEGER NOT NULL DEFAULT 0,
    kept INTEGER NOT NULL DEFAULT 0,
    skipped TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{{}}',
    run_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dag_id, node_id, run_seq, seq)
);
CREATE INDEX IF NOT EXISTS {ROWS_TABLE}_entity ON {ROWS_TABLE} (item_id);
CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
    dag_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    run_seq INTEGER NOT NULL DEFAULT 1,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (dag_id, node_id, run_seq, seq)
);
CREATE TABLE IF NOT EXISTS {TEXT_TABLE} (
    dag_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    run_seq INTEGER NOT NULL DEFAULT 1,
    item_id TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (dag_id, node_id, run_seq, item_id)
);
"""

#: Columns that hold JSON, so a reader gets the structure back rather than a
#: string that once was one.
_JSON_COLUMNS = ("gates", "surfaces", "pages", "queries", "skipped", "facts")


class RungTables:
    """The tables a lane's ladder writes, in the run's own database.

    One writer for every node kind, because every node writes the same thing: a
    population, the verdicts that produced it, who it passes on, and the prose
    those verdicts stand on. The rows carry a ``dag_id`` and a ``node_id`` rather
    than a path, so a rung table can be joined, counted and compared across runs
    with SQL instead of with a folder full of files.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        with self.store.connect() as connection:
            connection.executescript(_SCHEMA)

    def next_seq(self, dag_id: str, node_id: str) -> int:
        """The attempt number this write will be: one more than the last.

        A node's table is not overwritten. Re-running a rung adds an attempt, so
        what the run concluded last time is still there to compare against, and
        a person can see whether a rung got better or the world changed.
        """
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT MAX(run_seq) AS last FROM {ROWS_TABLE} WHERE dag_id=? AND node_id=?",
                (dag_id, node_id),
            ).fetchone()
        return int((found["last"] if found else 0) or 0) + 1

    def attempts(self, dag_id: str, node_id: str) -> list[int]:
        """Every attempt this node has been run, oldest first."""
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT DISTINCT run_seq FROM {ROWS_TABLE} WHERE dag_id=? AND node_id=? "
                "ORDER BY run_seq",
                (dag_id, node_id),
            ).fetchall()
        return [int(row["run_seq"]) for row in found]

    def write(
        self,
        dag_id: str,
        node_id: str,
        rows: Sequence[dict[str, Any]],
        *,
        texts: dict[str, str] | None = None,
        kind: str = "",
        lane: str = "",
        run_seq: int | None = None,
    ) -> int:
        """Add this attempt's rows, keeping every earlier attempt.

        Returns the attempt number, which is what a caller records so the exact
        table a verdict came from can be named later.
        """
        run_seq = int(run_seq or self.next_seq(dag_id, node_id))
        columns = (
            "dag_id", "node_id", "run_seq", "seq", "kind", "lane", "rung", "item_id", "candidate",
            "evidence", "outcome", "earned", "because", "gates", "surfaces", "pages",
            "queries", "advances", "next_rung", "source_uri", "visited", "kept", "skipped",
            "score", "tier", "facts", "run_id",
        )
        with self.store.connect() as connection:
            connection.execute(
                f"DELETE FROM {ROWS_TABLE} WHERE dag_id=? AND node_id=? AND run_seq=?",
                (dag_id, node_id, run_seq),
            )
            for seq, row in enumerate(rows):
                values = {
                    "dag_id": dag_id,
                    "node_id": node_id,
                    "run_seq": run_seq,
                    "seq": seq,
                    "kind": kind,
                    "lane": lane,
                    "rung": row.get("rung", ""),
                    "item_id": str(row.get("item_id") or ""),
                    "candidate": row.get("candidate", ""),
                    "evidence": row.get("evidence", ""),
                    "outcome": row.get("outcome", ""),
                    "earned": row.get("earned", ""),
                    "because": row.get("because", ""),
                    "gates": row.get("gates", []),
                    "surfaces": row.get("surfaces", {}),
                    "pages": row.get("pages", []),
                    "queries": row.get("queries", []),
                    "advances": 1 if row.get("advances") else 0,
                    "next_rung": row.get("next_rung", ""),
                    "source_uri": row.get("source_uri", ""),
                    "visited": int(row.get("visited") or 0),
                    "kept": int(row.get("kept") or 0),
                    "skipped": row.get("skipped", []),
                    "score": float(row.get("score") or 0.0),
                    "tier": row.get("tier", ""),
                    "facts": row.get("facts", {}),
                    "run_id": row.get("run_id", ""),
                }
                connection.execute(
                    f"INSERT OR REPLACE INTO {ROWS_TABLE} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(_stored(values[column]) for column in columns),
                )
            if texts:
                connection.execute(
                    f"DELETE FROM {TEXT_TABLE} WHERE dag_id=? AND node_id=? AND run_seq=?",
                    (dag_id, node_id, run_seq),
                )
                for item_id, text in texts.items():
                    connection.execute(
                        f"INSERT OR REPLACE INTO {TEXT_TABLE} "
                        "(dag_id, node_id, run_seq, item_id, text) VALUES (?, ?, ?, ?, ?)",
                        (dag_id, node_id, run_seq, str(item_id), text),
                    )
        return run_seq

    def rows(
        self, dag_id: str, node_id: str, run_seq: int | None = None
    ) -> list[dict[str, Any]]:
        """This node's table: the latest attempt unless one is named."""
        run_seq = run_seq or (self.attempts(dag_id, node_id) or [1])[-1]
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT * FROM {ROWS_TABLE} WHERE dag_id=? AND node_id=? AND run_seq=? "
                "ORDER BY seq",
                (dag_id, node_id, run_seq),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for record in found:
            row = dict(record)
            for column in _JSON_COLUMNS:
                try:
                    row[column] = json.loads(row.get(column) or "null")
                except ValueError:
                    pass
            row["advances"] = bool(row.get("advances"))
            out.append(row)
        return out

    def text(self, dag_id: str, node_id: str, item_id: str, run_seq: int | None = None) -> str:
        run_seq = run_seq or (self.attempts(dag_id, node_id) or [1])[-1]
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT text FROM {TEXT_TABLE} WHERE dag_id=? AND node_id=? AND run_seq=? "
                "AND item_id=?",
                (dag_id, node_id, run_seq, str(item_id)),
            ).fetchone()
        return str(found["text"]) if found else ""

    def texts(self, dag_id: str, node_id: str, run_seq: int | None = None) -> dict[str, str]:
        run_seq = run_seq or (self.attempts(dag_id, node_id) or [1])[-1]
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT item_id, text FROM {TEXT_TABLE} WHERE dag_id=? AND node_id=? "
                "AND run_seq=?",
                (dag_id, node_id, run_seq),
            ).fetchall()
        return {str(row["item_id"]): str(row["text"]) for row in found}

    def ids(self, dag_id: str, node_id: str, run_seq: int | None = None) -> list[str]:
        return [str(row["item_id"]) for row in self.rows(dag_id, node_id, run_seq)]

    def export_csv(
        self, dag_id: str, node_id: str, path: str | Path, run_seq: int | None = None
    ) -> int:
        """Hand a node's table to somebody who wants a file, on request."""
        return write_csv(path, self.rows(dag_id, node_id, run_seq))

    def write_items(
        self,
        dag_id: str,
        node_id: str,
        payloads: Sequence[Any],
        *,
        run_seq: int,
    ) -> int:
        """Store what a walk gathered, in the attempt its rows were written to.

        The attempt is required rather than derived: items that belong to one
        attempt and land in another is the kind of bug a default hides.
        """
        run_seq = int(run_seq)
        with self.store.connect() as connection:
            connection.execute(
                f"DELETE FROM {ITEMS_TABLE} WHERE dag_id=? AND node_id=? AND run_seq=?",
                (dag_id, node_id, run_seq),
            )
            for seq, payload in enumerate(payloads):
                connection.execute(
                    f"INSERT INTO {ITEMS_TABLE} (dag_id, node_id, run_seq, seq, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (dag_id, node_id, run_seq, seq, json.dumps(payload, ensure_ascii=False)),
                )
        return len(payloads)

    def items(
        self, dag_id: str, node_id: str, run_seq: int | None = None
    ) -> list[Any]:
        """The items a walk gathered, in order."""
        run_seq = run_seq or (self.attempts(dag_id, node_id) or [1])[-1]
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT payload FROM {ITEMS_TABLE} WHERE dag_id=? AND node_id=? "
                "AND run_seq=? ORDER BY seq",
                (dag_id, node_id, run_seq),
            ).fetchall()
        return [json.loads(row["payload"]) for row in found]

    def prune(self, *, keep_attempts: int = 1, dag_id: str | None = None) -> dict[str, int]:
        """Drop all but the newest attempts, and say what went.

        Rung tables are the record of a run, and a fleet that has run a hundred
        times is a hundred attempts per node. The newest is the answer; the rest
        are history somebody has to decide to keep, so this is a command rather
        than something that happens on its own.
        """
        removed = {"rows": 0, "text": 0, "items": 0}
        where = " WHERE dag_id=?" if dag_id else ""
        params: tuple[Any, ...] = (dag_id,) if dag_id else ()
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT DISTINCT dag_id, node_id FROM {ROWS_TABLE}{where}", params
            ).fetchall()
            for key in found:
                attempts = self.attempts(str(key["dag_id"]), str(key["node_id"]))
                stale = attempts[: max(0, len(attempts) - max(1, int(keep_attempts)))]
                for run_seq in stale:
                    for table, column in (
                        (ROWS_TABLE, "rows"),
                        (TEXT_TABLE, "text"),
                        (ITEMS_TABLE, "items"),
                    ):
                        cursor = connection.execute(
                            f"DELETE FROM {table} WHERE dag_id=? AND node_id=? AND run_seq=?",
                            (str(key["dag_id"]), str(key["node_id"]), run_seq),
                        )
                        removed[column] += int(cursor.rowcount or 0)
        return removed

    def stats(self) -> dict[str, int]:
        """What the store is holding, so growth is visible before it is a problem."""
        out: dict[str, int] = {}
        with self.store.connect() as connection:
            for table in (ROWS_TABLE, TEXT_TABLE, ITEMS_TABLE):
                out[table] = int(
                    connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                )
            out["attempts"] = int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT dag_id, node_id, run_seq "
                    f"FROM {ROWS_TABLE})"
                ).fetchone()["n"]
            )
        return out


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _stored(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def write_csv(
    path: str | Path,
    rows: Sequence[dict[str, Any]],
    *,
    columns: Sequence[str] | None = None,
) -> int:
    """Write rows as a CSV table and return how many were written.

    Columns are the spine in order, then any extra key the rows carry, in first
    appearance order. Values that are not scalars are written as JSON so a
    reader gets the whole verdict rather than ``[object Object]``; the file is
    one flat table either way. This is an *export*: the table lives in the run's
    database, and this is what you get when you ask for a file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = list(columns) if columns else list(TABLE_COLUMNS)
    for row in rows:
        for key in row:
            if key not in ordered:
                ordered.append(key)
    columns = ordered
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in columns})
    return len(rows)


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """Read an exported table back, for a test or a person."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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
    nothing left to buy — *unless* the rung above reads evidence this report
    does not have. That is the one case the old rule got wrong: a search result
    can pass every cheap gate a lane puts to it, and still carry none of the
    evidence the run's bar demands, because only a page can. The career lane
    delivered nothing at all that way — a snippet-qualified posting earned no
    visit, the ladder stopped, and the scoring stage was handed an empty room.
    """
    if report.eliminated or rung is None:
        return False
    if rung.evidence == FETCHED and not report.fetched:
        return True
    if report.qualified:
        return False
    if report.earned and report.earned == rung.name:
        return True
    return any(gate in rung.gates for gate in report.unresolved)


def surviving_ids(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The item ids the rung above left alive: everything it did not eliminate."""
    return [str(row["item_id"]) for row in rows if row.get("outcome") != "eliminated"]


def population(tables: Any, dag_id: str, node_ids: Sequence[str]) -> set[str]:
    """Who is still standing after a chain of gates.

    Nobody is dropped for failing to earn more evidence, and nobody comes back
    from a rung that eliminated them: an entity is alive when *no* rung threw it
    out, whether or not a later rung had anything left to ask it. Reading only
    the last table would lose every lead the ladder had finished with.
    """
    alive: set[str] = set()
    eliminated: set[str] = set()
    for node_id in node_ids:
        for row in tables.rows(dag_id, node_id):
            item_id = str(row.get("item_id") or "")
            if row.get("outcome") == "eliminated":
                eliminated.add(item_id)
            else:
                alive.add(item_id)
    return alive - eliminated


def count_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The shape of one rung's table: where the world shrank and what is open.

    Counted off the table rather than off the reports that produced it, so the
    number a run reports and the rows a person reads are the same fact. Who was
    eliminated, and why, is in the table itself — a count carrying its own copy
    of the names is a second source of truth, and second sources drift.
    """
    counts: dict[str, Any] = {
        "candidates": len(rows),
        "verdicts": {},
        "eliminated_at": {},
        "unresolved_at": {},
        "needing_retrieval": 0,
    }
    for row in rows:
        verdict = str(row.get("outcome") or "unknown")
        counts["verdicts"][verdict] = counts["verdicts"].get(verdict, 0) + 1
        gates = row.get("gates") or []
        if verdict == "eliminated":
            for result in gates:
                if result.get("outcome") == "fail":
                    gate = str(result["gate"])
                    counts["eliminated_at"][gate] = counts["eliminated_at"].get(gate, 0) + 1
                    break
            continue
        # A survivor still holding a gate open is what the run is waiting on. A
        # gate it passed is not open, and an eliminated candidate's unknowns are
        # not the survivors' problem.
        counts["needing_retrieval"] += 1
        for result in gates:
            if result.get("outcome") == "unknown":
                gate = str(result["gate"])
                counts["unresolved_at"][gate] = counts["unresolved_at"].get(gate, 0) + 1
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
    resolve_results: int = 6,
    resolve_query: str = "",
    backends: Sequence[str] = (),
    score: bool = False,
    score_task: str = "",
    score_run_id: str = "",
    score_policy: Any = None,
    sessions: int = 4,
    max_attempts: int = 300,
    top: int | None = None,
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
                    "rung_of": rung.as_dict(),
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
                "rung_of": rung.as_dict(),
                **({"profile": str(profile_path)} if profile_path else {}),
                **source,
            }
        )
        previous_gate = gate_id
        if rung.resolve and (index + 1) < len(rungs):
            # Before anything walks this candidate's site, ask the search engine
            # the firmographic question: one search instead of a page fetch per
            # entity, and the answer is a fact somebody already published.
            resolve_id = f"x{index}-{_node_slug(rung.name)}"
            nodes.append({
                "kind": "resolve",
                "id": resolve_id,
                "lane": lane.name,
                "from_gate": gate_id,
                "fields": list(rung.resolve),
                "backends": list(backends),
                "max_results": resolve_results,
                "timeout": timeout,
                **({"query": resolve_query} if resolve_query else {}),
            })
            # And then ask the rung's questions again, on what came back. This is
            # the whole point of searching first: a candidate whose open gates the
            # search settled is not owed a page visit, and the walk below only
            # reads the ones still standing.
            recheck_id = f"c{index}-{_node_slug(rung.name)}"
            nodes.append({
                "kind": "gate",
                "id": recheck_id,
                "lane": lane.name,
                "rung": rung.name,
                "rung_of": rung.as_dict(),
                **({"profile": str(profile_path)} if profile_path else {}),
                "from_gate": resolve_id,
            })
            previous_gate = recheck_id
    if score and (previous_gate or nodes):
        # The last stage is a stage: it reads the population the ladder left
        # standing, gathers what the walks read about those firms, and writes the
        # score and the facts onto the same running list everything else feeds.
        nodes.append({
            "kind": "score",
            "id": "s-score",
            "lane": lane.name,
            **({"from_gate": previous_gate} if previous_gate else {}),
            # Every node above it, not just the walks. The first gate holds what
            # the run already knew — a discovery run's records — and a graph over
            # an existing run whose walks come back empty has nothing else to
            # judge on: reading only the walks scored nobody and said so as if
            # the ladder had found nobody.
            "from_nodes": [
                node["id"] for node in nodes if node["kind"] in ("gate", "resolve", "retrieve")
            ],
            **({"from_items": from_items} if from_items is not None else {}),
            **({"task": score_task} if score_task else {}),
            **({"run_id": score_run_id} if score_run_id else {}),
            # Which routes the scoring stage may spend, when the caller knows
            # (a command with route flags) rather than leaving it to selection.
            **({"policy": score_policy} if score_policy is not None else {}),
            "sessions": sessions,
            "max_attempts": max_attempts,
            **({"top": top} if top else {}),
        })
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

"""The running list: every entity the fleet has looked at, across every run.

A rung table answers "what happened in this run, at this node". That is not the
question an operator has. Their question is "who have we looked at, what do we
know about them, and where did each one stop" — and the answer has to survive
the run that produced it, because the point of running again is to learn more
about the same firms, not to start from nothing.

So there are two records and they are different shapes.

``entity_state`` is the running list: one row per entity, forever. It holds what
is currently believed — the last verdict, the gate that stopped it, what is
still open, the evidence already gathered, how many times it has been seen — and
every run updates it in place. This is the table to read when you want the
population rather than a run.

``entity_events`` is append-only: one row per entity per node per attempt. It is
never updated, only added to, so "why did we drop them" and "when did they first
appear" stay answerable after the state row has moved on. State is fast to read;
events are the record of how it got there.

Both live in the run's SQLite database. Nothing here writes a CSV.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

#: The running list. One row per entity, updated in place.
STATE_TABLE = "entity_state"
#: The record of how it got there. Append-only.
EVENTS_TABLE = "entity_events"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
    entity TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_dag TEXT NOT NULL DEFAULT '',
    last_node TEXT NOT NULL DEFAULT '',
    last_rung TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    because TEXT NOT NULL DEFAULT '',
    gates_open TEXT NOT NULL DEFAULT '[]',
    lanes TEXT NOT NULL DEFAULT '[]',
    pages_seen INTEGER NOT NULL DEFAULT 0,
    sightings INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{{}}',
    scored_at TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    at TEXT NOT NULL,
    dag_id TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL DEFAULT '',
    run_seq INTEGER NOT NULL DEFAULT 1,
    lane TEXT NOT NULL DEFAULT '',
    rung TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    because TEXT NOT NULL DEFAULT '',
    gates_open TEXT NOT NULL DEFAULT '[]',
    pages TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{{}}',
    run_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS {EVENTS_TABLE}_entity ON {EVENTS_TABLE} (entity, at);
CREATE VIEW IF NOT EXISTS entity_score_history AS
    SELECT entity, at, score, tier, run_id, node_id
    FROM {EVENTS_TABLE} WHERE score > 0 ORDER BY entity, at;
"""

#: What a node says when it went and looked, rather than when it judged. These
#: are facts about the visit — a page was read, a search answered — and they are
#: not verdicts, so they never overwrite one. The running list's `outcome` is
#: "where this firm stands", and a walk is not a change of standing.
_MECHANICS = frozenset({"", "read", "nothing", "complete", "resolved"})

#: The verdict an elimination carries, and the only one that may overwrite
#: another verdict outright: worst news stands until something re-qualifies the
#: entity, and a node that merely carried it forward cannot revive it.
_ELIMINATED = "eliminated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    """The running list, and the events that produced it."""

    def __init__(self, store: Any) -> None:
        self.store = store
        with self.store.connect() as connection:
            connection.executescript(_SCHEMA)

    def record(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        dag_id: str = "",
        node_id: str = "",
        run_seq: int = 1,
        lane: str = "",
        at: str | None = None,
    ) -> int:
        """Fold one node's table into the running list, and log the events.

        Everything a rung learned is already in its rows, so this reads the rows
        rather than asking the node for anything: what an entity is, what was
        decided, why, and whether it is still standing.
        """
        stamp = at or _now()
        recorded = 0
        with self.store.connect() as connection:
            for row in rows:
                entity = str(row.get("item_id") or row.get("candidate") or "").strip()
                if not entity:
                    continue
                outcome = str(row.get("outcome") or "")
                because = str(row.get("because") or "")
                gates = row.get("gates") or []
                open_gates = [
                    str(gate.get("gate"))
                    for gate in gates
                    if isinstance(gate, dict) and gate.get("outcome") == "unknown"
                ]
                pages = [str(page) for page in (row.get("pages") or [])]
                connection.execute(
                    f"INSERT INTO {EVENTS_TABLE} (entity, at, dag_id, node_id, run_seq, lane, "
                    "rung, outcome, because, gates_open, pages, score, tier, facts, run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity, stamp, dag_id, node_id, int(run_seq), lane,
                        str(row.get("rung") or ""), outcome, because,
                        json.dumps(open_gates), json.dumps(pages),
                        float(row.get("score") or 0.0), str(row.get("tier") or ""),
                        json.dumps(row.get("facts") or {}),
                        # Every event names a run: the campaign that scored it
                        # when there was one, the graph that visited it otherwise.
                        str(row.get("run_id") or dag_id),
                    ),
                )
                found = connection.execute(
                    f"SELECT * FROM {STATE_TABLE} WHERE entity=?", (entity,)
                ).fetchone()
                if found is None:
                    connection.execute(
                        f"INSERT INTO {STATE_TABLE} (entity, first_seen, last_seen, last_dag, "
                        "last_node, last_rung, outcome, because, gates_open, lanes, pages_seen, "
                        "sightings, attempts, score, tier, facts, scored_at, run_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            entity, stamp, stamp, dag_id, node_id, str(row.get("rung") or ""),
                            outcome, because, json.dumps(open_gates),
                            json.dumps([lane] if lane else []), len(pages), 1, 1,
                            float(row.get("score") or 0.0), str(row.get("tier") or ""),
                            json.dumps(row.get("facts") or {}),
                            stamp if row.get("score") else "",
                            str(row.get("run_id") or ""),
                        ),
                    )
                    recorded += 1
                    continue
                # What gets written onto the list is a verdict, never a mechanic.
                # A node that says "read" is reporting a visit; letting that land
                # in `outcome` would erase the gate that decided the firm's fate
                # and leave a list of rows that say "read" and mean nothing.
                judged = outcome not in _MECHANICS or bool(row.get("tier"))
                keeps = judged and (
                    outcome == _ELIMINATED or found["outcome"] != _ELIMINATED
                )
                lanes = sorted({*(json.loads(found["lanes"] or "[]")), *([lane] if lane else [])})
                scored = bool(row.get("score")) or bool(row.get("facts"))
                connection.execute(
                    f"UPDATE {STATE_TABLE} SET last_seen=?, last_dag=?, last_node=?, "
                    "last_rung=?, outcome=?, because=?, gates_open=?, lanes=?, pages_seen=?, "
                    "sightings=sightings+1, attempts=attempts+?, score=?, tier=?, facts=?, "
                    "scored_at=?, run_id=? WHERE entity=?",
                    (
                        stamp, dag_id, node_id, str(row.get("rung") or ""),
                        outcome if keeps else found["outcome"],
                        because if keeps else found["because"],
                        json.dumps(open_gates) if keeps else found["gates_open"],
                        json.dumps(lanes),
                        int(found["pages_seen"] or 0) + len(pages),
                        1,
                        float(row.get("score") or found["score"] or 0.0) if scored else found["score"],
                        str(row.get("tier") or found["tier"] or "") if scored else found["tier"],
                        json.dumps(row.get("facts") or {}) if row.get("facts")
                        else found["facts"],
                        stamp if scored else found["scored_at"],
                        str(row.get("run_id") or found["run_id"] or ""),
                        entity,
                    ),
                )
                recorded += 1
        return recorded

    def entities(
        self,
        *,
        limit: int | None = None,
        outcome: str = "",
        lane: str = "",
        standing_only: bool = False,
    ) -> list[dict[str, Any]]:
        """The running list, newest first."""
        where: list[str] = []
        params: list[Any] = []
        if outcome:
            where.append("outcome=?")
            params.append(outcome)
        if lane:
            where.append("lanes LIKE ?")
            params.append(f'%"{lane}"%')
        if standing_only:
            where.append("outcome!='eliminated'")
        if getattr(self, "_scored_only", False):
            where.append("scored_at!=''")
        sql = f"SELECT * FROM {STATE_TABLE}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY score DESC, last_seen DESC, entity"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.store.connect() as connection:
            found = connection.execute(sql, tuple(params)).fetchall()
        out: list[dict[str, Any]] = []
        for record in found:
            row = dict(record)
            for column in ("gates_open", "lanes"):
                try:
                    row[column] = json.loads(row.get(column) or "[]")
                except ValueError:
                    pass
            try:
                row["facts"] = json.loads(row.get("facts") or "{}")
            except ValueError:
                pass
            out.append(row)
        return out

    def history(self, entity: str, limit: int = 50) -> list[dict[str, Any]]:
        """Everything that ever happened to one entity, oldest first."""
        with self.store.connect() as connection:
            found = connection.execute(
                f"SELECT * FROM {EVENTS_TABLE} WHERE entity=? ORDER BY at, event_id LIMIT ?",
                (entity, int(limit)),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for record in found:
            row = dict(record)
            for column in ("gates_open", "pages"):
                try:
                    row[column] = json.loads(row.get(column) or "[]")
                except ValueError:
                    pass
            try:
                row["facts"] = json.loads(row.get("facts") or "{}")
            except ValueError:
                pass
            out.append(row)
        return out

    def scores(self, entity: str, limit: int = 100) -> list[dict[str, Any]]:
        """Every number this entity has been given, oldest first.

        State holds the latest score, which answers "how good is this firm". It
        cannot answer "is this firm getting better", and that is a different
        question with a different use: a score that moved means something in the
        world changed, or the lane did.
        """
        with self.store.connect() as connection:
            found = connection.execute(
                "SELECT at, score, tier, run_id, node_id FROM entity_score_history "
                "WHERE entity=? ORDER BY at LIMIT ?",
                (entity, int(limit)),
            ).fetchall()
        return [dict(row) for row in found]

    def movers(self, limit: int = 25, minimum_delta: float = 0.0) -> list[dict[str, Any]]:
        """Who moved, and by how much: the last two numbers each entity got.

        Read off the events rather than a column, because a column can only hold
        one number and the point of this is the difference between two.
        """
        with self.store.connect() as connection:
            found = connection.execute(
                """
                WITH ranked AS (
                    SELECT entity, at, score, tier, run_id,
                           ROW_NUMBER() OVER (PARTITION BY entity ORDER BY at DESC) AS rank
                    FROM entity_score_history
                ),
                pair AS (
                    SELECT now.entity AS entity,
                           now.score AS score,
                           was.score AS previous,
                           now.tier AS tier,
                           now.at AS at,
                           now.run_id AS run_id
                    FROM ranked now JOIN ranked was
                      ON was.entity = now.entity AND was.rank = 2
                    WHERE now.rank = 1
                )
                SELECT * FROM pair WHERE ABS(score - previous) >= ? ORDER BY score DESC LIMIT ?
                """,
                (float(minimum_delta), int(limit)),
            ).fetchall()
        out = []
        for record in found:
            row = dict(record)
            row["delta"] = round(float(row["score"]) - float(row["previous"]), 2)
            out.append(row)
        return out

    def counts(self) -> dict[str, int]:
        """The running list in one line: how many, and where they stand."""
        with self.store.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS n FROM {STATE_TABLE}").fetchone()["n"]
            rows = connection.execute(
                f"SELECT outcome, COUNT(*) AS n FROM {STATE_TABLE} GROUP BY outcome"
            ).fetchall()
        out = {str(row["outcome"] or "unknown"): int(row["n"]) for row in rows}
        out["entities"] = int(total)
        return out

    def export_csv(self, path: str, rows: Iterable[dict[str, Any]] | None = None) -> int:
        """Write the running list to a file, when somebody asks for one."""
        from .rungs import write_csv

        return write_csv(path, list(rows if rows is not None else self.entities()))

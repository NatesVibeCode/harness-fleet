"""Deliver to a quota: how many records, at or above what score.

An operator does not want a run. They want *two hundred partner firms at 70 or
better*, and the difference matters: a run that returns forty of them is not a
result they can act on, and "run it again" is not an instruction the tool should
be handing back. So the quota is the unit of work, and the harness keeps widening
until it has delivered it or can say honestly why it stopped.

Three rules make that safe to run:

**Nothing is delivered twice.** The running list already knows every firm the
fleet has scored, so a later round draws from what it has not seen. Delivery is
counted off the ledger, not off one run's packet.

**Widening has an end.** A lane's query space is finite: its templates over its
terms, which is a number the engine can compute before it starts. Each round
takes the next slice of that space, so the harness can always say how much of it
is left. When it is exhausted, so is the quota — and that is a fact about the
lane the operator can fix by adding terms, not a throttle built into the tool.

**Bounds are the operator's, and they are visible.** `--rounds` caps the loop and
`--want` caps the delivery; nothing else stops it. The tool never decides on its
own that enough is enough.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Quota:
    """What the operator asked for, and how far the loop may go."""

    #: Records they need, at or above ``min_score``.
    want: int = 0
    #: The score a delivered record has to reach.
    min_score: float = 0.0
    #: Rounds the loop may spend. None means "until the query space runs out".
    max_rounds: int | None = None

    def wanted(self) -> bool:
        return self.want > 0


@dataclass
class Round:
    """One pass: what it searched, what it added, and where delivery stands."""

    number: int
    queries: list[str] = field(default_factory=list)
    candidates: int = 0
    delivered_now: int = 0
    delivered_total: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.number,
            "queries": len(self.queries),
            "candidates": self.candidates,
            "delivered_now": self.delivered_now,
            "delivered_total": self.delivered_total,
            "note": self.note,
        }


def query_space(lane: Any) -> list[str]:
    """Every query a lane can ask, uncapped: the space a quota is drawn from.

    The cap on a single run (`max_queries`) is how many of these one round
    searches. The size of this list is what says whether another round has
    anything new to ask.
    """
    import itertools

    queries = [str(query) for query in (getattr(lane, "queries", None) or []) if str(query).strip()]
    terms = {
        name: [str(value) for value in values]
        for name, values in (getattr(lane, "query_terms", None) or {}).items()
    }
    expanded: list[str] = []
    for query in queries:
        names = [name for name in terms if "{" + name + "}" in query]
        if not names:
            expanded.append(query)
            continue
        for combination in itertools.product(*(terms[name] for name in names)):
            filled = query
            for name, value in zip(names, combination, strict=True):
                filled = filled.replace("{" + name + "}", value)
            expanded.append(filled)
    seen: list[str] = []
    for query in expanded:
        if query not in seen:
            seen.append(query)
    return seen


def rounds(lane: Any, quota: Quota) -> list[list[str]]:
    """The query slices, in order: one round's worth per entry.

    Round one is a normal run. Every round after it searches the *next* slice,
    so a quota widens rather than repeats — repeating the same queries is how a
    loop spends money to rediscover the same firms.
    """
    space = query_space(lane)
    if not space:
        return []
    per_round = max(1, int(getattr(lane, "max_queries", 0) or len(space)))
    slices = [space[index:index + per_round] for index in range(0, len(space), per_round)]
    if quota.max_rounds is not None:
        slices = slices[: max(1, int(quota.max_rounds))]
    return slices


def delivered(store: Any, min_score: float) -> list[dict[str, Any]]:
    """What the fleet has already delivered: scored firms at or above the bar.

    Read off the running list, so it counts every run's contribution and a firm
    delivered last week is not delivered again this week.
    """
    from .ledger import Ledger

    return [
        row
        for row in Ledger(store).entities(limit=1_000_000)
        if row.get("scored_at") and float(row.get("score") or 0.0) >= float(min_score)
    ]


def outstanding(store: Any, quota: Quota) -> int:
    """How many more the operator is owed. Zero when the quota is empty or met."""
    if not quota.wanted():
        return 0
    return max(0, int(quota.want) - len(delivered(store, quota.min_score)))


def verdict(store: Any, quota: Quota, rounds_run: Sequence[Round]) -> dict[str, Any]:
    """Whether the quota was delivered, and if not, what stopped it.

    The report is the point of the loop. A run that stops short says how many it
    delivered, how many the operator asked for, how much of the lane's query
    space it searched, and therefore what to change — terms, not patience.
    """
    have = delivered(store, quota.min_score)
    owed = outstanding(store, quota)
    searched = sum(len(entry.queries) for entry in rounds_run)
    met = owed == 0
    if met:
        summary = f"delivered {len(have)} at or above {quota.min_score:g}"
    elif searched:
        summary = (
            f"delivered {len(have)} of {quota.want} at or above {quota.min_score:g} "
            f"after {searched} queries"
        )
    else:
        summary = (
            f"delivered {len(have)} of {quota.want} at or above {quota.min_score:g}"
        )
    return {
        "want": quota.want,
        "min_score": quota.min_score,
        "delivered": len(have),
        "owed": owed,
        "met": met,
        "rounds": [entry.as_dict() for entry in rounds_run],
        "summary": summary,
    }


def write_deliverable(store: Any, quota: Quota, path: str | Path) -> int:
    """The quota's answer: every delivered firm, at or above the bar, one table.

    Not one run's packet. The operator asked for a number of firms, so the file
    they open holds that number — whichever rounds produced them.
    """
    from .rungs import write_csv

    rows = sorted(
        delivered(store, quota.min_score),
        key=lambda row: (-float(row.get("score") or 0.0), str(row.get("entity") or "")),
    )
    if quota.want:
        rows = rows[: int(quota.want)]
    return write_csv(path, rows)


def run_to_quota(
    store: Any,
    lane: Any,
    quota: Quota,
    run_one: Callable[[Sequence[str], int], Any],
    *,
    deliverable: str | Path | None = None,
    on_round: Callable[[Round], None] | None = None,
) -> dict[str, Any]:
    """Keep widening until the quota is delivered, or the lane runs out of room.

    ``run_one`` is one complete research run over a slice of queries — the same
    command the operator would have typed, so each round is a real run with its
    own artifacts, its own place in the running list, and its own resume. This
    loop only decides *what to search next* and *whether to stop*.

    It stops the moment the quota is met, not at the round ceiling: the ceiling
    is a bound on spending, not a target to reach.
    """
    entries: list[Round] = []
    slices = rounds(lane, quota)
    if not slices:
        return verdict(store, quota, entries)

    before = len(delivered(store, quota.min_score))
    for index, queries in enumerate(slices, start=1):
        entry = Round(number=index, queries=list(queries))
        entry.delivered_total = len(delivered(store, quota.min_score))
        if quota.wanted() and outstanding(store, quota) == 0:
            entry.note = "quota already delivered"
            entries.append(entry)
            if on_round:
                on_round(entry)
            break
        run_one(queries, index)
        after = delivered(store, quota.min_score)
        entry.delivered_now = len(after) - entry.delivered_total
        entry.delivered_total = len(after)
        entries.append(entry)
        if on_round:
            on_round(entry)
        if quota.wanted() and outstanding(store, quota) == 0:
            break

    result = verdict(store, quota, entries)
    result["added"] = len(delivered(store, quota.min_score)) - before
    if deliverable:
        result["deliverable"] = str(deliverable)
        result["rows"] = write_deliverable(store, quota, deliverable)
    return result

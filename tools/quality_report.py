"""What every rung and every source actually returns.

The question this answers is not "did the run finish" but "did this rung, this
surface, this source return anything of value — and if not, is that a thing to
tweak or a thing to drop". Those are different verdicts and they need different
evidence:

**VALUE** — it produced rows, or eliminated rows, or advanced rows. A rung that
only ever passes everything through is not earning its place, but a rung that
eliminates five candidates for one page visit is doing exactly its job.

**EMPTY** — it ran, and returned nothing. That is the tweakable verdict: the
surface may be wrong, the path may have moved, the query may be too narrow, or
the source may not exist any more. Counted, with what the walk said about it.

**UNUSED** — no run read it at all. A rung or surface declared in a lane and
never reached is either unreachable (a ladder bug) or decoration (a lane bug),
and both are worth knowing before tuning what runs.

It reads the run database and the lane files, and nothing else: every number
here is a count of rows some node actually wrote.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _connect(db: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db))
    connection.row_factory = sqlite3.Row
    return connection


def _tables(db: str | Path) -> set[str]:
    with _connect(db) as connection:
        return {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def nodes_by_lane(db: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Every node any run has executed, grouped by the lane it ran for.

    Node kinds come from the recorded rows themselves: a gate writes rows with
    verdicts, a retrieve writes surfaces, a score writes scores.
    """
    out: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {
        "rows": 0, "runs": set(), "outcomes": defaultdict(int), "advanced": 0,
        "gates_run": defaultdict(int), "surfaces": defaultdict(int),
        "surface_rows": defaultdict(int), "surface_hits": defaultdict(int),
        "empty_surfaces": defaultdict(int), "scored": 0, "scores": [], "kind": "",
        "rung": "", "eliminated": 0, "skips": defaultdict(lambda: defaultdict(int)),
    }))
    with _connect(db) as connection:
        for row in connection.execute(
            "SELECT lane, dag_id, node_id, kind, rung, outcome, advances, score, gates, "
            "surfaces, skipped FROM rung_rows"
        ):
            lane = str(row["lane"] or "unknown")
            node = out[lane][str(row["node_id"])]
            node["rows"] += 1
            node["runs"].add(str(row["dag_id"]))
            node["outcomes"][str(row["outcome"] or "-")] += 1
            node["kind"] = str(row["kind"] or node["kind"])
            node["rung"] = str(row["rung"] or node["rung"])
            if str(row["outcome"] or "") == "eliminated":
                node["eliminated"] += 1
            if row["advances"]:
                node["advanced"] += 1
            if row["score"]:
                node["scored"] += 1
                node["scores"].append(float(row["score"]))
            for verdict in _json_list(row["gates"]):
                if isinstance(verdict, dict):
                    node["gates_run"][str(verdict.get("gate"))] += 1
            for surface, count in _json_map(row["surfaces"]).items():
                node["surfaces"][surface] += int(count or 0)
                node["surface_rows"][surface] += 1
                if count:
                    node["surface_hits"][surface] += 1
                else:
                    node["empty_surfaces"][surface] += 1
            for skip in _json_list(row["skipped"]):
                if not isinstance(skip, dict):
                    continue
                surface = str(skip.get("surface") or "")
                if surface:
                    node["skips"][surface][_skip_kind(str(skip.get("reason") or ""))] += 1
    return out


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skip_kind(reason: str) -> str:
    """Why a surface came back with nothing, in the three shapes it takes.

    * ``not named`` — this rung's ladder does not read that surface, so it was
      never tried. Not a failure of the source.
    * ``dead path`` — the URL answered, and said 404/410, or there is no
      sitemap. The path this install guesses is wrong for this site.
    * ``nothing there`` — the pages were fetched and carried nothing about the
      entity. The source is real and this candidate has nothing on it.
    """
    text = reason.lower()
    if "ladder does not name" in text:
        return "not named"
    if "http " in text or "no sitemap" in text or "timed out" in text or "refused" in text:
        return "dead path"
    return "nothing there"


def lane_plan(name: str) -> list[dict[str, Any]]:
    """The lane's own declaration: its rungs, gates and surfaces."""
    from harness_fleet.lanes import shipped_lanes

    lane = shipped_lanes().get(name)
    if lane is None:
        return []
    return [
        {
            "rung": rung.name, "evidence": rung.evidence, "gates": list(rung.gates),
            "surfaces": list(rung.surfaces), "resolve": list(rung.resolve),
        }
        for rung in (lane.funnel.rungs() or [])
    ]


def declared_surfaces() -> dict[str, int]:
    """Every surface the installed plan knows, and how many paths it names."""
    from harness_fleet.enrich import load_surfaces

    plan = load_surfaces()
    out: dict[str, int] = {}
    for surface, paths in (plan.get("first_party_paths") or {}).items():
        out[surface] = len(paths or [])
    for name in ("registry", "vendor_stories", "community"):
        if plan.get(name):
            out[name] = len((plan.get(name) or {}).get("vendors") or {}) or 1
    for name in (plan.get("channels") or {}):
        out[name] = 1
    return out


def verdict_for(node: dict[str, Any]) -> tuple[str, str]:
    """VALUE, EMPTY or UNUSED for one node, with the reason a reader can audit."""
    rows = int(node["rows"])
    if rows == 0:
        return "UNUSED", "no run has produced a single row here"
    eliminated = int(node["outcomes"].get("eliminated", 0))
    advanced = int(node["advanced"])
    produced = sum(int(count or 0) for count in node["surfaces"].values())
    if node["kind"] == "score":
        # A score node is judged on the scores, not on the rows: fourteen
        # candidates carrying a zero and one carrying a ten is a very different
        # report from fourteen carrying a ten, and only the count says which.
        scored = int(node["scored"])
        scores = node["scores"]
        mean = f", mean {sum(scores) / len(scores):.1f}" if scores else ""
        if scored:
            return "VALUE", f"{rows} row(s), {scored} scored above zero{mean}"
        return "EMPTY", f"{rows} row(s), every one of them scored zero"
    if eliminated or advanced or produced:
        bits = []
        if eliminated:
            bits.append(f"eliminated {eliminated}")
        if advanced:
            bits.append(f"advanced {advanced}")
        if produced:
            bits.append(f"read {produced} record(s)")
        return "VALUE", ", ".join(bits)
    return "EMPTY", f"{rows} row(s) and none of them decided anything"


def report(db: str | Path) -> dict[str, Any]:
    lanes = nodes_by_lane(db)
    plan = {name: lane_plan(name) for name in ("account", "career", "partner")}
    installed = declared_surfaces()
    used_surfaces: dict[str, int] = defaultdict(int)
    empty_surfaces: dict[str, int] = defaultdict(int)
    records_surfaced: dict[str, int] = defaultdict(int)
    skip_kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    out: dict[str, Any] = {"database": str(db), "lanes": {}}
    for lane, nodes in sorted(lanes.items()):
        entries = []
        reached = set()
        for node_id in sorted(nodes):
            node = nodes[node_id]
            state, reason = verdict_for(node)
            for surface, count in node["surfaces"].items():
                records_surfaced[surface] += int(count or 0)
            for surface, looked in node["surface_rows"].items():
                used_surfaces[surface] += looked
                empty_surfaces[surface] += looked - node["surface_hits"].get(surface, 0)
            for surface, kinds in node["skips"].items():
                for kind, count in kinds.items():
                    skip_kinds[surface][kind] += count
            if node["rung"]:
                reached.add(node["rung"])
            entries.append({
                "node": node_id, "runs": len(node["runs"]), "rows": node["rows"],
                "outcomes": dict(node["outcomes"]), "advanced": node["advanced"],
                "surfaces": dict(node["surfaces"]), "scored": node["scored"],
                "verdict": state, "because": reason,
            })
        declared = [rung["rung"] for rung in plan.get(lane, [])]
        out["lanes"][lane] = {
            "plan": plan.get(lane, []),
            "nodes": entries,
            "rungs_declared": declared,
            "rungs_reached": sorted(reached),
            "rungs_unreached": [name for name in declared if name not in reached],
        }
    out["surfaces"] = {
        "installed": installed, "used": dict(used_surfaces),
        "records": dict(records_surfaced),
        "empty": dict(empty_surfaces),
        "skips": {name: dict(kinds) for name, kinds in skip_kinds.items()},
        # A surface that was looked at and carried nothing is *used*: it left
        # skip notes, which is exactly the evidence a reader tunes against.
        # Counting only rows that produced records filed such a surface as both
        # "looked and carried nothing" and "never read", and told the reader to
        # stop trusting a source the run had in fact asked.
        "never_used": sorted(
            name for name in installed
            if name not in used_surfaces and name not in skip_kinds
        ),
        "always_empty": sorted(
            name for name, count in empty_surfaces.items()
            if count >= used_surfaces.get(name, 0)
        ),
    }
    return out


def markdown(payloads: list[dict[str, Any]]) -> str:
    """One document over every database it was pointed at."""
    lines: list[str] = []
    add = lines.append
    add("# Rung and source quality")
    add("")
    add("Generated by `tools/quality_report.py` from the run databases below. Every")
    add("number is a count of rows some node wrote; nothing here is estimated.")
    add("")
    add("**VALUE** produced, eliminated or advanced something. **EMPTY** ran and")
    add("returned nothing, so it is a thing to tweak. **UNUSED** no run reached it,")
    add("so it is either unreachable or decoration. In the source tables a count is")
    add("a *note* a row left behind, and one candidate can leave more than one: a")
    add("surface with three guessed paths can report three dead ones.")
    add("")
    for payload in payloads:
        db = payload["database"]
        add(f"# `{db}`")
        add("")
        for lane, data in payload["lanes"].items():
            add(f"## {lane}")
            add("")
            if not data["nodes"]:
                add("No runs for this lane in this database.")
                add("")
                continue
            declared = data["rungs_declared"]
            reached = data["rungs_reached"]
            add(
                "Rungs declared: "
                + (", ".join(f"`{name}`" for name in declared) or "—")
                + " · reached by a run: "
                + (", ".join(f"`{name}`" for name in reached) or "none")
            )
            if data["rungs_unreached"]:
                add("")
                add(
                    "**Declared and never reached:** "
                    + ", ".join(f"`{name}`" for name in data["rungs_unreached"])
                )
            add("")
            add("| node | runs | rows | outcomes | advanced | read | verdict | because |")
            add("|---|---|---|---|---|---|---|---|")
            for entry in data["nodes"]:
                outcomes = ", ".join(f"{k} {v}" for k, v in sorted(entry["outcomes"].items()))
                read = sum(entry["surfaces"].values())
                add(
                    f"| `{entry['node']}` | {entry['runs']} | {entry['rows']} | {outcomes} | "
                    f"{entry['advanced']} | {read} | **{entry['verdict']}** | {entry['because']} |"
                )
            add("")
            plan = data["plan"]
            if plan:
                add("Plan (what this lane *says* it reads):")
                add("")
                add("| rung | evidence | gates | surfaces |")
                add("|---|---|---|---|")
                for rung in plan:
                    surfaces = ", ".join(f"`{s}`" for s in rung["surfaces"]) or "—"
                    add(
                        f"| {rung['rung']} | `{rung['evidence']}` | "
                        f"{', '.join(rung['gates'])} | {surfaces} |"
                    )
                add("")
        surfaces = payload["surfaces"]
        add("## Sources")
        add("")
        add(f"Installed: {len(surfaces['installed'])} · read by some run: {len(surfaces['used'])}")
        add("")
        if surfaces["used"] or surfaces["skips"]:
            add(
                "| surface | records | rows with a record | looked and carried nothing "
                "| dead path or HTTP error | not read by that rung |"
            )
            add("|---|---|---|---|---|---|")
            names = set(surfaces["records"]) | set(surfaces["skips"])
            for name in sorted(names, key=lambda n: -surfaces["records"].get(n, 0)):
                kinds = surfaces["skips"].get(name, {})
                add(
                    f"| `{name}` | {surfaces['records'].get(name, 0)} | "
                    f"{surfaces['used'].get(name, 0)} | {kinds.get('nothing there', 0)} | "
                    f"{kinds.get('dead path', 0)} | {kinds.get('not named', 0)} |"
                )
            add("")
        if surfaces["never_used"]:
            add("**Never read by any recorded run** — unreachable or decoration:")
            add("")
            for name in surfaces["never_used"]:
                add(f"- `{name}`")
            add("")
        if surfaces["always_empty"]:
            add("**Read and never returned a record** — tweak or drop:")
            add("")
            for name in surfaces["always_empty"]:
                add(f"- `{name}`")
            add("")
    return "\n".join(lines) + "\n"


def _database(argument: str) -> Path:
    path = Path(argument)
    if path.is_dir():
        path = path / "harness-fleet.db"
    return path


def main(argv: list[str]) -> int:
    positional = [a for a in argv[1:] if not a.startswith("--") and a != _flag_value(argv, "--out")]
    if not positional:
        print("usage: quality_report.py <workspace|db> [more...] [--out report.md] [--json]")
        return 2
    payloads = [report(_database(argument)) for argument in positional]
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    else:
        out = None
    if "--json" in argv:
        print(json.dumps(payloads, indent=2, default=str))
        return 0
    text = markdown(payloads)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

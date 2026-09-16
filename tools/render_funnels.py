"""Render each lane's funnel from the shipped configuration.

Reads the real lane files, the surface plan and the contracts, so the picture
cannot drift from what a run does.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_fleet import contracts  # noqa: E402
from harness_fleet.enrich import load_surfaces  # noqa: E402
from harness_fleet.lanes import SHIPPED_LANE_DIR, load_lanes_dir  # noqa: E402
from harness_fleet.task import create_task_from_preset  # noqa: E402

W = 78
surfaces = load_surfaces()
PATHS = surfaces.get("first_party_paths") or {}
COMMUNITY = (surfaces.get("community") or {}).get("backends") or []
VENDORS = (surfaces.get("vendor_stories") or {}).get("vendors") or {}
CHANNELS = surfaces.get("channels") or {}


def rule(char: str = "\u2500") -> str:
    return char * W


def detail(name: str) -> str:
    if name in PATHS:
        shown = ", ".join(PATHS[name][:3])
        return "paths: " + shown + ("..." if len(PATHS[name]) > 3 else "")
    if name == "registry":
        return "partner-directory URL templates"
    if name == "vendor_stories":
        return str(len(VENDORS)) + " vendor story indexes (snowflake, databricks, elastic, ...)"
    if name == "community":
        return str(len(COMMUNITY)) + " backends: " + ", ".join(COMMUNITY)
    if name in ("ats", "code"):
        return "channel: " + ", ".join(CHANNELS.get(name) or [])
    return "-"


def render(lane) -> str:
    funnel = lane.funnel
    out: list[str] = []
    add = out.append

    templates = len(lane.queries or [])
    combos = 1
    for values in (lane.query_terms or {}).values():
        combos *= max(1, len(values))
    queries = min(templates * combos, lane.max_queries or 0) if lane.query_terms else templates

    add(rule("\u2550"))
    add("  " + lane.name.upper() + " LANE - " + lane.description)
    add(rule("\u2550"))
    asks = "asks: is this a delivery firm?" if funnel.allows == "services" else "asks no kind question"
    add("  population gate : allows=" + repr(funnel.allows) + "   (" + asks + ")")
    bar = lane.tier or "none"
    if lane.require_kinds:
        bar += " + require_kinds=" + str(lane.require_kinds)
    add("  bar             : " + bar)
    add("  scored with     : " + lane.preset + "    output: top " + str(lane.top))
    volume = str(queries) + " queries"
    volume += ", " + str(lane.stories) + " stories/vendor" if lane.stories else ", no story indexes"
    add("  volume          : " + volume)
    add("")

    add("  +-- ENTRY ----------------------------------------------------------+")
    line = "  | keyword search: " + str(templates) + " template(s)"
    if lane.query_terms:
        line += " x " + str(len(lane.query_terms)) + " term set(s)"
    line += " -> " + str(queries) + " queries"
    add(line)
    for term, values in (lane.query_terms or {}).items():
        add("  |   " + term + ": " + ", ".join(values))
    add("  | backends: " + ", ".join(lane.backends or []))
    if lane.stories:
        extra = ", " + str(lane.resolve_stories) + " resolved to a domain" if lane.resolve_stories else ""
        add("  | + " + str(lane.stories) + " vendor stories/vendor as candidate names" + extra)
    add("  +-------------------------------------------------------------------+")
    add("                      |  candidates: one row per company")
    add("                      v")

    for index, rung in enumerate(funnel.ladder):
        cost = "0 page fetches" if rung.evidence == "snippet" else "reads pages"
        add(rule())
        add("  RUNG " + str(index + 1) + " . " + rung.name + "   evidence=" + rung.evidence + "  (" + cost + ")")
        add("  gates     : " + ", ".join(rung.gates))
        if rung.surfaces:
            for surface in rung.surfaces:
                add("    read    : " + surface.ljust(14) + detail(surface))
        else:
            add("    read    : nothing - judges what the search already returned")
        add("  earns     : " + (rung.earns or "-"))
        if index + 1 < len(funnel.ladder):
            add("                      |")
            add("                      v  only what passed " + rung.name + " continues")
    add(rule())
    add("                      v")
    add("  +-- OUT ------------------------------------------------------------+")
    add("  | eliminated : recorded with the gate and reason, never fetched")
    add("  | lead       : unresolved on something a fetch could settle, gap named")
    add("  | qualified  : every gate it could be resolved on passed")
    add("  +-------------------------------------------------------------------+")
    add("                      |  survivors, bundled per company")
    add("                      v")

    required = tuple(lane.require_kinds) or tuple(contracts.TIER_MINIMUMS.get(lane.tier or "", ()))
    named = {s for rung in funnel.ladder for s in (rung.surfaces or [])}
    add("  BAR . " + (lane.tier or "require_kinds") + " needs: " + (", ".join(required) or "-"))
    unreachable = []
    for kind in required:
        carriers = contracts.surfaces_for_kinds([kind])
        reachable = [s for s in carriers if s in named]
        if not reachable:
            unreachable.append(kind)
        mark = "OK " if reachable else "GAP"
        note = "rung reads: " + ", ".join(reachable) if reachable else "NO RUNG READS THIS"
        add("    " + mark + " " + kind.ljust(24) + " carried by " + ", ".join(carriers) + "   <- " + note)
    for kind in unreachable:
        add("  !! " + kind + " cannot be gathered by this ladder as written")
    add("")

    task = create_task_from_preset(lane.preset, preset_name=lane.preset)
    props = (task.claims_schema or {}).get("properties") or {}
    answers = ((props.get("answers") or {}).get("properties") or {})
    add("  FIELDS THE ROW CARRIES . preset " + lane.preset)
    add("    claims: " + ", ".join(k for k in props if k != "answers"))
    if answers:
        add("    answers (" + str(len(answers)) + "): " + ", ".join(answers))
        req = "yes" if "answers" in (task.claims_schema.get("required") or []) else "NO - the model may skip it"
        add("             required by the preset: " + req)
    else:
        add("    answers: none defined - this preset returns claims only")
    return "\n".join(out)


blocks = []
for name in ("account", "career", "partner"):
    block = render(load_lanes_dir(SHIPPED_LANE_DIR)[name])
    blocks.append(block)
    print(block)
    print()

target = Path(__file__).resolve().parents[1] / "docs" / "funnels.body.md"
target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
print("wrote", target)

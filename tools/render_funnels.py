"""Render each lane's funnel — and the DAG it compiles to — from the shipped data.

Reads the real lane files, the surface plan and the contracts, and compiles the
ladder with the same `lane_spec` a run uses, so the picture cannot drift from
what a run does. Writes `docs/funnels.md` in full: the header used to be pasted
in by hand, which is exactly the drift this file exists to prevent.
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


def render_dag(lane) -> str:
    """The graph the ladder compiles to: one node per rung, and the walk between.

    This is the half a ladder table cannot show — which node writes which table,
    what each one spends, and that a candidate killed at one rung is absent from
    every table above it.
    """
    import tempfile

    from harness_fleet.dag import DagSpec
    from harness_fleet.rungs import lane_spec

    with tempfile.TemporaryDirectory() as tmp:
        items = Path(tmp) / "captured.jsonl"
        items.write_text("", encoding="utf-8")
        # The whole chain, including the stage that produces the deliverable:
        # a picture of the funnel that stopped before scoring would be a picture
        # of half a run.
        spec = DagSpec.model_validate(lane_spec(
            lane, from_items=str(items), score=True, score_task=lane.preset,
        ))
        order = spec.topo_order()
    out: list[str] = []
    add = out.append
    by_id = {node.id: node for node in spec.nodes}
    for node_id in order:
        node = by_id[node_id]
        kind = getattr(node, "kind", "")
        if kind == "gate":
            cost = "0 fetches" if node.evidence != "fetched" else "reads pages"
            writes = "rung_rows + rung_text (the text it gated on)"
            if node.from_retrieve:
                cost = "0 fetches (reads what the walk brought back)"
        elif kind == "score":
            cost = "reads the standing firms, runs the campaign"
            writes = "rung_rows + rung_text (score, tier, facts) + the running list"
        elif kind == "resolve":
            cost = str(len(node.fields)) + " search(es) per open candidate"
            writes = "rung_rows + rung_text (what the search said)"
        else:
            rung = getattr(node, "rung", "")
            surfaces = (getattr(node, "rung_of", None) or {}).get("surfaces") or []
            cost = "pages: " + (", ".join(surfaces) or "none")
            writes = "rung_rows + rung_text + rung_items"
        reads = next(
            (
                getattr(node, attr, None)
                for attr in ("from_items", "from_run", "from_gate", "from_retrieve")
                if getattr(node, attr, None)
            ),
            "",
        )
        source = str(reads).split("/")[-1]
        add("  " + node_id.ljust(14) + kind.ljust(9) + cost)
        add(f"  {'':14}reads {source:<16} | writes {writes}")
    add("")
    add("  | node | kind | rung | asks / fields | surfaces | writes |")
    add("  |---|---|---|---|---|---|")
    for node_id in order:
        node = by_id[node_id]
        kind = getattr(node, "kind", "")
        rung = getattr(node, "rung", "") or "-"
        rung_of = getattr(node, "rung_of", None) or {}
        if kind == "gate":
            asks = ", ".join(rung_of.get("gates") or [])
        elif kind == "score":
            asks = "the checklist and the bar, on whoever is standing"
        elif kind == "resolve":
            asks = "searches: " + ", ".join(node.fields) + " -> `" + node.query + "`"
        else:
            asks = "reads pages"
        surfaces = ", ".join("`" + surf + "`" for surf in (rung_of.get("surfaces") or [])) or "-"
        writes = {
            "gate": "`rung_rows`, `rung_text`",
            "resolve": "`rung_rows`, `rung_text`",
            "retrieve": "`rung_rows`, `rung_text`, `rung_items`",
            "score": "`rung_rows`, `rung_text`, `entity_state`",
        }.get(kind, "-")
        add("  | `" + node_id + "` | " + kind + " | " + rung + " | " + asks
            + " | " + surfaces + " | " + writes + " |")
    add("")
    resolve_rungs = [r for r in lane.funnel.ladder if r.resolve]
    if resolve_rungs:
        for rung in resolve_rungs:
            add("  a search settles " + ", ".join(rung.resolve) + " at "
                + rung.name + ", so no page is fetched for them")
    else:
        add("  no rung declares a search-settled gate: every firmographic costs a page")
    return "\n".join(out)


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
                add("    read    : " + surface.ljust(16) + detail(surface))
        else:
            add("    read    : nothing - judges what the search already returned")
        add("  earns     : " + (rung.earns or "-"))
        if index + 1 < len(funnel.ladder):
            add("                      |")
            add("                      v  only what passed " + rung.name + " continues")
    # The same ladder as a table, because a table is what a person compares
    # across lanes: what each rung costs, what it asks, and what passing buys.
    add("")
    add("  THE DAG THIS LADDER COMPILES TO")
    add("")
    add(render_dag(lane))
    add("")
    add("  RUNGS AS A TABLE")
    add("")
    add("  | # | rung | evidence | cost | gates | reads | earns |")
    add("  |---|---|---|---|---|---|---|")
    for index, rung in enumerate(funnel.ladder):
        cost = "0 fetches" if rung.evidence == "snippet" else "pages"
        surfaces_cell = ", ".join("`" + s + "`" for s in (rung.surfaces or [])) or "nothing"
        add(
            "  | " + str(index + 1)
            + " | **" + rung.name + "**"
            + " | `" + rung.evidence + "`"
            + " | " + cost
            + " | " + ", ".join(rung.gates)
            + " | " + surfaces_cell
            + " | " + (rung.earns or "-") + " |"
        )
    add("")
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

HEADER = """# The funnels, as they ship

Generated from the lane files, the surface plan and the contracts — not written
by hand — so this picture cannot drift from what a run does. Regenerate with
`tools/render_funnels.py`.

## How to read one

- **population gate** — the kind question the lane asks of a candidate. `services`
  asks "is this a delivery firm"; `any` asks nothing.
- **bar** — what a scored row has to be able to prove.
- **rung** — one step of the ladder. `evidence=snippet` costs no page fetch;
  `evidence=fetched` reads pages. Each rung judges what the ones below returned,
  and only what passes continues, so a candidate that fails early never spends a
  fetch.
- **gates** — the questions a rung puts to the candidate.
- **read** — the surfaces a rung fetches, expanded to what that name actually
  means.
- **earns** — what passing buys: the next rung, or nothing further.
- **OK / GAP** — whether the bar's evidence kinds can be gathered by the ladder
  as written. A GAP is a row that can never clear its own bar.
- **DAG** — the nodes the ladder compiles to, and the table each one writes. A
  `gate` puts a rung's questions; a `resolve` answers the firmographics one flat
  search can settle; a `retrieve` spends the page visits a rung declared; a
  `score` judges whoever is left and writes the number and the facts onto the
  running list. The
  nodes are the run: each one writes its population and the prose behind every
  verdict into the run's own database (`rung_rows`, `rung_text`), where the next
  node queries it. CSV is an export you ask for, not the store. Rows are kept
  per attempt, so re-running a rung adds an answer instead of replacing one, and
  a firm's score is a trajectory rather than a column.

Each lane also carries its ladder as a table — rung, evidence grade, cost,
gates, surfaces read, and what passing earns — which is the form to compare
lanes in."""


#: The reading, kept beside the renderer rather than in a hand-edited file: the
#: numbers above are computed, and what they mean belongs next to them.
FOOTER = """
## What this says today

**No lane has a GAP.** Every evidence kind a bar names is carried by a surface
some rung reads, and the three that were not are fixed in the lane data rather
than in the engine:

- **partner** carries the bar its own sources can prove: the floor is `tier_2`
  (`delivery_proof` + `stack_delivery`), read off the firm's case studies and
  services pages. It was `tier_1`, which also asks for
  `independent_validation` — carried only by vendor story indexes and community
  mentions, which is the ecosystem this lane looks past rather than samples.
- **career** names `ats` on the posting rung, which is what makes
  `delivery_hiring` reachable.
- **account and career firmographics** — the ICP and the employer profile carry
  typed `size_min`, `size_max`, `target_territories` and `target_industries`, so
  an operator has somewhere to state theirs.

**What is still missing**, in the order I would take it:

1. **The account row carries no fields.** `account-research` defines no
   `answers`, so a scored account has a score and a gap and no verticals, no
   stack, no named clients. The ICP names them (`required_stack`,
   `trigger_pain_phrases`, `target_roles`, `anchor_logos`) and they have nowhere
   to land.
2. **GSI / RSI / SI is not a field.** The partner profile has `partner_kind`, and
   the class is derivable from headcount and territory, but nothing writes it
   onto the row.
3. **Known firms are not excluded.** Nothing stops a run from proposing a firm
   the operator already works with; that wants a list or a rule, and it is a
   decision rather than a mechanism.
"""

target = (
    Path(sys.argv[1]) if len(sys.argv) > 1
    else Path(__file__).resolve().parents[1] / "docs" / "funnels.md"
)
target.write_text(
    HEADER + "\n\n" + "\n\n".join(blocks) + "\n" + FOOTER, encoding="utf-8"
)
print("wrote", target)

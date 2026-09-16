"""Lanes: what differs between products, as configuration rather than code.

A lane is the whole of a product's specificity: what it looks for, where it
looks, which checklist it scores with, what bar it demands, and how it presents
the result. It is data — closed-schema validated like every other trust
boundary — so tuning a lane is editing a file, and the shared mechanisms
(evidence bar, scoring, discovery, the registry) never learn about products.

What a lane may *not* contain is code or a mechanism override. If a lane needs a
source the engine cannot speak to, that is a channel; if it needs a claim the
contracts do not know, that is a contract change that applies to every lane.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import field_validator

from .gates import KIND_QUESTIONS, SNIPPET, LadderRung, validate_ladder
from .models import ClosedModel
from .task import PRESETS

LANE_DIRNAME = "lanes"
SLUG = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class LaneError(ValueError):
    """A lane file is unusable, naming the field that has to change."""


class LaneFunnel(ClosedModel):
    """How this lane shrinks its world before it spends anything.

    The funnel is one engine for every lane; what differs is which rungs this
    lane climbs, what each rung reads, and the firmographics it accepts. A role
    posting and an implementation partner are gated on different things, and
    that difference belongs in the lane file rather than in a branch inside the
    engine.
    """

    #: What kind of company this lane wants: ``services`` rules out product
    #: vendors at the first gate, ``any`` gates only on size and place.
    allows: str = "services"
    #: Headcount the lane accepts. Zero means unbounded on that side.
    size_min: int = 0
    size_max: int = 0
    #: Territory, in the lane's own words. Synonym matching means "United
    #: Kingdom" also accepts a page that says "London".
    locations: list[str] = []
    #: Industries the lane's candidates have to serve.
    verticals: list[str] = []
    #: Which rung fetches what, and what a candidate must satisfy to earn the
    #: next. Empty means the engine's default ladder.
    ladder: list[LadderRung] = []

    @field_validator("ladder", mode="before")
    @classmethod
    def rungs_from_data(cls, value: Any) -> Any:
        """Read the rungs a lane file writes as JSON objects."""
        if not isinstance(value, list):
            return value
        return [
            rung if isinstance(rung, LadderRung) else LadderRung(**dict(rung))
            for rung in value
        ]

    def gate_profile(self) -> dict[str, Any]:
        """The shape the gate engine reads, so a lane needs no adapter."""
        return {
            "allows": self.allows,
            "size_min": self.size_min,
            "size_max": self.size_max,
            "locations": tuple(self.locations),
            "verticals": tuple(self.verticals),
        }

    def rungs(self) -> list[LadderRung]:
        return list(self.ladder)


class Lane(ClosedModel):
    """One product's configuration over the shared engine."""

    name: str
    description: str = ""
    #: Anchors the lane starts from: a company, a vendor, a role family.
    seeds: list[str] = []
    queries: list[str] = []
    backends: list[str] = []
    channels: list[str] = []
    #: Vendor-published stories to gather per vendor as candidate entities. A
    #: vendor's own story index holds hundreds and each story is prose about a
    #: named company, which is the independent half of the evidence bar. Zero
    #: turns the source off.
    stories: int = 0
    #: Of those candidates, how many names to resolve to a domain. A name cannot
    #: be walked, and without walking there is no first-party evidence — the kind
    #: the bar requires — so this is what makes a large candidate set scorable.
    #: Each resolution is one search; zero leaves the candidates as names.
    resolve_stories: int = 0
    #: Values for the placeholders a lane's queries use, e.g.
    #: ``{"tech": ["Kafka", "dbt"], "vertical": ["fintech"]}``. One template
    #: becomes one query per combination, which is how a lane gathers from many
    #: sources instead of asking two questions and calling it research.
    query_terms: dict[str, list[str]] = {}
    #: Ceiling on expanded queries. Breadth costs a search per query, so the
    #: expansion is capped rather than left to the size of the term lists.
    max_queries: int = 40
    #: Which published story paths name the kind of company this lane wants.
    #: A vendor's ``/customers/`` index names firms that buy the product; its
    #: ``/partners/`` and award pages name the firms that implement it. Empty
    #: means every declared path.
    story_paths: list[str] = []
    #: Read the partner half of a vendor's ``<customer>-<partner>`` story slug.
    story_partner_half: bool = False
    #: Filters the lane cares about (e.g. "enterprise sales", "sales ops").
    title_include: list[str] = []
    title_exclude: list[str] = []
    remote: bool = False
    #: Scoring: which checklist, and the bar the lane demands.
    preset: str = "account-research"
    #: The tier ladder's floor, or None when the lane is not judged by it. The
    #: ladder asks what a *company* can prove about its vendor work; a lane whose
    #: rows are pages rather than companies (a role posting, a document) says so
    #: by leaving this unset and naming the evidence it does demand in
    #: ``require_kinds``.
    tier: str | None = "tier_2"
    require_kinds: list[str] = []
    #: The elimination half: what this lane accepts, and what a candidate has to
    #: satisfy to earn the next page visit. Defaults to the engine's ladder.
    funnel: LaneFunnel = LaneFunnel()
    #: Presentation.
    top: int = 25
    min_score: float | None = None
    revision: int = 1


def validate_lane(lane: Lane, *, channels: set[str] | None = None, backends: set[str] | None = None) -> None:
    """Everything that can be wrong with a lane, checked with named fields."""
    if not SLUG.fullmatch(lane.name):
        raise LaneError("name: must be a short slug (a-z, 0-9, _ or -)")
    if lane.preset not in PRESETS:
        raise LaneError(f"preset: '{lane.preset}' is not a preset (have: {', '.join(sorted(PRESETS))})")
    from .contracts import TIER_MINIMUMS

    if lane.tier is not None and lane.tier not in TIER_MINIMUMS:
        raise LaneError(f"tier: '{lane.tier}' is not a tier (have: {', '.join(sorted(TIER_MINIMUMS))})")
    from .contracts import EVIDENCE_KINDS

    for kind in lane.require_kinds:
        if kind not in EVIDENCE_KINDS:
            raise LaneError(f"require_kinds: '{kind}' is not an evidence kind")
    if backends is not None:
        for backend in lane.backends:
            if backend not in backends:
                raise LaneError(f"backends: '{backend}' is not a known backend or channel")
    if channels is not None:
        for channel in lane.channels:
            if channel not in channels:
                raise LaneError(
                    f"channels: '{channel}' is not installed (drop a file in <workspace>/sources/)"
                )
    if lane.top < 1:
        raise LaneError("top: must be at least 1")
    if lane.stories < 0:
        raise LaneError("stories: cannot be negative")
    if lane.resolve_stories < 0:
        raise LaneError("resolve_stories: cannot be negative")
    if lane.max_queries < 1:
        raise LaneError("max_queries: must be at least 1")
    for term, values in lane.query_terms.items():
        if not isinstance(values, list) or not values:
            raise LaneError(f"query_terms: '{term}' must be a non-empty list of values")
    if lane.min_score is not None and not 0 <= lane.min_score <= 100:
        raise LaneError("min_score: must be between 0 and 100")
    _validate_funnel(lane.funnel)


def _validate_funnel(funnel: LaneFunnel) -> None:
    """A lane whose gate vocabulary the engine cannot run is a lane that lies."""
    if funnel.allows not in ("services", "any"):
        raise LaneError(f"funnel.allows: '{funnel.allows}' is not a gate (have: services, any)")
    if funnel.size_min < 0 or funnel.size_max < 0:
        raise LaneError("funnel: size bounds cannot be negative")
    if funnel.size_max and funnel.size_min > funnel.size_max:
        raise LaneError(
            f"funnel: size_min {funnel.size_min} is above size_max {funnel.size_max}"
        )
    if funnel.ladder:
        try:
            validate_ladder(funnel.ladder)
        except ValueError as exc:
            raise LaneError(f"funnel.{exc}") from exc
        first = funnel.ladder[0]
        if first.evidence != SNIPPET:
            raise LaneError(
                "funnel.ladder[0]: the first rung reads what the search already "
                "returned; a lane may not open by fetching everything"
            )
        # Every gate this lane's funnel will actually run has to be carried by
        # some rung. Two gates can come back unresolved and never be settled,
        # unlike an elimination: the semantic one, and the cheap ones on a
        # candidate no fetch ever reached. A lane that carries neither leaves
        # such a lead unable to name its next step — it would go quiet instead of
        # saying what to fetch. The engine relies on this: it earns the first
        # rung carrying something open, with no fallback for a gate no rung knows.
        # A gate the profile leaves unset is not run at all, so it is not owed a
        # rung: `location` with no territory named never gates.
        # `kind` always gates. The rest gate only when the profile states
        # something to gate on: no territory named means the location gate never
        # runs, and a size range of zero means the size gate never runs. A lane
        # loaded here carries only its own defaults, so a gate it leaves unset is
        # still owed a rung — a person merges their profile in at run time.
        declarable = ("size", "location", "vertical")
        # Whether kind is gated at all is the lane's to say, exactly as it is in
        # the funnel: a lane that asks no kind question is not owed a rung for
        # one, and demanding it made the validator insist every lane run the
        # partner population test.
        gated = {"kind"} if funnel.allows in KIND_QUESTIONS else set()
        if funnel.size_min or funnel.size_max:
            gated.add("size")
        if funnel.locations:
            gated.add("location")
        if funnel.verticals:
            gated.add("vertical")

        carried = {gate for rung in funnel.ladder for gate in rung.gates}
        unearnable = sorted(gated - carried)
        if unearnable:
            raise LaneError(
                f"funnel.ladder: the funnel can leave {', '.join(unearnable)} "
                "unresolved for this lane, but no rung carries it, so a lead "
                "stalled on it could not name the fetch it had earned"
            )
        unknown_gates = sorted(carried - gated - set(declarable))
        if unknown_gates:
            raise LaneError(
                f"funnel.ladder: {', '.join(unknown_gates)} is gated on but the "
                "funnel never leaves it unresolved, so the rung settles nothing"
            )
        if "kind" in gated and "kind" not in {
            g for rung in funnel.ladder if rung.evidence == SNIPPET for g in rung.gates
        }:
            raise LaneError(
                "funnel.ladder: no rung gates on the kind of company at snippet "
                "grade, which is the one elimination a search result settles alone"
            )


def load_lane(path: Path | str, *, channels: set[str] | None = None,
              backends: set[str] | None = None) -> Lane:
    target = Path(path)
    try:
        payload: Any = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LaneError(f"{target}: cannot read ({exc})") from exc
    except ValueError as exc:
        raise LaneError(f"{target}: not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise LaneError(f"{target}: a lane is a JSON object")
    try:
        lane = Lane.model_validate(payload)
    except Exception as exc:  # pydantic names the field; keep that in the message
        raise LaneError(f"{target}: {exc}") from exc
    validate_lane(lane, channels=channels, backends=backends)
    return lane


def load_lanes(root: str | Path = ".", *, channels: set[str] | None = None,
               backends: set[str] | None = None) -> dict[str, Lane]:
    """Every lane the workspace defines, by name."""
    directory = Path(root) / LANE_DIRNAME
    lanes: dict[str, Lane] = {}
    if not directory.is_dir():
        return lanes
    for path in sorted(directory.glob("*.json")):
        lane = load_lane(path, channels=channels, backends=backends)
        if lane.name != path.stem:
            raise LaneError(f"{path.name}: lane name '{lane.name}' must match the file name")
        if lane.name in lanes:
            raise LaneError(f"duplicate lane '{lane.name}'")
        lanes[lane.name] = lane
    return lanes

#: Lanes that ship with the package. A workspace may override a shipped lane by
#: defining one with the same name in <workspace>/lanes/.
SHIPPED_LANE_DIR = Path(__file__).resolve().parent / "resources" / "lanes"


def shipped_lanes() -> dict[str, Lane]:
    """The products that come with the tool, as installed."""
    return load_lanes_dir(SHIPPED_LANE_DIR)


def load_lanes_dir(directory: Path) -> dict[str, Lane]:
    lanes: dict[str, Lane] = {}
    if not directory.is_dir():
        return lanes
    for path in sorted(directory.glob("*.json")):
        lane = load_lane(path)
        if lane.name != path.stem:
            raise LaneError(f"{path.name}: lane name '{lane.name}' must match the file name")
        lanes[lane.name] = lane
    return lanes


def load_available_lanes(root: str | Path = ".", *, channels: set[str] | None = None,
                         backends: set[str] | None = None) -> dict[str, Lane]:
    """Shipped lanes, with any workspace lane of the same name taking precedence."""
    available = shipped_lanes()
    workspace_dir = Path(root) / LANE_DIRNAME
    if workspace_dir.is_dir():
        for path in sorted(workspace_dir.glob("*.json")):
            lane = load_lane(path, channels=channels, backends=backends)
            if lane.name != path.stem:
                raise LaneError(f"{path.name}: lane name '{lane.name}' must match the file name")
            available[lane.name] = lane
    return available


def lane_source(root: str | Path = ".", name: str = "") -> str:
    """Where a lane came from: the workspace or the package."""
    return "workspace" if (Path(root) / LANE_DIRNAME / f"{name}.json").is_file() else "shipped"

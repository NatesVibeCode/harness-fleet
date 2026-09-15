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

from .models import ClosedModel
from .task import PRESETS

LANE_DIRNAME = "lanes"
SLUG = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class LaneError(ValueError):
    """A lane file is unusable, naming the field that has to change."""


class Lane(ClosedModel):
    """One product's configuration over the shared engine."""

    name: str
    description: str = ""
    #: Anchors the lane starts from: a company, a vendor, a role family.
    seeds: list[str] = []
    queries: list[str] = []
    backends: list[str] = []
    channels: list[str] = []
    #: Filters the lane cares about (e.g. "enterprise sales", "sales ops").
    title_include: list[str] = []
    title_exclude: list[str] = []
    remote: bool = False
    #: Scoring: which checklist, and the bar the lane demands.
    preset: str = "account-research"
    tier: str = "tier_2"
    require_kinds: list[str] = []
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

    if lane.tier not in TIER_MINIMUMS:
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
    if lane.min_score is not None and not 0 <= lane.min_score <= 100:
        raise LaneError("min_score: must be between 0 and 100")


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

"""The lanes that ship, and the guard that keeps one public repo clean."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harness_fleet import lanes as lane_module
from harness_fleet.channels import load_channels
from harness_fleet.discover import BACKENDS

REPO = Path(__file__).resolve().parents[1]


def test_every_shipped_lane_validates():
    """A lane that ships must be runnable as installed, with no workspace setup.

    The lanes live in package data, so this asserts on what an *install* carries
    — a product repo has no repo-root lanes directory and must still pass. That
    is now literally true: the repo-root `lanes/` directory was removed once it
    was found to be shadowing the packaged lanes with stale content. The package
    is the only copy, and `test_funnel_wiring.py` guards against a second one
    reappearing.
    """
    loaded = lane_module.shipped_lanes()
    assert loaded
    loaded = lane_module.load_available_lanes(REPO, channels=set(load_channels(REPO)), backends=set(BACKENDS))
    assert loaded, "the package ships lanes"
    for name, lane in loaded.items():
        assert lane.description, f"{name} explains what it is for"
        assert lane.queries or lane.seeds, f"{name} has something to search for"
        lane_module.validate_lane(lane, channels=set(), backends=set(BACKENDS))


def test_the_three_products_ship_as_lanes():
    assert set(lane_module.shipped_lanes()) >= {"account", "career", "partner"}


def test_the_career_lane_is_the_role_search_it_claims_to_be():
    """The lane that motivates the tuning plan: titles, remote, its own preset."""
    career = lane_module.shipped_lanes()["career"]
    assert "enterprise sales" in career.title_include
    assert career.remote is True
    assert career.preset == "triage"


def test_no_private_data_is_tracked_in_the_public_repo():
    """One public repo means the guard has to be absolute, not a habit.

    Career's databases, run artifacts and workspace registries belong to a
    person, not to the repository. If any of them is ever tracked, this fails
    before a commit can carry it out.
    """
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    forbidden = [
        path for path in tracked
        if path.endswith((".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3"))
        or path.startswith("runs/")
        or path.endswith("source_registry.json")
        or "/runs/" in path
    ]
    assert forbidden == [], f"private/runtime artifacts must not be tracked: {forbidden}"

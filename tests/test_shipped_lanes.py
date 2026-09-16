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


def test_the_account_lane_does_not_sweep_vendor_customer_indexes():
    """Accounts come from the lane's queries, not from software vendors' sites.

    The vendor-story sweep walks Snowflake/Databricks/Elastic/Datadog/MongoDB
    customer indexes — firms that *buy* the product, not firms doing the work
    the account lane hunts. Bulk-gathering them burned hundreds of page fetches
    on the wrong population, so the account lane leaves the stage off
    (`stories: 0` skips it in `research`) while its queries still find any
    customer story worth scoring.
    """
    account = lane_module.shipped_lanes()["account"]
    assert account.stories == 0, "account lane must not bulk-gather vendor stories"
    assert account.resolve_stories == 0


def test_the_partner_lane_finds_sis_directly_not_on_vendor_sites():
    """Net-new integrators are found, not harvested from a vendor's site.

    A partner already sitting on a vendor's customer-story index is by
    definition not net new, and those indexes name the vendors' buyers anyway.
    The lane therefore runs no vendor-story sweep (`stories: 0` skips it in
    `research`): candidates come from its queries, are qualified layer by layer
    on their own pages, and score on the tools they implement, the verticals
    they serve and the expertise they show.
    """
    partner = lane_module.shipped_lanes()["partner"]
    assert partner.stories == 0, "partner lane must not bulk-gather vendor stories"
    assert partner.resolve_stories == 0
    assert partner.story_partner_half is False


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

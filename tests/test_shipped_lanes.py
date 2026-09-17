"""The lanes that ship, and the guard that keeps one public repo clean."""
from __future__ import annotations

import re
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
        assert lane.queries, f"{name} has something to search for"
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
    assert career.preset == "career-research"


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


def _readme_lane_table() -> dict[str, tuple[str, str]]:
    """The lane table in the README, as {lane: (preset, bar)}.

    The bar cell is prose ("the tier ladder, floor `tier_3`" / "`delivery_hiring`"),
    so it comes back as written and the assertions below read it.
    """
    readme = (REPO / "README.md").read_text(encoding="utf8")
    rows: dict[str, tuple[str, str]] = {}
    for line in readme.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or not cells[0].startswith("`"):
            continue
        name = cells[0].strip("`")
        if name not in {"account", "career", "partner"}:
            continue
        rows[name] = (cells[2].strip("`"), cells[3])
    return rows


def test_the_readme_lane_table_matches_the_lanes_that_ship():
    """The table is how a reader decides which lane to run, so it must be true.

    It drifted: partner's documented floor said `tier_1` while the lane said
    `tier_2`. A reader then judged correct output as failing the lane's own bar —
    and the README was the stricter of the two, so the error was silent and in
    the direction of throwing away good records.
    """
    shipped = lane_module.shipped_lanes()
    documented = _readme_lane_table()
    assert documented, "the README still carries the lane table"

    assert set(documented) == set(shipped), (
        f"documented lanes {sorted(documented)} != shipped {sorted(shipped)}"
    )

    for name, (preset, bar) in documented.items():
        lane = shipped[name]
        assert preset == lane.preset, f"{name}: README preset {preset!r} != lane {lane.preset!r}"
        if lane.tier is None:
            # A lane that gates on evidence instead of the tier ladder names it.
            assert lane.require_kinds, f"{name} gates on the tier ladder or names its evidence"
            for kind in lane.require_kinds:
                assert kind in bar, f"{name}: README bar omits required evidence {kind!r}"
        else:
            assert lane.tier in bar, f"{name}: README bar omits the real floor {lane.tier!r}"
            for other in {"tier_1", "tier_2", "tier_3"} - {lane.tier}:
                assert other not in bar, f"{name}: README bar names {other} but the lane floors at {lane.tier}"


def test_every_documented_lane_name_resolves_or_is_a_placeholder():
    """Every `--lane <value>` a reader will copy must be a real lane name.

    Written after the first version of this test passed against a deliberate
    typo: it collected the names it found and then asserted those names existed
    in the set it had collected them from, which cannot fail. This version
    inverts it — every value must be a shipped lane or an explicit `<...>`
    placeholder, so a misspelling is a failure rather than a no-op.
    """
    shipped = set(lane_module.shipped_lanes())
    assert shipped, "there are lanes to name"

    # The README only: it is what a user copies. The design notes under docs/
    # discuss `--lane` in prose ("which lanes --lane can name"), so judging
    # their next word would flag English rather than a lane name.
    readme = (REPO / "README.md").read_text(encoding="utf8").replace("`", " ")
    looks_like_a_lane = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_-]*|<[^<>]+>)$")
    seen: dict[str, list[str]] = {}
    tokens = readme.split()
    for index, token in enumerate(tokens[:-1]):
        if token not in {"--lane", "--lane="}:
            continue
        value = tokens[index + 1].strip(".,;:")
        if looks_like_a_lane.match(value):
            seen.setdefault(value, []).append("README.md")

    assert seen, "the docs name lanes"
    unknown = {value: where for value, where in seen.items()
               if value not in shipped and not value.startswith("<")}
    assert unknown == {}, f"documented lane names that do not ship: {unknown}"
    # The placeholder is only honest if adding your own lane is possible, and the
    # README says where the file goes.
    if any(value.startswith("<") for value in seen):
        assert "lanes/<name>.json" in (REPO / "README.md").read_text(encoding="utf8"), (
            "an unnamed lane is only runnable if the docs say where to drop one"
        )


#: Every copy of the lane documentation a reader can land in. The README is
#: guarded above; these are the skills, which ship inside the package and in the
#: agent skill directories, and which said `tier_1` for a lane that floors at
#: `tier_2` long after the lane moved.
SKILL_LANE_DOCS = (
    "skills/harness-fleet/SKILL.md",
    ".agents/skills/harness-fleet/SKILL.md",
    "harness_fleet/resources/harness_skill/SKILL.md",
)
SKILL_BAR_DOCS = {
    "partner": (
        "skills/partner-fleet/SKILL.md",
        ".agents/skills/partner-fleet/SKILL.md",
        "harness_fleet/resources/partner_skill/SKILL.md",
    ),
}


def _skill_lane_rows(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        name = cells[0].strip("`")
        if name in {"account", "career", "partner"}:
            rows[name] = (cells[2].strip("`"), cells[3])
    return rows


def test_the_lane_tables_in_the_skills_match_the_lanes_that_ship():
    """The same table, in three more places, guarded the same way.

    A skill is the first thing a session reads, and it carries the lane table
    verbatim. Nothing compared those tables to the lanes, so partner's floor was
    documented as `tier_1` in all three copies while the lane said `tier_2`, and
    account's as `tier_2` while its lane says `tier_3` — both in the direction of
    a reader judging correct output as failing the lane's own bar.
    """
    shipped = lane_module.shipped_lanes()
    checked = 0
    for relative in SKILL_LANE_DOCS:
        path = REPO / relative
        assert path.is_file(), f"{relative} is part of the shipped surface"
        rows = _skill_lane_rows(path.read_text(encoding="utf8"))
        assert rows, f"{relative} carries the lane table"
        for name, (preset, bar) in rows.items():
            lane = shipped[name]
            assert preset == lane.preset, f"{relative}: {name} preset {preset!r}"
            if lane.tier is None:
                # Career gates on evidence rather than the tier ladder, so its
                # row names the evidence instead of a floor.
                for kind in lane.require_kinds:
                    assert kind in bar, f"{relative}: {name} omits {kind!r}"
                checked += 1
                continue
            assert lane.tier in bar, f"{relative}: {name} omits the real floor {lane.tier!r}"
            for other in {"tier_1", "tier_2", "tier_3"} - {lane.tier}:
                assert other not in bar, (
                    f"{relative}: {name} names {other} but the lane floors at {lane.tier}"
                )
            checked += 1
    assert checked >= 9, "three copies of a three-lane table"


def test_every_skill_bar_line_matches_its_lane():
    """`Bar:` in a lane skill is a claim about that lane, so it is checked."""
    shipped = lane_module.shipped_lanes()
    for name, relatives in SKILL_BAR_DOCS.items():
        lane = shipped[name]
        for relative in relatives:
            path = REPO / relative
            assert path.is_file(), f"{relative} is part of the shipped surface"
            text = path.read_text(encoding="utf8")
            line = next(
                (ln for ln in text.splitlines() if ln.startswith("- **Preset:**")), ""
            )
            assert line, f"{relative} states the lane's preset and bar"
            assert f"`{lane.preset}`" in line, f"{relative}: preset is {lane.preset!r}"
            if lane.tier:
                assert f"`{lane.tier}`" in line, f"{relative}: bar is {lane.tier!r}"
                for other in {"tier_1", "tier_2", "tier_3"} - {lane.tier}:
                    assert other not in line, f"{relative}: bar names {other}"

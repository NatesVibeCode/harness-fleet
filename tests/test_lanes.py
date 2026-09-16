"""A lane is configuration: what differs between products, and nothing more."""
import json

import pytest

from harness_fleet import lanes


def _write(tmp_path, name, **fields):
    (tmp_path / "lanes").mkdir(exist_ok=True)
    payload = {"name": name, **fields}
    (tmp_path / "lanes" / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_a_lane_carries_the_things_that_differ(tmp_path):
    _write(tmp_path, "career", description="enterprise sales/ops roles, remote",
           seeds=["enterprise software"], title_include=["enterprise sales", "sales ops"],
           remote=True, preset="triage", tier="tier_3", top=50)
    lane = lanes.load_lanes(tmp_path)["career"]
    assert lane.title_include == ["enterprise sales", "sales ops"]
    assert lane.remote is True
    assert lane.preset == "triage" and lane.tier == "tier_3" and lane.top == 50


def test_unknown_keys_are_refused(tmp_path):
    """Closed schema: a lane cannot smuggle in a mechanism override."""
    _write(tmp_path, "sneaky", score_multiplier=2.0)
    with pytest.raises(lanes.LaneError, match="score_multiplier"):
        lanes.load_lanes(tmp_path)


def test_each_bad_field_is_named(tmp_path):
    # The case directory is indexed, not hashed: `hash()` of a string varies per
    # process, so two cases could collide and the second mkdir would fail. A
    # test that fails once in a few runs is worse than no test.
    for index, (fields, expected) in enumerate((
        ({"preset": "no-such-preset"}, "preset"),
        ({"tier": "tier_9"}, "tier"),
        ({"require_kinds": ["vibes"]}, "require_kinds"),
        ({"top": 0}, "top"),
        ({"min_score": 140}, "min_score"),
    )):
        where = tmp_path / f"case{index}"
        where.mkdir()
        _write(where, "broken", **fields)
        with pytest.raises(lanes.LaneError) as err:
            lanes.load_lanes(where)
        assert expected in str(err.value), (fields, str(err.value))


def test_a_lane_must_match_its_file_name(tmp_path):
    (tmp_path / "lanes").mkdir()
    (tmp_path / "lanes" / "mismatch.json").write_text(
        json.dumps({"name": "other"}), encoding="utf-8")
    with pytest.raises(lanes.LaneError, match="must match the file name"):
        lanes.load_lanes(tmp_path)


def test_a_lane_can_only_use_channels_that_exist(tmp_path):
    _write(tmp_path, "account", channels=["industry"])
    with pytest.raises(lanes.LaneError, match="not installed"):
        lanes.load_lanes(tmp_path, channels=set())
    assert lanes.load_lanes(tmp_path, channels={"industry"})["account"].channels == ["industry"]


def test_an_empty_workspace_has_no_lanes(tmp_path):
    assert lanes.load_lanes(tmp_path) == {}


def test_a_lane_may_gate_on_its_own_evidence_instead_of_the_tier_ladder():
    """The tier ladder asks what a company proved about its vendor work.

    A lane whose rows are role postings is not answering that question, so it
    declares no floor and names the evidence it does demand. Leaving the field
    out entirely still means tier_2, so no existing lane changes meaning.
    """
    from harness_fleet.lanes import Lane, validate_lane

    default = Lane(name="account", preset="account-research")
    assert default.tier == "tier_2"

    career = Lane(name="career", preset="triage", tier=None, require_kinds=["delivery_hiring"])
    validate_lane(career)
    assert career.tier is None

    with pytest.raises(Exception) as excinfo:
        validate_lane(Lane(name="career", preset="triage", tier="tier_9"))
    assert "tier" in str(excinfo.value)

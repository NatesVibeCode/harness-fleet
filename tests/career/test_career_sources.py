import json
from types import SimpleNamespace

from career_fleet.cli import cmd_discover, cmd_init, cmd_sources
from career_fleet.community_sources import COMMUNITY_SOURCE_TYPES


def _discover_args(tmp_path, source="reddit", target=None, **overrides):
    values = {
        "source": source,
        "target": target,
        "preset": None,
        "config": "career_sources.json",
        "max": None,
        "db": str(tmp_path / "career.db"),
        "workspace_root": str(tmp_path),
        "profile": None,
        "subreddit": None,
        "reddit_rss": None,
        "subreddit_sort": None,
        "se_tagged": None,
        "se_site": None,
        "se_answers": None,
        "discourse_url": None,
        "lemmy_instance": None,
        "include_low_signal": False,
        "delay": None,
        "timeout": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_init_writes_visible_deterministic_source_plan(tmp_path):
    assert cmd_init(SimpleNamespace(db="career.db", workspace_root=str(tmp_path))) == 0

    config = json.loads((tmp_path / "career_sources.json").read_text())
    assert [entry["source"] for entry in config["sources"]] == list(COMMUNITY_SOURCE_TYPES)
    assert all(entry["max_items"] == 10 for entry in config["sources"])
    assert config["sources"][0]["reddit_rss"] is True
    assert next(entry for entry in config["sources"] if entry["source"] == "lobsters")["target"] == "job"


def test_discover_uses_preset_when_target_is_omitted(tmp_path, monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "success",
            "source_type": kwargs["source_type"],
            "target": kwargs["target"],
            "companies_discovered": 0,
            "postings_added": 0,
            "pages_skipped": 0,
            "community_signals_added": 0,
        }

    monkeypatch.setattr("career_fleet.cli.run_lane1_sourcing", fake_run)

    assert cmd_discover(_discover_args(tmp_path)) == 0
    assert captured["source_type"] == "reddit"
    assert captured["target"] == "career"
    assert captured["max_items"] == 10
    assert captured["reddit_rss"] is True
    assert captured["subreddit"] == ["forhire", "remotejs", "cscareerquestions", "experienceddevs"]


def test_community_discover_runs_all_enabled_presets(tmp_path, monkeypatch):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {
            "status": "success",
            "source_type": kwargs["source_type"],
            "target": kwargs["target"],
            "companies_discovered": 0,
            "postings_added": 0,
            "pages_skipped": 0,
            "community_signals_added": 0,
        }

    monkeypatch.setattr("career_fleet.cli.run_lane1_sourcing", fake_run)

    assert cmd_discover(_discover_args(tmp_path, source="community")) == 0
    assert [call["source_type"] for call in calls] == list(COMMUNITY_SOURCE_TYPES)
    assert [call["max_items"] for call in calls] == [10] * len(COMMUNITY_SOURCE_TYPES)


def test_community_discover_rejects_ambiguous_shared_target(tmp_path, capsys):
    assert cmd_discover(_discover_args(tmp_path, source="community", target="not-a-shared-target")) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_sources_can_write_and_list_the_plan(tmp_path, capsys):
    assert cmd_sources(SimpleNamespace(
        init=True,
        config="career_sources.json",
        workspace_root=str(tmp_path),
    )) == 0
    output = capsys.readouterr().out
    assert "reddit-career" in output
    assert "devto-career" in output

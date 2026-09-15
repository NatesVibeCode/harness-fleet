"""The registry and channels are drivable from the CLI, not just from Python."""
import json
from argparse import Namespace

import pytest

from harness_fleet import cli, registry


def _ns(tmp_path, command, **overrides):
    base = dict(
        sources_command=command, workspace_root=str(tmp_path), registry=None, json=True,
        domain=None, category=None, reason="", db=None,
    )
    base.update(overrides)
    return Namespace(**base)


def test_promote_then_list_round_trips(tmp_path, capsys):
    cli.cmd_sources(_ns(tmp_path, "promote", domain="vendorhub.example",
                        category="vendor_registry", reason="publishes partner stories"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["category"] == "vendor_registry"

    cli.cmd_sources(_ns(tmp_path, "list"))
    listed = json.loads(capsys.readouterr().out)
    assert listed["domains"]["vendorhub.example"]["category"] == "vendor_registry"
    assert listed["domains"]["vendorhub.example"]["reason"] == "publishes partner stories"
    assert listed["domains"]["vendorhub.example"]["promoted_by"] == "manual"
    # Written where the engine will read it.
    assert registry.lookup("vendorhub.example", tmp_path / "source_registry.json") == "vendor_registry"


def test_promote_refuses_a_category_the_taxonomy_does_not_have(tmp_path, capsys):
    with pytest.raises(ValueError, match="unknown category"):
        cli.cmd_sources(_ns(tmp_path, "promote", domain="x.example", category="secret_sauce"))
    assert not (tmp_path / "source_registry.json").exists(), "a refused promotion writes nothing"


def test_propose_reports_what_is_waiting(tmp_path, capsys):
    cli.cmd_sources(_ns(tmp_path, "propose"))
    empty = json.loads(capsys.readouterr().out)
    assert empty["count"] == 0

    for index in range(2):
        registry.observe(f"https://weak.example/x{index}/clutch",
                         path=tmp_path / "source_registry.json")
    cli.cmd_sources(_ns(tmp_path, "propose"))
    proposals = json.loads(capsys.readouterr().out)["proposals"]
    assert proposals[0]["domain"] == "weak.example"
    assert proposals[0]["category"] == "b2b_directory_audit"


def test_channels_lists_what_the_workspace_defines(tmp_path, capsys):
    cli.cmd_sources(_ns(tmp_path, "channels"))
    assert json.loads(capsys.readouterr().out)["count"] == 0

    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "industry.json").write_text(json.dumps({
        "name": "industry", "category": "b2b_directory_audit",
        "list_url": "https://directory.example/?q={query}",
    }), encoding="utf-8")
    cli.cmd_sources(_ns(tmp_path, "channels"))
    channels = json.loads(capsys.readouterr().out)
    assert channels["count"] == 1
    assert channels["channels"]["industry"]["category"] == "b2b_directory_audit"


def test_a_broken_channel_is_reported_not_swallowed(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "broken.json").write_text('{"name": "broken"}', encoding="utf-8")
    with pytest.raises(ValueError, match="must declare a category"):
        cli.cmd_sources(_ns(tmp_path, "channels"))


def test_a_promotion_changes_what_counts_as_evidence(tmp_path, capsys, monkeypatch):
    """The loop a person actually cares about: promote, then it is evidence."""
    from harness_fleet import sources

    # The registry resolves through the environment, so point it here before
    # promoting and the classification below will see the same file.
    monkeypatch.setenv("HARNESS_FLEET_REGISTRY", str(tmp_path / "source_registry.json"))
    cli.cmd_sources(_ns(tmp_path, "promote", domain="vendorhub.example", category="vendor_registry"))
    capsys.readouterr()
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert sources.classify_source_category("https://vendorhub.example/press/acme") == "vendor_registry"
    finally:
        os.chdir(cwd)

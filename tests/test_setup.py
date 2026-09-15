from pathlib import Path

import pytest

from harness_fleet.setup import bundled_skill_path, setup_workspace
from harness_fleet.store import HarnessStore


def test_setup_installs_bundled_skill_and_database_idempotently(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()

    first = setup_workspace(
        scope="user",
        workspace_root=workspace,
        home=home,
    )
    second = setup_workspace(
        scope="user",
        workspace_root=workspace,
        home=home,
    )

    assert first.actions[0].status == "created"
    assert second.actions[0].status == "unchanged"
    assert Path(first.skill_path, "SKILL.md").is_file()
    assert HarnessStore(first.database).schema_version() == "5"
    assert Path(first.stdio_server.command).stem in {"harness-fleet", "account-fleet", "career-fleet", "career-lanes"}
    assert Path(first.skill_path).with_name("account-fleet").joinpath("SKILL.md").is_file()
    # The partner skill ships with the partner-research preset, so setup must
    # install it too; leaving it behind made the preset unusable from a fresh
    # workspace.
    assert Path(first.skill_path).with_name("partner-fleet").joinpath("SKILL.md").is_file()
    assert first.database in first.stdio_server.args
    assert first.ready is False  # packaged route hints are not fresh price evidence


def test_user_scope_uses_portable_agents_directory(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    report = setup_workspace(
        scope="user",
        workspace_root=workspace,
        home=home,
        dry_run=True,
    )
    assert report.skill_path == str(home / ".agents/skills/harness-fleet")


def test_custom_skill_root_supports_nonstandard_harness(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    report = setup_workspace(
        scope="project",
        workspace_root=workspace,
        skill_root=".any-harness/skills",
        home=home,
        dry_run=True,
    )
    assert report.skill_path == str(workspace / ".any-harness/skills/harness-fleet")


def test_stdio_server_uses_current_python_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    environment = tmp_path / "tool-environment/bin"
    home.mkdir()
    workspace.mkdir()
    environment.mkdir(parents=True)
    executable = environment / "harness-fleet"
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr("harness_fleet.setup.sys.executable", str(environment / "python"))

    report = setup_workspace(
        scope="project",
        workspace_root=workspace,
        home=home,
        dry_run=True,
    )

    assert report.stdio_server.command == str(executable)


def test_setup_refuses_different_existing_skill_without_force(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    destination = home / ".agents/skills/harness-fleet"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("different")
    workspace.mkdir()

    with pytest.raises(FileExistsError, match="--force"):
        setup_workspace(
            scope="user",
            workspace_root=workspace,
            home=home,
        )


def test_setup_database_cannot_escape_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="workspace root"):
        setup_workspace(
            scope="project",
            workspace_root=workspace,
            db_path=tmp_path / "outside.db",
            home=tmp_path,
            dry_run=True,
        )


def test_packaged_skill_is_complete():
    root = bundled_skill_path()
    assert {
        Path("SKILL.md"),
        Path("references/operations.md"),
        Path("references/task-contracts.md"),
        Path("agents/openai.yaml"),
    } <= {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def test_all_distributed_skill_copies_match():
    packaged = bundled_skill_path()
    repository = Path(__file__).resolve().parents[1]
    expected = {path.relative_to(packaged): path.read_bytes() for path in packaged.rglob("*") if path.is_file()}
    for root in [repository / "skills/harness-fleet", repository / ".agents/skills/harness-fleet"]:
        actual = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        assert actual == expected


def test_partner_skill_is_bundled_and_every_copy_matches():
    repository = Path(__file__).resolve().parents[1]
    resources = repository / "harness_fleet/resources"
    packaged = resources / "partner_skill"
    expected = {p.relative_to(packaged): p.read_bytes() for p in packaged.rglob("*") if p.is_file()}
    assert Path("SKILL.md") in expected, "the partner skill must ship a SKILL.md"
    assert Path("references/discovery-playbook.md") in expected
    for root in [repository / "skills/partner-fleet", repository / ".agents/skills/partner-fleet"]:
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == expected


def test_partner_skill_files_are_declared_as_package_data():
    """A skill that is not in package-data is missing from the built wheel."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10, which this project still supports
        pytest.importorskip("tomli", reason="needs a TOML parser: tomllib (3.11+) or tomli")
        import tomli as tomllib  # type: ignore[no-redef]

    repository = Path(__file__).resolve().parents[1]
    config = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    declared = config["tool"]["setuptools"]["package-data"]["harness_fleet"]
    assert any(pattern.startswith("resources/partner_skill/") for pattern in declared)
    assert any(pattern.startswith("resources/account_skill/") for pattern in declared)


def test_account_skill_and_examples_are_bundled():
    repository = Path(__file__).resolve().parents[1]
    resources = repository / "harness_fleet/resources"
    expected = {p.relative_to(resources / "account_skill"): p.read_bytes() for p in (resources / "account_skill").rglob("*") if p.is_file()}
    assert expected
    for root in [repository / "skills/account-fleet", repository / ".agents/skills/account-fleet"]:
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == expected
    for name in ("task.json", "sample_accounts.csv"):
        assert (resources / "examples/account_research" / name).read_bytes() == (repository / "examples/account_research" / name).read_bytes()

def test_generated_commands_name_the_installed_cli(monkeypatch, tmp_path):
    """Setup must hand back the commands this install can actually run.

    An account-fleet install that is told to run `harness-fleet routes` is a
    broken first five minutes, which is what makes a release feel unusable.
    """
    from harness_fleet import setup

    monkeypatch.setattr(setup, "installed_cli_path", lambda: "/opt/fleet/bin/harness-fleet")
    report = setup.setup_workspace(scope="project", workspace_root=tmp_path, dry_run=True)
    assert report.stdio_server.command == "/opt/fleet/bin/harness-fleet"
    assert report.next_commands, "setup hands back the next steps to run"
    assert all(command[0] == "harness-fleet" for command in report.next_commands), report.next_commands
    assert all("/opt/fleet/bin" not in command[0] for command in report.next_commands)


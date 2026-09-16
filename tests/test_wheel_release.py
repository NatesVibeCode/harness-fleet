from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
CAREER_SKILL = "career_fleet/resources/skill/career-fleet/SKILL.md"


@pytest.fixture(scope="module")
def check_wheel():
    spec = importlib.util.spec_from_file_location("check_wheel", REPOSITORY / "scripts" / "check_wheel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=release@example.com", "-c", "user.name=Release", *args],
        cwd=repository, check=True, capture_output=True, text=True,
    ).stdout


def _mini_repository(root: Path) -> Path:
    repository = root / "mini"
    (repository / "pkg").mkdir(parents=True)
    (repository / "pyproject.toml").write_text("[project]\nname = 'mini'\n", encoding="utf-8")
    (repository / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "add", ".")
    return repository


def _indexed_files(repository: Path) -> set[str]:
    return set(_git(repository, "ls-files").splitlines())


def test_snapshot_excludes_untracked_and_ignored_files(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    (repository / "pkg" / "untracked.py").write_text("SECRET = 'untracked'\n", encoding="utf-8")
    (repository / "data.secret").write_text("SECRET = 'ignored'\n", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    check_wheel.source_snapshot(repository, snapshot, [])
    present = {path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()}
    assert present == _indexed_files(repository)
    assert "pkg/untracked.py" not in present and "data.secret" not in present


def test_snapshot_ships_working_bytes_not_index_bytes(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    (repository / "pkg" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    check_wheel.source_snapshot(repository, snapshot, [])
    assert (snapshot / "pkg" / "mod.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_snapshot_include_adds_one_intended_candidate(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    (repository / "pkg" / "new.resource.md").write_text("candidate\n", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    check_wheel.source_snapshot(repository, snapshot, [])
    assert not (snapshot / "pkg" / "new.resource.md").exists()
    included = tmp_path / "snapshot-included"
    check_wheel.source_snapshot(repository, included, ["pkg/new.resource.md"])
    assert (included / "pkg" / "new.resource.md").read_text(encoding="utf-8") == "candidate\n"


@pytest.mark.skipif(os.name == "nt", reason="symlink fixtures are POSIX-only")
def test_snapshot_refuses_symlink_escapes(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    (repository / "escape.py").symlink_to(tmp_path / "outside.txt")
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(repository, "add", "escape.py")
    with pytest.raises(ValueError, match="symlink"):
        check_wheel.source_snapshot(repository, tmp_path / "snapshot", [])


def test_snapshot_rejects_traversal_and_absolute_includes(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    with pytest.raises(ValueError, match="repository-relative"):
        check_wheel.source_snapshot(repository, tmp_path / "snapshot", ["../outside.txt"])
    with pytest.raises(ValueError, match="repository-relative"):
        check_wheel.source_snapshot(repository, tmp_path / "snapshot", [str(repository / "pyproject.toml")])


@pytest.mark.parametrize("name", ["pkg", "missing.md", ".git/config", "../outside"])
def test_invalid_include_fails_closed(check_wheel, tmp_path, name):
    repository = _mini_repository(tmp_path)
    with pytest.raises(ValueError):
        check_wheel.source_snapshot(repository, tmp_path / "snapshot", [name])


def test_deleted_indexed_file_fails_closed(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    (repository / "pkg/mod.py").unlink()
    with pytest.raises(ValueError, match="existing file"):
        check_wheel.source_snapshot(repository, tmp_path / "snapshot", [])


@pytest.mark.skipif(os.name == "nt", reason="newline filenames are POSIX-only")
def test_snapshot_handles_unusual_indexed_paths(check_wheel, tmp_path):
    repository = _mini_repository(tmp_path)
    name = "pkg/space café\tline\nbreak.py"
    (repository / name).write_bytes(b"candidate\n")
    _git(repository, "add", "--", name)
    check_wheel.source_snapshot(repository, tmp_path / "snapshot", [])
    assert (tmp_path / "snapshot" / name).read_bytes() == b"candidate\n"


def _write_skill_skeleton(snapshot: Path, present: set[str], wheel_module) -> None:
    for name in wheel_module.REQUIRED_RESOURCES:
        if name in present:
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"resource\n")


def test_missing_advertised_skill_fails_the_release_gate(check_wheel, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _write_skill_skeleton(snapshot, set(check_wheel.REQUIRED_RESOURCES) - {CAREER_SKILL}, check_wheel)
    with pytest.raises(ValueError, match="career_fleet/resources/skill/career-fleet/SKILL.md"):
        check_wheel.resource_manifest(snapshot)


def test_manifest_is_deterministic_for_identical_resources(check_wheel, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for snapshot in (first, second):
        snapshot.mkdir()
        _write_skill_skeleton(snapshot, set(check_wheel.REQUIRED_RESOURCES), check_wheel)
    assert check_wheel.resource_manifest(first) == check_wheel.resource_manifest(second)
    _write_skill_skeleton(second, set(), check_wheel)
    (second / CAREER_SKILL).write_bytes(b"changed\n")
    assert check_wheel.resource_manifest(first) != check_wheel.resource_manifest(second)


def test_gate_fails_closed_before_building(check_wheel, tmp_path, monkeypatch, capsys):
    repository = _mini_repository(tmp_path)
    _write_skill_skeleton(repository, set(check_wheel.REQUIRED_RESOURCES), check_wheel)
    _git(repository, "add", "harness_fleet")
    monkeypatch.setattr(check_wheel, "__file__", str(repository / "scripts/check_wheel.py"))
    monkeypatch.setattr(sys, "argv", ["check_wheel.py", "--distribution", "harness-fleet"])
    with pytest.raises(SystemExit) as exc:
        check_wheel.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert CAREER_SKILL in captured.err and "--include" in captured.err


def test_gate_builds_only_the_snapshot_with_explicit_includes(check_wheel, tmp_path, monkeypatch):
    repository = _mini_repository(tmp_path)
    _write_skill_skeleton(repository, set(check_wheel.REQUIRED_RESOURCES), check_wheel)
    _git(repository, "add", "harness_fleet")
    (repository / "extra.txt").write_text("candidate", encoding="utf-8")
    (repository / "data.secret").write_text("excluded", encoding="utf-8")
    real_run = subprocess.run

    class BuildReachedError(Exception):
        pass

    def stop_at_build(command, **kwargs):
        if list(command[:4]) == [sys.executable, "-m", "pip", "wheel"]:
            target = Path(command[4])
            assert target != repository and target.is_dir()
            assert (target / CAREER_SKILL).is_file()
            assert (target / "extra.txt").read_text(encoding="utf-8") == "candidate"
            assert not (target / "data.secret").exists()
            assert "--no-index" in command and "--no-build-isolation" in command
            raise BuildReachedError
        return real_run(command, **kwargs)

    monkeypatch.setattr(check_wheel.subprocess, "run", stop_at_build)
    monkeypatch.setattr(check_wheel, "__file__", str(repository / "scripts/check_wheel.py"))
    monkeypatch.setattr(sys, "argv", [
        "check_wheel.py", "--distribution", "harness-fleet", "--offline-system-deps",
        "--include", CAREER_SKILL, "--include", "extra.txt",
    ])
    with pytest.raises(BuildReachedError):
        check_wheel.main()

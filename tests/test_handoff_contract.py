from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_harness_drift as drift

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "fixtures" / "handoff-contracts.json"


@pytest.fixture
def contract():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def write_contract(tmp_path, contract):
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    return path


def test_snapshot_matches_without_runtime_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("contract gate attempted runtime access")

    from harness_fleet.providers.harness import CLIHarnessProvider

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("shutil.which", forbidden)
    monkeypatch.setattr("tempfile.mkdtemp", forbidden)
    monkeypatch.setattr(CLIHarnessProvider, "run_prompt", forbidden)
    monkeypatch.setattr(CLIHarnessProvider, "is_available", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    assert drift._check_handoff_contract(CONTRACT)


@pytest.mark.parametrize("name", drift.HANDOFF_PROVIDERS)
@pytest.mark.parametrize("field", drift.HANDOFF_SPEC_FIELDS)
@pytest.mark.parametrize("mutation", ["missing", "different", "wrong_type"])
def test_spec_mutations_fail_closed(tmp_path, contract, name, field, mutation, capsys):
    entry = contract["harnesses"][name]
    value = entry[field]
    if mutation == "missing":
        del entry[field]
    elif mutation == "wrong_type":
        entry[field] = int(value) if isinstance(value, bool) else {"invalid": value}
    elif isinstance(value, bool):
        entry[field] = not value
    elif isinstance(value, list):
        entry[field] = [*value, "--drift"]
    else:
        entry[field] = f"{value}-drift"
    assert not drift._check_handoff_contract(write_contract(tmp_path, contract))
    assert f"{name}.{field}" in capsys.readouterr().out


@pytest.mark.parametrize("name", drift.HANDOFF_PROVIDERS)
@pytest.mark.parametrize("mutation", ["append", "remove", "reorder", "string", "unknown_placeholder"])
def test_argv_mutations_fail_closed(tmp_path, contract, name, mutation, capsys):
    entry = contract["harnesses"][name]
    argv = entry["oneshot_argv"]
    if mutation == "append":
        argv.append("--drift")
    elif mutation == "remove":
        argv.pop()
    elif mutation == "reorder":
        argv.reverse()
    elif mutation == "string":
        entry["oneshot_argv"] = " ".join(argv)
    else:
        argv.append("<unknown>")
    assert not drift._check_handoff_contract(write_contract(tmp_path, contract))
    assert "FAIL   handoff contract" in capsys.readouterr().out


@pytest.mark.parametrize("name", drift.HANDOFF_PROVIDERS)
def test_adapter_argv_drift_is_detected(monkeypatch, name, capsys):
    module = importlib.import_module(f"harness_fleet.providers.{name}")
    provider = getattr(module, drift.HANDOFF_PROVIDERS[name])
    original = provider.build_argv

    def changed(self, **kwargs):
        return [*original(self, **kwargs), "--drift"]

    monkeypatch.setattr(provider, "build_argv", changed)
    assert not drift._check_handoff_contract(CONTRACT)
    assert f"{name}.oneshot_argv" in capsys.readouterr().out


@pytest.mark.parametrize("mutation", [
    "missing_provider", "extra_provider", "null_provider", "missing_version",
    "wrong_version", "bool_version", "missing_placeholders", "wrong_placeholders",
    "missing_harnesses", "list_harnesses", "missing_argv", "null_argv", "empty_argv",
    "nonstring_argv",
])
def test_invalid_contract_structure(tmp_path, contract, mutation):
    if mutation == "missing_provider":
        del contract["harnesses"]["codex"]
    elif mutation == "extra_provider":
        contract["harnesses"]["unknown"] = {}
    elif mutation == "null_provider":
        contract["harnesses"]["codex"] = None
    elif mutation == "missing_version":
        del contract["version"]
    elif mutation == "wrong_version":
        contract["version"] = 2
    elif mutation == "bool_version":
        contract["version"] = True
    elif mutation == "missing_placeholders":
        del contract["placeholders"]
    elif mutation == "wrong_placeholders":
        contract["placeholders"] = ["<unknown>"]
    elif mutation == "missing_harnesses":
        del contract["harnesses"]
    elif mutation == "list_harnesses":
        contract["harnesses"] = []
    elif mutation == "missing_argv":
        del contract["harnesses"]["codex"]["oneshot_argv"]
    else:
        contract["harnesses"]["codex"]["oneshot_argv"] = {
            "null_argv": None, "empty_argv": [], "nonstring_argv": [1],
        }[mutation]
    assert not drift._check_handoff_contract(write_contract(tmp_path, contract))


@pytest.mark.parametrize("text", ["{", "null", "[]", "1", '{"version":1,"version":1}', '{"version":NaN}'])
def test_invalid_json_fails_closed(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text, encoding="utf-8")
    assert not drift._check_handoff_contract(path)


def test_missing_unreadable_and_invalid_encoding_fail_closed(tmp_path):
    assert not drift._check_handoff_contract(tmp_path / "missing.json")
    assert not drift._check_handoff_contract(tmp_path)
    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    assert not drift._check_handoff_contract(path)


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_harness_drift.py"), "--self", "--no-tests", *args],
        cwd=ROOT,
        env={**os.environ, "HARNESS_FLEET_LIVE_TESTS": "0"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_cli_accepts_explicit_snapshot():
    result = run_cli("--handoff-contract", str(CONTRACT))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS   handoff contract" in result.stdout


@pytest.mark.parametrize("mutation", ["missing", "invalid", "drift"])
def test_cli_fails_for_explicit_bad_contract(tmp_path, contract, mutation):
    path = tmp_path / "contracts.json"
    if mutation == "invalid":
        path.write_text("not JSON", encoding="utf-8")
    elif mutation == "drift":
        contract["harnesses"]["codex"]["oneshot_argv"].append("--drift")
        path = write_contract(tmp_path, contract)
    result = run_cli("--handoff-contract", str(path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL   handoff contract" in result.stdout


def test_default_does_not_discover_handoff_contract(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default check attempted handoff discovery")

    monkeypatch.setattr(drift, "_run_handoff_contract", forbidden)
    monkeypatch.setattr(sys, "argv", ["check_harness_drift.py", "--self", "--no-tests"])
    assert drift.main() == 0


def test_default_still_runs_route_contract(monkeypatch):
    checked = []
    monkeypatch.setattr(drift, "_run_contract", lambda repo: checked.append(repo) or True)
    monkeypatch.setattr(sys, "argv", ["check_harness_drift.py", "--self"])
    assert drift.main() == 0
    assert checked == [Path.cwd().resolve()]

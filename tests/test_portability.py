"""Fresh-install and real-campaign safety regressions."""
import json
from pathlib import Path

import pytest

from harness_fleet import branding
from harness_fleet.catalog import RouteCatalog
from harness_fleet.cli import build_parser, cmd_mcp_install
from harness_fleet.input_data import InputDataError, load_input_items
from harness_fleet.models import RoutePolicy
from harness_fleet.packer import pack_items
from harness_fleet.store import HarnessStore
from harness_fleet.task import create_task_from_preset


def test_demo_is_never_selected_for_real_campaigns(tmp_path):
    catalog = RouteCatalog(db_path=tmp_path / "fleet.db")
    catalog.add_route("demo/fake", "demo", 0, 0)
    catalog.add_route("ollama/model", "ollama", 0, 0)
    assert catalog.get_ladder() == ["ollama/model"]
    assert catalog.get_ladder(policy=RoutePolicy(allowed_routes=["demo/fake"])) == ["demo/fake"]


def test_repeated_rate_limits_do_not_exhaust_batch_budget(tmp_path):
    store = HarnessStore(tmp_path / "fleet.db")
    spec = create_task_from_preset("test")
    revision = store.register_task(spec)
    store.create_run(run_id="run", task_revision_id=revision, input_path="test", input_digest="a" * 64,
                     total_items=1, max_attempts=2, batch_size=1, output_path=str(tmp_path / "out.json"))
    store.enqueue_batches("run", pack_items([{"item_id": "a", "text": "some useful source text"}]), 1)
    for _ in range(5):
        lease = store.lease_batch("run", "worker")
        assert lease is not None
        store.release_lease("run", lease["attempt_id"], "worker", "Rate limited")
    assert store.lease_batch("run", "worker") is not None
    assert store.reset_leased_batches("run") == 0
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM batch_attempts").fetchone()[0] == 6


def test_missing_survivor_file_fails_instead_of_silently_emptying_campaign(tmp_path):
    source = tmp_path / "accounts.csv"
    source.write_text("id,text\na,Some source text\n", encoding="utf-8")
    with pytest.raises(InputDataError, match="not found"):
        load_input_items(source, only_ids=tmp_path / "missing.csv")


def test_cli_prog_and_mcp_install_use_this_distributions_name(tmp_path, monkeypatch):
    """Each product's CLI introduces itself as that product, engine and all."""
    import harness_fleet.cli as cli_module
    from harness_fleet import branding

    assert cli_module.build_parser().prog == branding.CLI_NAME
    monkeypatch.setattr("sys.argv", [branding.CLI_NAME, "doctor"])
    assert cli_module._package_version()

    config = tmp_path / "mcp.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    (tmp_path / "harness-fleet.db").touch()
    args = build_parser().parse_args(
        ["mcp", "install", "--client", "cursor", "--workspace-root", str(tmp_path),
         "--db", "harness-fleet.db", "--json"]
    )
    cmd_mcp_install(args)
    installed = json.loads(config.read_text(encoding="utf-8"))
    assert branding.CLI_NAME in installed["mcpServers"]
    assert "free-fleet" not in installed["mcpServers"]


def test_mcp_install_preserves_invalid_existing_config(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text("{broken config", encoding="utf-8")
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    args = build_parser().parse_args(["mcp", "install", "--client", "cursor", "--workspace-root", str(tmp_path), "--json"])
    with pytest.raises(ValueError, match="config"):
        cmd_mcp_install(args)
    assert config.read_text(encoding="utf-8") == "{broken config"


def _install_args(tmp_path, *extra: str):
    return build_parser().parse_args(
        ["mcp", "install", "--client", "cursor", "--workspace-root", str(tmp_path),
         "--db", "harness-fleet.db", *extra]
    )


def test_mcp_install_passes_requested_env_into_the_client_config(tmp_path, monkeypatch):
    """Desktop apps do not inherit the shell environment, so --env must carry keys."""
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-not-real")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")

    cmd_mcp_install(_install_args(
        tmp_path, "--env", "OPENROUTER_API_KEY,GROQ_API_KEY", "--env", "OLLAMA_BASE_URL", "--json",
    ))

    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][branding.CLI_NAME]
    assert entry["env"] == {
        "OPENROUTER_API_KEY": "sk-or-test-not-real",
        "GROQ_API_KEY": "gsk-test-not-real",
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
    }


def test_mcp_install_skips_unset_env_but_still_installs(tmp_path, monkeypatch, capsys):
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    cmd_mcp_install(_install_args(tmp_path, "--env", "OPENROUTER_API_KEY", "--env", "GROQ_API_KEY"))

    out = capsys.readouterr().out
    assert "env: OPENROUTER_API_KEY (set)" in out
    assert "GROQ_API_KEY requested but not set in this shell; skipping" in out
    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][branding.CLI_NAME]
    assert entry["env"] == {"OPENROUTER_API_KEY": "sk-or-test-not-real"}
    assert "GROQ_API_KEY" not in json.dumps(entry)


def test_mcp_install_without_env_writes_no_env_block(tmp_path, monkeypatch, capsys):
    """Regression guard: the default entry stays exactly as it is today."""
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")

    cmd_mcp_install(_install_args(tmp_path))

    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][branding.CLI_NAME]
    assert "env" not in entry
    # Opt-in only: an ambient key is never written silently, but the user is told.
    assert "not passed to the client" in capsys.readouterr().out


def test_mcp_install_reports_env_keys_a_plain_rerun_drops(tmp_path, monkeypatch, capsys):
    """Without --env the entry keeps today's shape, so say which keys that removes."""
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    cmd_mcp_install(_install_args(tmp_path, "--env", "OPENROUTER_API_KEY", "--json"))
    capsys.readouterr()

    cmd_mcp_install(_install_args(tmp_path))

    assert "env: OPENROUTER_API_KEY dropped" in capsys.readouterr().out
    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][branding.CLI_NAME]
    assert "env" not in entry


def test_mcp_install_never_prints_env_values(tmp_path, monkeypatch, capsys):
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")

    cmd_mcp_install(_install_args(tmp_path, "--env", "OPENROUTER_API_KEY", "--json", "--dry-run"))
    dry_run_out = capsys.readouterr().out
    assert not config.exists()
    assert "OPENROUTER_API_KEY" in dry_run_out
    payload = json.loads(dry_run_out)
    assert payload["installed"][0]["server"]["env"] == {"OPENROUTER_API_KEY": "<redacted>"}

    cmd_mcp_install(_install_args(tmp_path, "--env", "OPENROUTER_API_KEY", "--json"))
    assert "sk-or-test-not-real" not in capsys.readouterr().out


def test_csv_survivor_ids_match_the_same_normalized_ids(tmp_path):
    source = tmp_path / "accounts.csv"
    source.write_text("id,text\nhttps://example.com,Some source text\n", encoding="utf-8")
    assert len(load_input_items(source, only_ids={"https://example.com"})) == 1


def test_doctor_uses_selected_workspace_and_accepts_local_provider(tmp_path, monkeypatch, capsys):
    from harness_fleet.cli import cmd_doctor
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("HARNESS_FLEET_DB", raising=False)
    catalog = RouteCatalog(db_path=tmp_path / "harness-fleet.db")
    catalog.add_route("ollama/model", "ollama", 0, 0)
    cmd_doctor(build_parser().parse_args(["doctor", "--workspace-root", str(tmp_path), "--json"]))
    result = json.loads(capsys.readouterr().out)
    assert result["ready"]
    assert Path(result["database"]) == (tmp_path / "harness-fleet.db").resolve()


@pytest.mark.parametrize("platform,subpath", [("win32", "AppData/Roaming/Claude"), ("linux", ".config/Claude"), ("darwin", "Library/Application Support/Claude")])
def test_client_config_default_matches_operating_system(tmp_path, monkeypatch, platform, subpath):
    from harness_fleet.cli import _claude_config_candidates
    monkeypatch.setattr("harness_fleet.cli.sys.platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert _claude_config_candidates()[0] == tmp_path / subpath / "claude_desktop_config.json"


def _codex_install_args(tmp_path, *extra: str):
    return build_parser().parse_args(
        ["mcp", "install", "--client", "codex", "--workspace-root", str(tmp_path),
         "--db", "harness-fleet.db", *extra]
    )


def test_mcp_install_writes_codex_toml_section(tmp_path, monkeypatch):
    """Codex speaks TOML: the installer appends a section, never JSON."""
    config = tmp_path / "config.toml"
    config.write_text("[mcp_servers.other]\ncommand = \"other\"\n", encoding="utf-8")
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    (tmp_path / "harness-fleet.db").touch()
    cmd_mcp_install(_codex_install_args(tmp_path, "--json"))
    text = config.read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in text
    assert f'[mcp_servers."{branding.CLI_NAME}"]' in text
    assert "command = " in text


def test_mcp_install_codex_is_append_only(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    (tmp_path / "harness-fleet.db").touch()
    cmd_mcp_install(_codex_install_args(tmp_path, "--json"))
    once = config.read_text(encoding="utf-8")
    cmd_mcp_install(_codex_install_args(tmp_path, "--json"))
    assert config.read_text(encoding="utf-8") == once
    assert once.count(f'[mcp_servers."{branding.CLI_NAME}"]') == 1


def test_mcp_install_codex_carries_env(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    (tmp_path / "harness-fleet.db").touch()
    cmd_mcp_install(_codex_install_args(tmp_path, "--env", "OPENROUTER_API_KEY", "--json"))
    text = config.read_text(encoding="utf-8")
    assert f'[mcp_servers."{branding.CLI_NAME}".env]' in text
    assert 'OPENROUTER_API_KEY = "sk-test-not-real"' in text


def test_mcp_install_muse_uses_json_mcpservers(tmp_path, monkeypatch):
    config = tmp_path / "settings.json"
    monkeypatch.setattr("harness_fleet.cli._existing_mcp_path_for_client", lambda *a: config)
    (tmp_path / "harness-fleet.db").touch()
    args = build_parser().parse_args(
        ["mcp", "install", "--client", "muse", "--workspace-root", str(tmp_path),
         "--db", "harness-fleet.db", "--json"]
    )
    cmd_mcp_install(args)
    assert branding.CLI_NAME in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]


def test_mcp_install_rejects_unknown_client(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["mcp", "install", "--client", "nope", "--workspace-root", str(tmp_path)]
        )


@pytest.mark.parametrize("name", ["openrouter", "openai_compatible"])
def test_different_worker_timeouts_do_not_close_an_active_http_client(monkeypatch, name):
    import importlib
    module = importlib.import_module("harness_fleet.providers." + name)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(module, "_shared_client", None)
    first = module._shared_httpx_client(5)
    try:
        assert module._shared_httpx_client(30) is first
        assert not first.is_closed
    finally:
        first.close()

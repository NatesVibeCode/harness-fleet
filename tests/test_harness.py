"""Spec tests for the universal CLI-harness base (H0; opencode behavior unchanged)."""
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness_fleet.models import RouteId
from harness_fleet.providers.harness import (
    CLIHarnessProvider,
    HarnessSpec,
    LocalHarnessCLI,
    MalformedRouteError,
    parse_json_object_stdout,
)


class StubRunner:
    def __init__(self, code=0, stdout="", stderr=""):
        self.seen: dict[str, Any] = {}
        self.code = code
        self.stdout = stdout
        self.stderr = stderr

    def run(self, *, argv, stdin_text, timeout_sec):
        self.seen = {"argv": argv, "stdin_text": stdin_text, "timeout_sec": timeout_sec}
        return self.code, self.stdout, self.stderr


class FakeProvider(CLIHarnessProvider):
    def build_argv(self, *, model, prompt, prompt_file, workspace=None, workdir=None):
        parts = ["run", "--model", model]
        if prompt_file is not None:
            parts += ["--prompt-file", prompt_file]
        elif self.spec.prompt_delivery == "argv_last":
            parts.append(prompt)
        return parts

    def parse_output(self, *, code, stdout, stderr, receipt, started, workdir=None):
        return parse_json_object_stdout(stdout, receipt=receipt)


def _spec(**overrides):
    base = {"name": "fake", "binary": "fake-harness-bin", "prompt_delivery": "argv_last"}
    base.update(overrides)
    return HarnessSpec(**base)


def test_route_id_parse_table():
    assert RouteId.parse("harness/model") == RouteId(provider="harness", model="model")
    assert RouteId.parse("harness:model") == RouteId(provider="harness", model="model")
    assert RouteId.parse("openrouter/foo/bar:free") == RouteId(
        provider="openrouter", model="foo/bar:free"
    )
    assert RouteId.parse("  harness/model  ") == RouteId(provider="harness", model="model")
    bare = RouteId.parse("harness")
    assert bare == RouteId(provider="harness", model=None)
    assert str(bare) == "harness"
    assert str(RouteId.parse("harness:model")) == "harness/model"
    for bad in ["", "   ", "/leading", "trailing/", "a:", ":b", 123, None]:
        with pytest.raises(MalformedRouteError):
            RouteId.parse(bad)


def test_derive_model_requires_model_when_routed():
    provider = FakeProvider(_spec(), runner=StubRunner())
    assert provider.derive_model("fake/model") == "model"
    with pytest.raises(MalformedRouteError):
        provider.derive_model("noseparator")


def test_spec_requires_file_flag_for_file_delivery():
    with pytest.raises(ValueError):
        HarnessSpec(name="x", binary="x", prompt_delivery="file_flag")
    assert _spec(prompt_delivery="file_flag", prompt_file_flag="--prompt-file").prompt_file_flag == "--prompt-file"


def test_argv_is_always_a_typed_list():
    runner = StubRunner(stdout=json.dumps({"result": "ok text"}))
    provider = FakeProvider(_spec(), runner=runner)
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, text, _ = provider.run_prompt("fake/model", "do the thing")
    assert ok is True and text == "ok text"
    argv = runner.seen["argv"]
    assert isinstance(argv, list) and all(isinstance(part, str) for part in argv)
    assert argv[-1] == "do the thing"  # ONE trailing element, never split
    assert runner.seen["stdin_text"] is None


def test_stdin_delivery_keeps_prompt_out_of_argv():
    runner = StubRunner(stdout=json.dumps({"result": "stdin ok"}))
    provider = FakeProvider(_spec(prompt_delivery="stdin"), runner=runner)
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, _, _ = provider.run_prompt("fake/model", "line1\nline2 --x")
    assert ok is True
    assert runner.seen["stdin_text"] == "line1\nline2 --x"
    assert all("line1" not in part for part in runner.seen["argv"])


def test_file_delivery_stages_exact_prompt():
    runner = StubRunner(stdout=json.dumps({"result": "file ok"}))
    provider = FakeProvider(
        _spec(prompt_delivery="file_flag", prompt_file_flag="--prompt-file"), runner=runner
    )
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, _, _ = provider.run_prompt("fake/model", "exact bytes \n --flag")
    assert ok is True
    argv = runner.seen["argv"]
    path = Path(argv[argv.index("--prompt-file") + 1])
    assert not path.exists()  # staged file is cleaned up after the run
    assert runner.seen["stdin_text"] is None


def test_bare_route_id_allowed_when_model_ignored():
    runner = StubRunner(stdout=json.dumps({"result": "ok"}))
    provider = FakeProvider(_spec(model_from_route=False), runner=runner)
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, text, _ = provider.run_prompt("fake", "prompt")
    assert ok is True and text == "ok"


def test_lockdown_strategy_fails_closed():
    runner = StubRunner(stdout=json.dumps({"result": "ok"}))
    provider = FakeProvider(_spec(task_config_strategy="cursor_sandbox_enabled"), runner=runner)
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, _, receipt = provider.run_prompt("fake/model", "prompt")
    assert ok is False
    assert "lockdown" in (receipt.error or "")


def test_malformed_route_fails_closed():
    runner = StubRunner(stdout=json.dumps({"result": "never"}))
    provider = FakeProvider(_spec(), runner=runner)
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, text, receipt = provider.run_prompt("noseparator", "prompt")
    assert ok is False and text is None
    assert receipt.status == "failed" and receipt.error


def test_timeout_receipt():
    class TimeoutRunner:
        def run(self, *, argv, stdin_text, timeout_sec):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_sec)

    provider = FakeProvider(_spec(), runner=TimeoutRunner())  # type: ignore[arg-type]
    provider._preflight = lambda: None  # type: ignore[method-assign]
    ok, text, receipt = provider.run_prompt("fake/model", "prompt")
    assert ok is False and text is None
    assert receipt.error_type == "timeout"


def test_missing_binary_fails_closed_without_running():
    def _boom(**kwargs):
        raise AssertionError("runner must not run without a binary")

    provider = FakeProvider(_spec(binary="definitely-not-installed-xyz"), runner=_boom)  # type: ignore[arg-type]
    ok, text, receipt = provider.run_prompt("fake/model", "prompt")
    assert ok is False and text is None
    assert "not found in PATH" in (receipt.error or "")


def test_local_runner_uses_shell_false(monkeypatch):
    captured = {}

    def fake_run(argv, input, capture_output, text, timeout, shell, env=None):
        captured["shell"] = shell
        captured["argv"] = argv
        import subprocess as _sp

        return _sp.CompletedProcess(argv, 0, stdout='{"result": "hi"}', stderr="")

    monkeypatch.setattr("harness_fleet.providers.harness.subprocess.run", fake_run)
    code, stdout, _ = LocalHarnessCLI().run(argv=["bin", "a b"], stdin_text=None, timeout_sec=5)
    assert captured["shell"] is False and captured["argv"] == ["bin", "a b"]
    assert code == 0 and json.loads(stdout)["result"] == "hi"


def test_conservative_parser_rejects_garbage():
    from harness_fleet.providers.harness import ProviderReceipt

    receipt = ProviderReceipt(
        id="recept-test", provider="fake", requested_route="fake/model",
        status="failed", error_type="inference_error",
    )
    ok, text, receipt = parse_json_object_stdout('{"unrelated": 1}', receipt=receipt)
    assert ok is False and text is None
    assert receipt.error_type == "inference_error"


def test_a_harness_that_cannot_write_its_state_gets_a_writable_home(tmp_path, monkeypatch):
    """Discovery and inference both need it, and it must carry the login.

    opencode opens `$XDG_DATA_HOME/opencode/log/opencode.log` before it answers,
    and under the file sandbox that write is denied — so `opencode models` failed
    with `FileSystem.open(.../opencode.log)` and the registry came back empty.
    The same denial killed every `opencode run`. The machine had 84 models
    reachable through that CLI, twenty of them verified-free OpenRouter routes,
    and the fleet reported "no verified-free route" and "opencode is out".
    """
    from harness_fleet.providers.harness import HarnessSpec, discovery_env

    plain = HarnessSpec(
        name="plain", binary="true", prompt_delivery="argv_last", discovery_argv=["models"]
    )
    assert discovery_env(plain) is None, "a harness that needs no writable state inherits"

    real_home = tmp_path / "real"
    (real_home / ".local" / "share" / "opencode").mkdir(parents=True)
    (real_home / ".local" / "share" / "opencode" / "auth.json").write_text('{"k":"v"}')
    (real_home / ".config" / "opencode").mkdir(parents=True)
    (real_home / ".config" / "opencode" / "config.json").write_text("{}")
    monkeypatch.setenv("HOME", str(real_home))

    spec = HarnessSpec(
        name="opencode-test",
        binary="true",
        prompt_delivery="argv_last",
        discovery_argv=["models"],
        writable_home=True,
        carry_over=(".local/share/opencode/auth.json", ".config/opencode"),
    )
    env = discovery_env(spec)
    assert env is not None
    assert env["HOME"] != str(real_home), "a writable stand-in, not the sandboxed home"
    # The credential came across: without it the CLI answers with a fraction of
    # its models and none of the passthrough routes.
    carried = Path(env["HOME"]) / ".local" / "share" / "opencode" / "auth.json"
    assert carried.read_text() == '{"k":"v"}'
    assert (Path(env["HOME"]) / ".config" / "opencode" / "config.json").is_file()
    assert env["XDG_DATA_HOME"].startswith(env["HOME"])
    # One stand-in per harness per process, not one per call.
    assert discovery_env(spec) is env

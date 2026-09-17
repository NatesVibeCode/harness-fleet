"""OpenCode CLI-harness adapter: spec + parser over the shared base.

CLI contract (verified against the installed CLI): ``opencode run --dir W
--format json [--model M] [--file F] MSG`` with the prompt as the trailing
argv message; JSONL ``text`` / ``step_finish`` / ``error`` events; discovery
via ``opencode models``.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

from .base import BaseProvider
from .harness import CLIHarnessProvider, HarnessSpec, ProviderReceipt, classify_failure

OPENCODE_SPEC = HarnessSpec(
    name="opencode",
    binary="opencode",
    prompt_delivery="argv_last",
    parser="opencode_jsonl",
    task_config_strategy="opencode_deny_all",
    # No namespace argument: opencode's registry carries the OpenRouter-backed
    # models too (``openrouter/<model>:free``), and those are the no-key way to
    # reach OpenRouter. Route ids become ``opencode/openrouter/<model>:free``
    # and the CLI resolves them through the opencode login.
    discovery_argv=["models", "--verbose"],
    model_from_route=True,
    # opencode opens its log under $XDG_DATA_HOME/opencode before it answers, and
    # the sandbox denies that write, so discovery failed with FileSystem.open and
    # the registry came back with only the packaged hints. It also keeps its
    # login in that same directory: the stand-in HOME has to carry `auth.json`
    # and the config, or the OpenRouter passthrough never appears.
    writable_home=True,
    carry_over=(".local/share/opencode/auth.json", ".config/opencode"),
)


class OpenCodeRunner(Protocol):
    def run(self, task_config: dict, args: list[str], timeout_sec: int) -> tuple[int, str, str]: ...


class LocalOpenCodeCLI:
    """Invoke OpenCode normally while applying a task-local no-tools config."""

    def run(self, task_config: dict, args: list[str], timeout_sec: int) -> tuple[int, str, str]:
        from .harness import per_call_home

        # opencode opens its log before it reads a prompt, so without a writable
        # HOME every call fails on the log write — which is what "opencode is out"
        # actually was. Each call gets its own state directory, because several
        # calls now answer one prompt at once and one shared SQLite file answers
        # them all with "database is locked".
        with per_call_home(OPENCODE_SPEC) as env:
            with tempfile.TemporaryDirectory(prefix="harness-fleet-opencode-") as temp_dir:
                Path(temp_dir, "opencode.json").write_text(json.dumps(task_config, indent=2))
                completed = subprocess.run(
                    ["opencode", *args],
                    cwd=temp_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    env=env,
                )
        return completed.returncode, completed.stdout, completed.stderr


def parse_opencode_events(
    stdout: str,
    stderr: str,
    code: int,
    *,
    receipt: ProviderReceipt,
) -> tuple[bool, str | None, ProviderReceipt]:
    """Parse OpenCode ``--format json`` JSONL events into (ok, text, receipt)."""
    texts = []
    finished = False
    costs = []
    last_err = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        part = event.get("part", {})

        if event_type == "text":
            texts.append(part.get("text", ""))
        elif event_type == "step_finish":
            finished = True
            c = part.get("cost")
            if c is not None:
                costs.append(c)
            receipt.usage = part.get("tokens")
        elif event_type == "error":
            last_err = str(event.get("error", ""))[:500]

    if code != 0 or not finished or not texts:
        err_msg = last_err or (stderr or stdout)[-500:] or f"Exit code {code}"
        receipt.error = err_msg
        receipt.error_type = classify_failure(err_msg)  # type: ignore[assignment]
        if receipt.error_type == "rate_limit":
            receipt.retry_after = 10.0
        return False, None, receipt

    if costs and all(isinstance(c, (int, float)) for c in costs):
        total_cost = float(sum(costs))
        receipt.cost = total_cost
        receipt.cost_status = "reported_zero" if total_cost == 0 else "billed"
    receipt.status = "complete"
    return True, "\n".join(texts), receipt


class OpenCodeProvider(CLIHarnessProvider):
    def __init__(self, runner: OpenCodeRunner | None = None):
        super().__init__(OPENCODE_SPEC)
        # Narrower than the base HarnessRunner: _invoke bridges the legacy
        # (task_config, args) protocol, so the base runner is never used here.
        self.runner: OpenCodeRunner = runner or LocalOpenCodeCLI()  # type: ignore[assignment]

    def _preflight(self) -> str | None:
        # Legacy flow: no binary probe here. A missing binary surfaces as a
        # runner error receipt, exactly as before the universal base.
        return None

    def derive_model(self, route_id: str) -> str:
        # Tested native-prefix rule: strip a leading "opencode/" (or
        # "opencode:") namespace only; a bare "opencode/<model>" id keeps its
        # native provider prefix for discovery-shaped ids.
        actual_model = route_id
        if actual_model.startswith("opencode/") and "/" in actual_model[len("opencode/"):]:
            actual_model = actual_model[len("opencode/"):]
        elif actual_model.startswith("opencode:"):
            actual_model = actual_model[len("opencode:"):]
        return actual_model

    def _task_config_for(self, model: str) -> dict[str, Any]:
        # Task-local OpenCode configuration: normal CLI auth, no model tools or MCP.
        return {
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "permission": {"*": "deny"},
            "mcp": {},
            "share": "disabled",
        }

    def build_argv(
        self,
        *,
        model: str,
        prompt: str,
        prompt_file: str | None,
        workspace: Path | None = None,
        workdir: Path | None = None,
    ) -> list[str]:
        return ["run", "--format", "json", "--model", model, prompt]

    def _invoke(
        self,
        *,
        argv: list[str],
        stdin_text: str | None,
        task_config: dict[str, Any] | None,
        timeout_sec: int,
    ) -> tuple[int, str, str]:
        if task_config is None:
            raise RuntimeError("opencode task config staging failed")
        return self.runner.run(
            task_config=task_config,
            args=argv[1:],
            timeout_sec=timeout_sec,
        )

    def parse_output(
        self,
        *,
        code: int,
        stdout: str,
        stderr: str,
        receipt: ProviderReceipt,
        started: float,
        workdir: Path | None = None,
    ) -> tuple[bool, str | None, ProviderReceipt]:
        return parse_opencode_events(stdout, stderr, code, receipt=receipt)


assert issubclass(OpenCodeProvider, BaseProvider)

"""Cursor CLI-harness adapter: spec + parser over the shared base.

CLI contract (verified against the installed CLI): ``cursor-agent --workspace W
--print --output-format json TASK`` with the prompt as ONE argv element
(the CLI advertises no prompt-file flag; long prompts travel as a single
argument, never shell text). ``--model`` selects an explicit model from
``cursor-agent models`` discovery, and ``--sandbox enabled`` is the
documented lockdown for unattended runs. ``--resume`` continues an
exact chat and is documented but never executed: the engine fresh-runs
every prompt.
"""
from __future__ import annotations

from pathlib import Path

from .harness import (
    CLIHarnessProvider,
    HarnessSpec,
    ProviderReceipt,
    parse_json_object_stdout,
    run_error_receipt,
)

CURSOR_SPEC = HarnessSpec(
    name="cursor",
    binary="cursor-agent",
    prompt_delivery="argv_last",
    parser="json_object",
    task_config_strategy="cursor_sandbox_enabled",
    discovery_argv=["models"],
    model_from_route=True,
)


class CursorProvider(CLIHarnessProvider):
    def __init__(self, runner=None):
        super().__init__(CURSOR_SPEC, runner=runner)

    def build_argv(
        self,
        *,
        model: str,
        prompt: str,
        prompt_file: str | None,
        workspace: Path | None = None,
        workdir: Path | None = None,
    ) -> list[str]:
        return [
            "--workspace", str(workspace or Path.cwd()),
            "--sandbox", "enabled",
            "--print",
            "--output-format", "json",
            "--model", model,
            prompt,
        ]

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
        if code != 0:
            return run_error_receipt(code, stdout, stderr, receipt=receipt)
        return parse_json_object_stdout(stdout, receipt=receipt)

"""Grok Build CLI-harness adapter: spec + parser over the shared base.

CLI contract (verified against the installed CLI): ``grok --cwd WORKSPACE
--prompt-file FILE --output-format json``. Discovery is binary plus
``--version`` only, so no models command exists. ``--resume`` continues an
exact conversation and is documented but never executed: the engine
fresh-runs every prompt. Automatic-approval flags are deliberately absent:
fleet workers run tool-less JSON prompts unattended.
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

GROK_SPEC = HarnessSpec(
    name="grok",
    binary="grok",
    prompt_delivery="file_flag",
    prompt_file_flag="--prompt-file",
    parser="json_object",
    call_workdir=True,
    discovery_argv=["models"],
    model_from_route=False,
)


class GrokProvider(CLIHarnessProvider):
    def __init__(self, runner=None):
        super().__init__(GROK_SPEC, runner=runner)

    def build_argv(
        self,
        *,
        model: str,
        prompt: str,
        prompt_file: str | None,
        workspace: Path | None = None,
        workdir: Path | None = None,
    ) -> list[str]:
        # No --model in the documented headless recipe: the CLI default serves.
        if prompt_file is None:
            raise RuntimeError("grok prompt staging failed")
        return [
            "--cwd", str(workspace or Path.cwd()),
            "--prompt-file", prompt_file,
            "--output-format", "json",
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

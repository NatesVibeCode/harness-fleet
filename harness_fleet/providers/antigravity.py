"""Antigravity CLI-harness adapter: spec + parser over the shared base.

CLI contract (verified against the installed CLI): ``agy --output-format
json --print TASK`` with the prompt as ONE argv element (this CLI does not
use ``--workspace`` or ``--prompt-file`` syntax). Discovery is binary plus
``--version`` only, so no models command exists. ``--conversation``
continues an exact conversation and is documented but never executed: the
engine fresh-runs every prompt. Permission-skip flags are deliberately
absent: fleet workers run tool-less JSON prompts unattended.
"""
from __future__ import annotations

from pathlib import Path

from ..models import ProviderReceipt
from .harness import (
    CLIHarnessProvider,
    HarnessSpec,
    parse_json_object_stdout,
    run_error_receipt,
)

ANTIGRAVITY_SPEC = HarnessSpec(
    name="antigravity",
    binary="agy",
    prompt_delivery="argv_last",
    parser="json_object",
    discovery_argv=["models"],
    model_from_route=False,
)


class AntigravityProvider(CLIHarnessProvider):
    def __init__(self, runner=None):
        super().__init__(ANTIGRAVITY_SPEC, runner=runner)

    def build_argv(
        self,
        *,
        model: str,
        prompt: str,
        prompt_file: str | None,
        workspace: Path | None = None,
        workdir: Path | None = None,
    ) -> list[str]:
        # No --model in the documented one-shot recipe: the CLI default serves.
        return ["--output-format", "json", "--print", prompt]

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

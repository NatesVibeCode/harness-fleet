"""Muse CLI-harness adapter: spec + parser over the shared base.

CLI contract (verified against the installed CLI): ``muse exec --json --workspace
W --worktree off --prompt-file FILE``. Discovery is binary plus
``--version`` only, so no models command exists. History continuation is
interactive-only, so fresh-only execution holds by design: the engine
fresh-runs every prompt.

``--json`` emits an NDJSON *session event stream*, not one JSON document: the
final assistant text arrives in the ``run_terminal`` record, while
``run_output_delta`` records carry the streamed chunks. Parsing stdout as a
single object therefore never found text, so the stream is read here and a
single-object payload is still accepted as a fallback.
"""
from __future__ import annotations

import json
from pathlib import Path

from .harness import (
    CLIHarnessProvider,
    HarnessSpec,
    ProviderReceipt,
    parse_json_object_stdout,
    run_error_receipt,
)

#: Terminal states that mean the run produced no usable answer.
FAILED_TERMINALS = frozenset({"failed", "error", "cancelled", "canceled", "aborted", "timeout"})

MUSE_SPEC = HarnessSpec(
    name="muse",
    binary="muse",
    prompt_delivery="file_flag",
    prompt_file_flag="--prompt-file",
    parser="muse_jsonl",
    call_workdir=True,
    discovery_argv=None,
    model_from_route=False,
)


def parse_muse_jsonl(
    stdout: str,
    stderr: str,
    code: int,
    *,
    receipt: ProviderReceipt,
) -> tuple[bool, str | None, ProviderReceipt]:
    """Read the Muse session-event stream; the terminal record is authoritative.

    Falls back to :func:`parse_json_object_stdout` when the stream carries no
    recognisable Muse records, so a single-object transcript still parses.
    """
    deltas: list[str] = []
    terminal_text: str | None = None
    terminal_state: str | None = None
    failure_reason: str | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        if kind == "run_output_delta":
            chunk = payload.get("text")
            if isinstance(chunk, str):
                deltas.append(chunk)
        elif kind == "run_terminal":
            state = payload.get("terminal")
            if isinstance(state, str):
                terminal_state = state
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                terminal_text = text
            reason = payload.get("reason")
            if reason:
                failure_reason = str(reason)[:500]

    if terminal_text is not None and (terminal_state or "").lower() not in FAILED_TERMINALS:
        receipt.status = "complete"
        return True, terminal_text, receipt

    if terminal_state is not None and terminal_state.lower() in FAILED_TERMINALS:
        receipt.error = failure_reason or f"muse run terminated as {terminal_state!r}"
        receipt.error_type = "transient_http" if code == 0 else "inference_error"
        return False, None, receipt

    streamed = "".join(deltas).strip()
    if streamed:
        receipt.status = "complete"
        return True, streamed, receipt

    if code != 0:
        return run_error_receipt(code, stdout, stderr, receipt=receipt)
    # No Muse-shaped records at all: accept a plain JSON-object transcript.
    return parse_json_object_stdout(stdout, receipt=receipt)


class MuseProvider(CLIHarnessProvider):
    def __init__(self, runner=None):
        super().__init__(MUSE_SPEC, runner=runner)

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
            raise RuntimeError("muse prompt staging failed")
        return [
            "exec",
            "--json",
            "--workspace", str(workspace or Path.cwd()),
            "--worktree", "off",
            "--prompt-file", prompt_file,
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
        return parse_muse_jsonl(stdout, stderr, code, receipt=receipt)

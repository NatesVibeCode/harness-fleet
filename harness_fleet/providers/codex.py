"""Codex CLI-harness adapter: spec + JSONL parser over the shared base.

CLI contract (verified against the installed CLI): ``codex exec -C WORKSPACE
--json -o OUTFILE -`` with the prompt on stdin from a prompt file; JSONL
events stream on stdout while the final message lands in OUTFILE. Event
shapes beyond the JSON envelope are not pinned by the CLI contract, so text is
extracted conservatively per line and a turn with no text fails closed.
Approval-bypass flags are deliberately absent: fleet workers run tool-less
JSON prompts unattended, and anything demanding approvals fails closed
instead (this deviates from the skill's bypass-by-default, which targets
agentic coding rather than batch inference). The skill mandates fresh-only
sessions, so no resume path exists by design.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import ProviderReceipt
from .harness import (
    CLIHarnessProvider,
    HarnessSpec,
    classify_failure,
    extract_conservative_text,
)

CODEX_SPEC = HarnessSpec(
    name="codex",
    binary="codex",
    prompt_delivery="stdin",
    parser="codex_jsonl",
    call_workdir=True,
    discovery_argv=None,
    model_from_route=False,
)


def parse_codex_jsonl(
    stdout: str, stderr: str, code: int, *, receipt: ProviderReceipt, workdir: Path | None = None
) -> tuple[bool, str | None, ProviderReceipt]:
    """Walk Codex ``--json`` events; fail closed when no text is found.

    The ``-o`` capture file is the documented final-message path, so when
    stdout carries no text but the run exited 0, its content is the fallback
    source -- never invented, never garbage from elsewhere.
    """
    texts: list[str] = []
    last_err: str | None = None
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
        item = event.get("item")
        if event.get("type") == "error" or event.get("error"):
            last_err = str(event.get("error", event))[:500]
            continue
        if isinstance(item, dict) and item.get("type") == "error":
            last_err = str(item.get("error", item))[:500]
            continue
        text = extract_conservative_text(event)
        if text:
            texts.append(text)
        usage = event.get("usage", event.get("tokens"))
        if isinstance(usage, dict):
            receipt.usage = usage
    if code != 0 or not texts:
        if code == 0 and workdir is not None:
            final = workdir / "final.md"
            try:
                fallback = final.read_text(encoding="utf-8").strip()
            except OSError:
                fallback = ""
            if fallback:
                receipt.status = "complete"
                return True, fallback, receipt
        err_msg = last_err or (stderr or stdout)[-500:] or f"Exit code {code}"
        receipt.error = err_msg
        receipt.error_type = classify_failure(err_msg)  # type: ignore[assignment]
        if receipt.error_type == "rate_limit":
            receipt.retry_after = 10.0
        return False, None, receipt
    receipt.status = "complete"
    return True, "\n".join(texts), receipt


class CodexProvider(CLIHarnessProvider):
    def __init__(self, runner=None):
        super().__init__(CODEX_SPEC, runner=runner)

    def build_argv(
        self,
        *,
        model: str,
        prompt: str,
        prompt_file: str | None,
        workspace: Path | None = None,
        workdir: Path | None = None,
    ) -> list[str]:
        if workdir is None:
            raise RuntimeError("codex call workdir staging failed")
        out_file = workdir / "final.md"
        return [
            "exec",
            "-C", str(workspace or Path.cwd()),
            "--json",
            "-o", str(out_file),
            "-",
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
        return parse_codex_jsonl(stdout, stderr, code, receipt=receipt, workdir=workdir)

"""Typed universal CLI-harness provider base.

Every CLI harness (opencode, claude, codex, cursor, grok, muse,
antigravity) is an equal entry behind one interface: a closed
``HarnessSpec`` plus a thin ``CLIHarnessProvider`` that detects the binary,
stages the prompt, builds ``list[str]`` argv, runs via an injectable runner,
parses per spec, and builds the standard receipt.

Rules, with no exceptions:
- argv is always a ``list[str]`` built from typed spec fields. Prompt text
  is never interpolated into a command line: it travels as a file, as
  stdin bytes, or as ONE trailing argv element, per ``prompt_delivery``.
- ``shell=True`` appears nowhere; runners use ``shell=False`` list argv.
- Unknown providers and malformed routes fail closed with an error receipt.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import model_validator

from ..models import (
    ClosedModel,
    MalformedRouteError,
    ProviderReceipt,
    RouteId,
    RoutePolicy,
)
from .base import BaseProvider

PromptDelivery = Literal["argv_last", "stdin", "file_flag"]
ParserKind = Literal["opencode_jsonl", "codex_jsonl", "muse_jsonl", "json_object"]
TaskConfigStrategy = Literal["opencode_deny_all", "cursor_sandbox_enabled", "none"]


class HarnessSpec(ClosedModel):
    """Closed per-harness contract: the flags and shapes an adapter is written against."""

    name: str
    binary: str
    prompt_delivery: PromptDelivery
    prompt_file_flag: str | None = None
    parser: ParserKind = "json_object"
    task_config_strategy: TaskConfigStrategy = "none"
    discovery_argv: list[str] | None = None
    model_from_route: bool = True
    call_workdir: bool = False

    @model_validator(mode="after")
    def _check_file_flag(self) -> HarnessSpec:
        if self.prompt_delivery == "file_flag" and not self.prompt_file_flag:
            raise ValueError("prompt_delivery='file_flag' requires prompt_file_flag")
        return self


class HarnessRunner(Protocol):
    """Injectable subprocess seam. List argv only; never a shell string."""

    def run(
        self, *, argv: list[str], stdin_text: str | None, timeout_sec: int
    ) -> tuple[int, str, str]: ...


class LocalHarnessCLI:
    """Default runner: ``subprocess.run`` with ``shell=False``."""

    def run(
        self, *, argv: list[str], stdin_text: str | None, timeout_sec: int
    ) -> tuple[int, str, str]:
        completed = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
        return completed.returncode, completed.stdout, completed.stderr


def extract_conservative_text(payload: Any) -> str | None:
    """Best-effort text from a harness JSON object, or None when absent.

    Only documented keys plus narrow fallbacks are read
    (``text``/``result``/``output``/``message``/``content``/``response``);
    anything else fails closed to an error receipt, never garbage text.
    ``response`` is a plain string for some harnesses (antigravity) and a
    container for others, so it is checked as both.
    """
    if isinstance(payload, str):
        return payload if payload.strip() else None
    if not isinstance(payload, dict):
        return None
    for key in ("text", "result", "output", "message", "content", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("data", "response", "payload", "item"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_conservative_text(nested)
            if found:
                return found
    return None


def parse_json_object_stdout(
    stdout: str, *, receipt: ProviderReceipt
) -> tuple[bool, str | None, ProviderReceipt]:
    """Parse a single-JSON-object harness transcript (conservative).

    Returns ``(ok, text, receipt)``; missing text fails closed with an
    ``inference_error`` receipt. ``cost``/``usage`` use documented keys plus
    narrow fallbacks and stay ``unknown`` when absent.
    """
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, AttributeError):
        receipt.error = (stdout or "")[-500:] or "empty harness output"
        receipt.error_type = "inference_error"
        return False, None, receipt
    text = extract_conservative_text(payload)
    if isinstance(payload, dict) and (
        payload.get("error") or payload.get("type") == "error" or payload.get("is_error") is True
    ):
        detail = payload.get("error") or payload.get("message")
        if detail is None:
            data = payload.get("data")
            if isinstance(data, dict):
                detail = data.get("message") or data.get("error")
        if detail is None:
            # Never drop the transcript: an unlabelled error object is still the
            # only diagnostic the operator gets.
            detail = json.dumps(payload)[:500]
        receipt.error = str(detail)[:500]
        receipt.error_type = classify_failure(receipt.error)  # type: ignore[assignment]
        return False, None, receipt
    if not text:
        receipt.error = "harness returned no readable text"
        receipt.error_type = "inference_error"
        return False, None, receipt
    if isinstance(payload, dict):
        usage = payload.get("usage", payload.get("tokens"))
        if isinstance(usage, dict):
            receipt.usage = usage
        cost = payload.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            receipt.cost = float(cost)
            receipt.cost_status = "reported_zero" if float(cost) == 0 else "billed"
    receipt.status = "complete"
    return True, text, receipt


def classify_failure(err_msg: str) -> str:
    """Map a harness error transcript onto a receipt ``error_type``.

    The engine parks ``rate_limit``/``transient_http``/``auth_error`` routes in
    cooldown without burning attempt budget. Leaving an unauthenticated CLI
    classified as ``inference_error`` makes a run spend its whole budget on a
    route that can never answer, so the sign-in case is called out explicitly.
    """
    lowered = err_msg.lower()
    if "429" in lowered or "rate limit" in lowered:
        return "rate_limit"
    if any(marker in lowered for marker in (
        "authentication required",
        "not signed in",
        "not logged in",
        "please login",
        "please log in",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "api key not",
        "no credentials",
        "credentials not found",
        "agent login",
        "run 'claude login'",
    )):
        return "auth_error"
    if any(marker in lowered for marker in (
        "unexpected server error",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "502",
        "503",
        "504",
    )):
        return "transient_http"
    return "inference_error"


def run_error_receipt(
    code: int, stdout: str, stderr: str, *, receipt: ProviderReceipt
) -> tuple[bool, None, ProviderReceipt]:
    """Shared non-zero-exit / no-output failure receipt (rate-limit aware)."""
    err_msg = (stderr or stdout)[-500:] or f"Exit code {code}"
    receipt.error = err_msg
    receipt.error_type = classify_failure(err_msg)  # type: ignore[assignment]
    if receipt.error_type == "rate_limit":
        receipt.retry_after = 10.0
    return False, None, receipt


class CLIHarnessProvider(BaseProvider):
    """Shared detect -> stage -> build-argv -> run -> parse -> receipt flow."""

    def __init__(self, spec: HarnessSpec, runner: HarnessRunner | None = None):
        self.spec = spec
        self.runner = runner or LocalHarnessCLI()

    def _new_receipt(self, route_id: str, session_id: str | None) -> ProviderReceipt:
        # Total constructor: foreign callers may pass non-strings, and the
        # receipt must still exist for the fail-closed handlers below.
        return ProviderReceipt(
            id=uuid.uuid4().hex,
            session_id=session_id,
            provider=self.spec.name,
            requested_route=route_id if isinstance(route_id, str) else repr(route_id),
            status="failed",
        )

    def _preflight(self) -> str | None:
        """Return an error string when the harness binary is unavailable."""
        if shutil.which(self.spec.binary) is None:
            return f"{self.spec.binary} not found in PATH"
        return None

    def derive_model(self, route_id: str) -> str:
        """Model identity from a validated route id (base rule).

        Adapters with ``model_from_route=False`` ignore the model, so bare
        route ids are accepted for them without validation.
        """
        if not self.spec.model_from_route:
            _, _, remainder = route_id.partition("/")
            if not remainder:
                _, _, remainder = route_id.partition(":")
            return remainder
        parsed = RouteId.parse(route_id)
        if parsed.model is None:
            raise MalformedRouteError(f"malformed harness route id: {route_id!r}")
        return parsed.model

    def build_argv(
        self,
        *,
        model: str,
        prompt: str,
        prompt_file: str | None,
        workspace: Path | None = None,
        workdir: Path | None = None,
    ) -> list[str]:
        """Adapter-owned argv template over typed spec fields. No formatting."""
        raise NotImplementedError

    def _invoke(
        self,
        *,
        argv: list[str],
        stdin_text: str | None,
        task_config: dict[str, Any] | None,
        timeout_sec: int,
    ) -> tuple[int, str, str]:
        return self.runner.run(argv=argv, stdin_text=stdin_text, timeout_sec=timeout_sec)

    def _task_config_for(self, model: str) -> dict[str, Any] | None:
        """Task-local harness config (opencode deny-all); None for most."""
        return None

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
        """Adapter-owned parser. Must never return garbage text."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """A harness route is usable iff its binary is on PATH (all seven equal)."""
        return shutil.which(self.spec.binary) is not None

    def run_prompt(
        self,
        route_id: str,
        prompt: str,
        system_prompt: str | None = None,
        timeout_sec: int = 120,
        session_id: str | None = None,
        policy: RoutePolicy | None = None,
    ) -> tuple[bool, str | None, ProviderReceipt]:
        started = time.time()
        receipt = self._new_receipt(route_id, session_id)
        full_prompt = (system_prompt + "\n\n" if system_prompt else "") + prompt
        prompt_file: str | None = None
        workdir: Path | None = None
        try:
            # Fail fast on blank/structural garbage, including for adapters
            # that ignore the model (their derive_model would not raise).
            RouteId.parse(route_id)
            problem = self._preflight()
            if problem is not None:
                receipt.error = problem
                receipt.error_type = "inference_error"
                receipt.duration_seconds = time.time() - started
                return False, None, receipt
            model = self.derive_model(route_id)
            stdin_text: str | None = None
            if self.spec.prompt_delivery == "stdin":
                stdin_text = full_prompt
            # Adapters that need call-scoped files (prompt files, codex -o)
            # share one workdir, removed in the finally below.
            if self.spec.prompt_delivery == "file_flag" or self.spec.call_workdir:
                workdir = Path(tempfile.mkdtemp(prefix=f"harness-fleet-{self.spec.name}-"))
            if self.spec.prompt_delivery == "file_flag":
                prompt_path = workdir / "prompt.txt" if workdir is not None else None
                if prompt_path is None:
                    raise RuntimeError(f"{self.spec.name} prompt file staging failed")
                prompt_path.write_text(full_prompt, encoding="utf-8")
                prompt_file = str(prompt_path)
            argv = [self.spec.binary, *self.build_argv(
                model=model,
                prompt=full_prompt,
                prompt_file=prompt_file,
                workspace=None,
                workdir=workdir,
            )]
            if not all(isinstance(part, str) for part in argv):
                raise TypeError("harness argv must be list[str]")
            task_config = self._task_config_for(model)
            # The lockdown strategy is load-bearing documentation: a spec
            # that promises tool lockdown fails closed when its argv/config
            # stops carrying it.
            if self.spec.task_config_strategy == "cursor_sandbox_enabled" and "--sandbox" not in argv:
                raise RuntimeError(f"{self.spec.name} argv lost its --sandbox lockdown")
            if self.spec.task_config_strategy == "opencode_deny_all" and (
                not isinstance(task_config, dict) or task_config.get("permission") != {"*": "deny"}
            ):
                raise RuntimeError(f"{self.spec.name} task config lost its deny-all lockdown")
            code, stdout, stderr = self._invoke(
                argv=argv,
                stdin_text=stdin_text,
                task_config=task_config,
                timeout_sec=timeout_sec,
            )
            ok, text, receipt = self.parse_output(
                code=code, stdout=stdout, stderr=stderr, receipt=receipt, started=started,
                workdir=workdir,
            )
            receipt.duration_seconds = time.time() - started
            return ok, text, receipt
        except MalformedRouteError as exc:
            receipt.error = str(exc)
            receipt.error_type = "inference_error"
            receipt.duration_seconds = time.time() - started
            return False, None, receipt
        except subprocess.TimeoutExpired as exc:
            receipt.error = str(exc)
            receipt.error_type = "timeout"
            receipt.duration_seconds = time.time() - started
            return False, None, receipt
        except Exception as exc:
            receipt.error = str(exc)
            receipt.error_type = "inference_error"
            receipt.duration_seconds = time.time() - started
            return False, None, receipt
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

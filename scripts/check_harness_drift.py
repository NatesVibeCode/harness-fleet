#!/usr/bin/env python3
"""Check the shared harness-fleet contract across local sibling repositories.

This is intentionally check-only. It never copies or overwrites source files.
career-fleet may extend its first migration, shared models, and operations
documentation with profile metadata, so those career-specific overlays are
normalized or intentionally excluded from byte-for-byte comparison.
Package-specific aliases, bundled skills, and task presets are also
variant-specific and are intentionally outside the shared-file allowlist. The
profile persistence path is shared, so its schema, store, engine, CLI, MCP,
and setup files are checked explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

EXACT_FILES = (
    "scripts/check_harness_drift.py",
    "tests/test_route_policy_contract.py",
    "tests/test_harness_live.py",
    "tests/test_live_probe_classification.py",
    "harness_fleet/providers/base.py",
    "harness_fleet/providers/harness.py",
    "harness_fleet/providers/opencode.py",
    "harness_fleet/providers/claude.py",
    "harness_fleet/providers/codex.py",
    "harness_fleet/providers/cursor.py",
    "harness_fleet/providers/grok.py",
    "harness_fleet/providers/muse.py",
    "harness_fleet/providers/antigravity.py",
    "harness_fleet/providers/registry.py",
    "harness_fleet/providers/demo.py",
    "harness_fleet/providers/openai_compatible.py",
    "harness_fleet/providers/openrouter.py",
    "harness_fleet/candidates.py",
    "harness_fleet/calibrate.py",
    "harness_fleet/catalog.py",
    "harness_fleet/profile.py",
    "harness_fleet/store.py",
    "harness_fleet/dag.py",
    "harness_fleet/engine.py",
    "harness_fleet/models.py",
    # cli.py is intentionally absent: it wires each variant's discover.py,
    # whose surface is variant-specific (account-fleet exposes stack/evidence
    # gates and a source_quality report that career-fleet's fork does not).
    # The shared modules cli.py builds on are still checked individually.
    "harness_fleet/mcp_server.py",
    "harness_fleet/setup.py",
    "harness_fleet/studio.py",
    "harness_fleet/__init__.py",
    "harness_fleet/migrations/003_profiles.sql",
    "harness_fleet/migrations/004_studio_settings.sql",
    "harness_fleet/export.py",
    "harness_fleet/grounding.py",
    "harness_fleet/input_data.py",
    "harness_fleet/packer.py",
    "harness_fleet/sessions.py",
    "harness_fleet/slicer.py",
    # The product surface itself: one CLI, one connector set, one board, one
    # task vocabulary. Only branding.py differs per distribution, so a fix or a
    # connector lands in every product at once instead of being ported.
    "harness_fleet/cli.py",
    "harness_fleet/discover.py",
    "harness_fleet/board.py",
    "harness_fleet/bundler.py",
    "harness_fleet/task.py",
    "harness_fleet/resources/board/index.html",
    # The source taxonomy and the evidence rules are the fleet-wide semantic
    # contract: every variant must judge a source and a dossier the same way.
    "harness_fleet/sources.py",
    # The central contracts every altitude reads.
    "harness_fleet/contracts.py",
    "harness_fleet/registry.py",
    "harness_fleet/channels.py",
    "harness_fleet/lanes.py",
    "harness_fleet/lane_report.py",
    "harness_fleet/evidence.py",
    "harness_fleet/ui.py",
    "harness_fleet/migrations/002_intelligence_and_policy.sql",
    "harness_fleet/migrations/001_control_plane.sql",
    "harness_fleet/data/routes.seed.json",
    "harness_fleet/resources/harness_skill/references/task-contracts.md",
    "harness_fleet/resources/harness_skill/references/operations.md",
    "tests/test_openrouter_policy.py",
    "harness_fleet/resources/studio/index.html",
    "skills/harness-fleet/references/operations.md",
    ".agents/skills/harness-fleet/references/operations.md",
)





def _default_repos(cwd: Path) -> list[Path]:
    """The repositories to check.

    This used to discover sibling product checkouts and compare them byte for
    byte. Those products now live here (their repositories are archived), so the
    default is this repository alone; pass ``--repo`` repeatedly to compare
    several checkouts explicitly.
    """
    resolved = cwd.resolve()
    return [resolved] if (resolved / ".git").exists() else []


def _normalized_digest(path: Path, relative: str) -> str:
    text = path.read_text(encoding="utf-8")
    if relative == "harness_fleet/__init__.py":
        # Each fleet releases independently, so the version literal legitimately
        # differs. Everything else in the SDK surface must still match.
        text = re.sub(r'(?m)^__version__ = "[^"]*"$', '__version__ = "shared"', text)
    if relative.endswith(("references/operations.md", "references/task-contracts.md", "SKILL.md")):
        # The bundled engine skill is one document in every distribution, but the
        # binary a user runs is the one they installed: account-fleet's copy says
        # account-fleet. Normalize only the invocation, never the distribution or
        # skill name, so `pip install harness-fleet` and
        # `.agents/skills/harness-fleet` still have to match byte for byte.
        text = re.sub(r'(?<![\w./-])(?:harness|account|career)-fleet(?= [a-z])', "<fleet>", text)
        text = re.sub(r'"(?:harness|account|career)-fleet"', '"<fleet>"', text)
    if relative.endswith("references/operations.md"):
        text = re.sub(
            r'Schema version is `?"(?:2|3|4|5)"`?(?:\. Account runs can also retain the exact immutable Ideal Company Profile revision used for the campaign\.)?\.?',
            'Schema version is `"shared"`.',
            text,
        )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_exact_files(repos: list[Path]) -> bool:
    ok = True
    baseline = repos[0]
    for relative in EXACT_FILES:
        expected = baseline / relative
        if not expected.is_file():
            print(f"MISSING {baseline}: {relative}")
            ok = False
            continue
        expected_digest = _normalized_digest(expected, relative)
        for repo in repos[1:]:
            candidate = repo / relative
            if not candidate.is_file():
                print(f"MISSING {repo}: {relative}")
                ok = False
            elif _normalized_digest(candidate, relative) != expected_digest:
                print(f"DRIFT  {relative}: {baseline} != {repo}")
                ok = False
    return ok


def _run_contract(repo: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_route_policy_contract.py"],
        cwd=repo,
        check=False,
    )
    if result.returncode:
        print(f"FAIL   route-policy contract: {repo}")
        return False
    print(f"PASS   route-policy contract: {repo}")
    return True


HANDOFF_PROVIDERS = {
    "antigravity": "AntigravityProvider",
    "claude": "ClaudeProvider",
    "codex": "CodexProvider",
    "cursor": "CursorProvider",
    "grok": "GrokProvider",
    "muse": "MuseProvider",
    "opencode": "OpenCodeProvider",
}
HANDOFF_SPEC_FIELDS = (
    "binary", "prompt_delivery", "prompt_file_flag", "parser",
    "model_from_route", "discovery_argv", "call_workdir", "task_config_strategy",
)
HANDOFF_PLACEHOLDERS = ("<prompt>", "<model>", "<workspace>", "<prompt-file>", "<workdir>")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _check_handoff_contract(path: Path) -> bool:
    try:
        contract = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(contract, dict):
            raise ValueError("contract must be an object")
        if type(contract.get("version")) is not int or contract["version"] != 1:
            raise ValueError("contract version must be 1")
        placeholders = contract.get("placeholders")
        if (
            not isinstance(placeholders, list)
            or not all(isinstance(item, str) for item in placeholders)
            or len(placeholders) != len(HANDOFF_PLACEHOLDERS)
            or set(placeholders) != set(HANDOFF_PLACEHOLDERS)
        ):
            raise ValueError("invalid contract placeholders")
        harnesses = contract.get("harnesses")
        if not isinstance(harnesses, dict) or set(harnesses) != set(HANDOFF_PROVIDERS):
            raise ValueError("contract must contain exactly the seven supported harnesses")
        for name, class_name in HANDOFF_PROVIDERS.items():
            entry = harnesses[name]
            if not isinstance(entry, dict):
                raise ValueError(f"{name}: contract must be an object")
            module = importlib.import_module(f"harness_fleet.providers.{name}")
            provider = getattr(module, class_name)()
            if provider.spec.name != name:
                raise ValueError(f"{name}: spec name differs")
            for field in HANDOFF_SPEC_FIELDS:
                actual = getattr(provider.spec, field)
                if field not in entry:
                    raise ValueError(f"{name}.{field}: missing field")
                expected = entry[field]
                if type(expected) is not type(actual) or expected != actual:
                    raise ValueError(f"{name}.{field}: contract differs from HarnessSpec")
            template = entry.get("oneshot_argv")
            if not isinstance(template, list) or not template or not all(
                isinstance(part, str) for part in template
            ):
                raise ValueError(f"{name}.oneshot_argv: expected nonempty list[str]")
            for values in (
                HANDOFF_PLACEHOLDERS,
                ("contract prompt 'quoted'\nsecond line", "vendor/contract-model",
                 "/contract workspace", "/contract work/prompt file.txt", "/contract work"),
            ):
                replacements = dict(zip(HANDOFF_PLACEHOLDERS, values, strict=True))
                expected_argv = [
                    re.sub(r"<[^<>]+>", lambda match, replacements=replacements: replacements[match[0]], part)
                    for part in template
                ]
                actual_argv = provider.build_argv(
                    model=values[1], prompt=values[0], prompt_file=values[3],
                    workspace=Path(values[2]), workdir=Path(values[4]),
                )
                if type(actual_argv) is not list or actual_argv != expected_argv:
                    raise ValueError(f"{name}.oneshot_argv: build_argv differs from contract")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, ImportError, RuntimeError) as exc:
        print(f"FAIL   handoff contract: {path}: {exc}")
        return False
    print(f"PASS   handoff contract: {path}")
    return True


def _run_handoff_contract(repo: Path, path: Path) -> bool:
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
            "checker = runpy.run_path(sys.argv[2]); "
            "raise SystemExit(0 if checker['_check_handoff_contract']("
            "checker['Path'](sys.argv[3])) else 1)",
            str(repo), str(Path(__file__).resolve()), str(path),
        ],
        cwd=repo,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        dest="repos",
        action="append",
        type=Path,
        help="Repository to check; repeat for a multi-repo comparison",
    )
    parser.add_argument(
        "--self",
        action="store_true",
        help="Check only the current repository (the CI mode)",
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="Compare exact files without running the contract test",
    )
    parser.add_argument(
        "--handoff-contract",
        type=Path,
        metavar="PATH",
        help="Check HarnessSpec and pure argv against this explicit handoff JSON contract",
    )
    args = parser.parse_args()

    repos = [path.expanduser().resolve() for path in (args.repos or [])]
    if not repos:
        repos = [Path.cwd().resolve()] if args.self else _default_repos(Path.cwd())
    if not repos:
        parser.error("no Git repositories found; pass --repo or run inside a checkout")

    ok = True
    if args.handoff_contract is not None:
        path = args.handoff_contract.expanduser().resolve()
        for repo in repos:
            ok = _run_handoff_contract(repo, path) and ok
    if len(repos) > 1:
        ok = _check_exact_files(repos) and ok
    if not args.no_tests:
        for repo in repos:
            ok = _run_contract(repo) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

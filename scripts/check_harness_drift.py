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
    candidates = (
        cwd,
        cwd.parent / "harness-fleet",
        cwd.parent / "account-fleet",
        cwd.parent / "Career" / "career-fleet",
        # The fleets may live in one tree with the products nested under the
        # engine (…/harness-fleet/career-fleet), so look inside as well as beside.
        cwd / "career-fleet",
        cwd / "account-fleet",
        cwd.parent.parent / "harness-fleet",
        cwd.parent.parent / "account-fleet",
        cwd.parent.parent / "Career" / "career-fleet",
    )
    repos: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and (resolved / ".git").exists() and resolved not in repos:
            repos.append(resolved)
    return repos


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
    args = parser.parse_args()

    repos = [path.expanduser().resolve() for path in (args.repos or [])]
    if not repos:
        repos = [Path.cwd().resolve()] if args.self else _default_repos(Path.cwd())
    if not repos:
        parser.error("no Git repositories found; pass --repo or run inside a checkout")

    ok = True
    if len(repos) > 1:
        ok = _check_exact_files(repos) and ok
    if not args.no_tests:
        for repo in repos:
            ok = _run_contract(repo) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

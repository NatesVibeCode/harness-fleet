"""Every command in the docs and skills must be one this install can run.

A release reads as broken when a README tells you to run a command that does not
exist, or a binary this distribution does not ship. Both are checked here: the
documented commands are parsed by the real CLI parser, and command lines may
only invoke this distribution's own binary.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OWN_BINARY = "harness-fleet"


def _shipped_binaries() -> set[str]:
    """Every command this distribution installs, from its own packaging."""
    import tomllib

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data.get("project", {}).get("scripts", {}))


SHIPPED_BINARIES = _shipped_binaries()
#: True only where this CLI does not provide the engine commands
#: (career-fleet ships the engine skill but not the engine CLI).
ENGINE_CLI_IS_SEPARATE = False
FLEET_BINARIES = ("harness-fleet", "account-fleet", "career-fleet", "free-fleet")
FENCE_RE = re.compile(r"```([a-zA-Z]*)\n(.*?)```", re.S)
DOC_GLOBS = (
    "README.md",
    "FREE-ACCESS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "skills/**/*.md",
    "harness_fleet/resources/**/*.md",
    "career_fleet/resources/**/*.md",
)
#: A command from another product is allowed only where the doc is that other
#: product's own skill, or where it is explicitly the old CLI in a migration
#: note. Everything else must use the binary this distribution installs.
FOREIGN_ALLOWED = {
    # harness-fleet ships the account and partner skills, whose commands belong
    # to those distributions.
    "skills/account-fleet/": {"account-fleet"},
    "skills/partner-fleet/": {"harness-fleet"},
    "harness_fleet/resources/account_skill/": {"account-fleet"},
    "harness_fleet/resources/partner_skill/": {"harness-fleet"},
    # The engine skill shipped by a product whose CLI does not provide those
    # commands says so in its own first paragraph.
    "skills/harness-fleet/": {"harness-fleet"} if ENGINE_CLI_IS_SEPARATE else set(),
    ".agents/skills/harness-fleet/": {"harness-fleet"} if ENGINE_CLI_IS_SEPARATE else set(),
    "harness_fleet/resources/harness_skill/": {"harness-fleet"} if ENGINE_CLI_IS_SEPARATE else set(),
}


def _parser(binary: str = "") -> argparse.ArgumentParser:
    """The parser for a surface this repo ships."""
    if binary == "career-fleet" and (REPO / "career_fleet").is_dir():
        from career_fleet import cli as career_cli

        captured: dict[str, argparse.ArgumentParser] = {}
        real = argparse.ArgumentParser.parse_args

        def spy(self, *args, **kwargs):
            captured["parser"] = self
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = spy  # type: ignore[method-assign]
        try:
            career_cli.main()
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = real  # type: ignore[method-assign]
        return captured["parser"]

    if OWN_BINARY == "career-fleet":
        from career_fleet import cli as career_cli

        captured: dict[str, argparse.ArgumentParser] = {}
        real = argparse.ArgumentParser.parse_args

        def spy(self, *args, **kwargs):
            captured["parser"] = self
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = spy  # type: ignore[method-assign]
        try:
            career_cli.main()
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = real  # type: ignore[method-assign]
        return captured["parser"]

    from harness_fleet.cli import build_parser

    return build_parser()


def _documented_commands() -> list[tuple[str, int, list[str]]]:
    found = []
    for pattern in DOC_GLOBS:
        for path in sorted(REPO.glob(pattern)):
            if not path.is_file():
                continue
            relative = path.relative_to(REPO).as_posix()
            text = path.read_text(encoding="utf-8")
            for block in FENCE_RE.finditer(text):
                if (block.group(1) or "").lower() not in {"bash", "sh", "console", "shell", ""}:
                    continue
                base = text[: block.start(2)].count("\n") + 1
                pending = ""
                for offset, raw in enumerate(block.group(2).splitlines()):
                    line = raw.strip()
                    if pending:
                        line, pending = f"{pending} {line}", ""
                    if not line or line.startswith("#"):
                        continue
                    if line.endswith("\\"):
                        pending = line[:-1].strip()
                        continue
                    line = re.sub(r"\s+#.*$", "", line)
                    line = re.sub(r"^[A-Z_]+=[^\s]+\s+", "", line)
                    try:
                        argv = shlex.split(line)
                    except ValueError:
                        continue
                    if not argv or argv[0] not in FLEET_BINARIES:
                        continue
                    found.append((relative, base + offset, argv))
    return found


DOCUMENTED = _documented_commands()


def test_the_docs_actually_contain_commands():
    """A silent glob failure must not turn this file into a green no-op."""
    assert len(DOCUMENTED) > 20, f"only found {len(DOCUMENTED)} documented commands"


@pytest.mark.parametrize("relative,line,argv", DOCUMENTED, ids=lambda v: str(v)[:60])
def test_every_documented_command_parses(relative, line, argv):
    if argv[0] not in SHIPPED_BINARIES:
        pytest.skip(f"{relative} documents the {argv[0]} distribution")
    parser = _parser(argv[0])
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(argv[1:])
        except SystemExit as exc:
            if exc.code in (0, None):
                return  # --help / --version exit cleanly
            pytest.fail(f"{relative}:{line} -> {argv[0]} {' '.join(argv[1:])}\n{stderr.getvalue().strip()}")


def _allowed_foreign(relative: str) -> set[str]:
    allowed: set[str] = set()
    for prefix, names in FOREIGN_ALLOWED.items():
        if relative.startswith(prefix):
            allowed |= names
    return allowed


@pytest.mark.parametrize("relative,line,argv", DOCUMENTED, ids=lambda v: str(v)[:60])
def test_no_documented_command_uses_another_products_binary(relative, line, argv):
    if argv[0] in SHIPPED_BINARIES or argv[0] == "free-fleet":
        return  # free-fleet appears only as the pre-rename CLI in migration notes
    if argv[0] in _allowed_foreign(relative):
        return
    pytest.fail(
        f"{relative}:{line} tells the user to run {argv[0]}, "
        f"but this distribution installs {OWN_BINARY}"
    )

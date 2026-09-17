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
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10, which this project still supports
        pytest.importorskip("tomli", reason="needs a TOML parser: tomllib (3.11+) or tomli")
        import tomli as tomllib  # type: ignore[no-redef]

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
    # The product docs carry commands too, and they had none of this: a stale
    # command in docs/ parsed nowhere and nobody noticed.
    "docs/**/*.md",
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


def test_the_funnels_doc_is_the_renderer_s_output(tmp_path):
    """The doc claims it cannot drift from a run. This is what makes that true.

    Regenerating has to be one command and the committed file has to be the
    result of it, or the picture a person reads and the ladder a run climbs
    disagree the first time somebody edits a lane.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    committed = (repo / "docs" / "funnels.md").read_text(encoding="utf-8")
    fresh = tmp_path / "funnels.md"
    subprocess.run(
        [sys.executable, str(repo / "tools" / "render_funnels.py"), str(fresh)],
        cwd=repo, check=True, capture_output=True,
    )
    assert fresh.read_text(encoding="utf-8") == committed, (
        "docs/funnels.md is stale: run tools/render_funnels.py and commit the result"
    )


#: Backticked names in the docs that read as storage: the ones this work
#: introduced, and the ones that have already gone stale twice —
#: docs/data-models.md and docs/scoring.md both described a view that had been
#: deleted while the tool moved on.
TABLE_TOKEN_RE = re.compile(r"`((?:rung_|entity_|score_)[a-z_]+)`")

#: Modules whose names a doc may legitimately be using: `entity_evidence` is a
#: function, not a table, and a guard that cannot tell the difference would have
#: to be narrowed until it stopped catching the thing it exists for.
_SYMBOL_MODULES = (
    "board", "bundler", "contracts", "dag", "discover", "engine", "enrich",
    "evidence", "export", "gates", "input_data", "ledger", "models", "profile",
    "rungs", "sources", "store", "task",
)


def test_every_documented_store_table_exists(tmp_path):
    """The docs say where the data lives; the store says whether that is true.

    A reader who goes looking for a table the docs named and finds nothing has
    been told the wrong thing silently — the same failure as a README row that
    disagrees with a lane file, and just as invisible.
    """
    import importlib

    from harness_fleet.ledger import Ledger
    from harness_fleet.rungs import RungTables
    from harness_fleet.store import HarnessStore

    store = HarnessStore(tmp_path / "t.db")
    RungTables(store)
    Ledger(store)
    with store.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    named: set[str] = set()
    for pattern in DOC_GLOBS:
        for path in sorted(REPO.glob(pattern)):
            if path.is_file():
                named |= set(TABLE_TOKEN_RE.findall(path.read_text(encoding="utf-8")))

    known: set[str] = set()
    for module_name in _SYMBOL_MODULES:
        module = importlib.import_module(f"harness_fleet.{module_name}")
        known |= {name for name in dir(module) if not name.startswith("__")}

    assert named, "the scan found no table names at all, which proves nothing"
    # Neither a table nor anything the code defines: a reader following that
    # name finds nothing, wherever they look.
    missing = sorted(name for name in named if name not in tables and name not in known)
    assert not missing, (
        f"the docs name {missing}, which is neither a table in the store nor "
        "anything the code defines"
    )

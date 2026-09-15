"""Fresh-system setup for skill discovery and local SQLite state."""
from __future__ import annotations

import filecmp
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

from .catalog import RouteCatalog
from .models import SetupAction, SetupReport, StdioServerConfig
from .store import HarnessStore

ScopeName = Literal["user", "project"]


def bundled_skill_path() -> Path:
    path = Path(__file__).resolve().parent / "resources" / "harness_skill"
    if not (path / "SKILL.md").is_file():
        raise RuntimeError("installed package is missing the bundled harness-fleet skill")
    return path


def skill_destination(
    scope: ScopeName,
    home: Path,
    workspace: Path,
    skill_root: str | Path | None = None,
) -> Path:
    if skill_root is None:
        root = (home if scope == "user" else workspace) / ".agents" / "skills"
    else:
        candidate = Path(skill_root).expanduser()
        root = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
    return root / "harness-fleet"


def _relative_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def _same_skill(source: Path, destination: Path) -> bool:
    source_files = _relative_files(source)
    destination_files = _relative_files(destination) if destination.is_dir() else set()
    return source_files == destination_files and all(
        filecmp.cmp(source / relative, destination / relative, shallow=False) for relative in source_files
    )


def installed_skill_matches(destination: Path) -> bool:
    return destination.is_dir() and _same_skill(bundled_skill_path(), destination)


def _install_skill(source: Path, destination: Path, *, dry_run: bool, force: bool) -> SetupAction:
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError(f"skill destination is not a directory: {destination}")
    if destination.is_dir() and _same_skill(source, destination):
        return SetupAction(kind="skill", status="unchanged", path=str(destination))
    if destination.exists() and not force:
        raise FileExistsError(
            f"a different skill already exists at {destination}; rerun with --force to update managed files"
        )
    status: Literal["planned", "created", "updated", "unchanged", "skipped"] = ("planned" if dry_run else ("updated" if destination.exists() else "created"))
    if not dry_run:
        if destination.exists():
            # A forced update owns the managed skill directory. Replacing it
            # removes files from an older package that no longer exist.
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    return SetupAction(kind="skill", status=status, path=str(destination))


def _cli_search_names() -> list[str]:
    """Product CLIs to search, preferred entry first.

    Each distribution ships its own script but this module is byte-identical
    across repos, so every product name is searched. The invoking product
    goes first; pyproject [project.scripts] stays per-repo.
    """
    names = ["harness-fleet", "account-fleet", "career-fleet", "career-lanes"]
    invoked = Path(sys.argv[0]).stem.lower()
    if invoked in names:
        names.insert(0, names.pop(names.index(invoked)))
    return names


def installed_cli_path() -> str:
    for directory in (Path(sys.executable).absolute().parent, Path(sys.prefix) / "Scripts"):
        for name in _cli_search_names():
            for suffix in (".exe", "") if sys.platform == "win32" else ("",):
                sibling = directory / (name + suffix)
                if sibling.is_file():
                    return str(sibling)
    for name in _cli_search_names():
        discovered = shutil.which(name)
        if discovered:
            return str(Path(discovered).resolve())
    return _cli_search_names()[0]


def setup_workspace(
    *,
    scope: ScopeName,
    workspace_root: str | Path,
    db_path: str | Path | None = None,
    skill_root: str | Path | None = None,
    home: str | Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    refresh_routes: bool = False,
) -> SetupReport:
    workspace = Path(workspace_root).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace root must be a directory")
    home_path = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    destination = skill_destination(scope, home_path, workspace, skill_root)
    configured_db = (
        os.environ.get("HARNESS_FLEET_DB")
    )
    database = Path(db_path or configured_db or "harness-fleet.db").expanduser()
    database = (database if database.is_absolute() else workspace / database).resolve()
    if not database.is_relative_to(workspace):
        raise ValueError("setup database must stay below the workspace root")

    # Every first-party skill this checkout bundles rides along with setup, so a
    # fresh workspace gets the playbook for each preset it can run. Discovery is
    # by layout rather than a hardcoded list because the distributions differ:
    # the shared engine skills sit in harness_fleet/resources/<name>_skill, while
    # a variant's own primary skill lives in its package's resources/skill/<name>
    # (career-fleet). Anything absent is skipped, so the same code serves every
    # distribution — account-fleet installed neither the partner skill nor its
    # own until this was made generic.
    package_root = Path(__file__).resolve().parent
    engine_skill = bundled_skill_path().resolve()
    bundled_skills: list[tuple[Path, str]] = []
    seen_sources: set[Path] = {engine_skill}
    candidates = [
        *sorted((package_root / "resources").glob("*_skill")),
        *sorted(package_root.parent.glob("*/resources/skill/*")),
    ]
    for candidate in candidates:
        if not candidate.is_dir() or candidate.resolve() in seen_sources:
            continue
        seen_sources.add(candidate.resolve())
        # account_skill -> account-fleet; resources/skill/career-fleet keeps its name.
        skill_name = (
            f"{candidate.name.removesuffix('_skill')}-fleet"
            if candidate.name.endswith("_skill")
            else candidate.name
        )
        bundled_skills.append((candidate, skill_name))

    # Check every destination before writing any of them, so a conflicting
    # second skill cannot leave setup half-complete.
    skill_sources = [(bundled_skill_path(), destination)]
    skill_sources.extend(
        (source, destination.parent / skill_name) for source, skill_name in bundled_skills
    )
    for source, target in skill_sources:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError(f"skill destination is not a directory: {target}")
        if target.is_dir() and not _same_skill(source, target) and not force:
            raise FileExistsError(
                f"a different skill already exists at {target}; rerun with --force to update managed files"
            )

    actions = [
        _install_skill(source, target, dry_run=dry_run, force=force)
        for source, target in skill_sources
    ]
    refresh_result: dict[str, Any] | None = None
    if dry_run:
        actions.append(SetupAction(kind="database", status="planned", path=str(database)))
        actions.append(SetupAction(
            kind="routes",
            status="planned" if refresh_routes else "skipped",
            detail="provider discovery" if refresh_routes else "run routes --refresh when ready",
        ))
        observed_route_count = 0
    else:
        existed = database.exists()
        store = HarnessStore(database)
        actions.append(SetupAction(
            kind="database",
            status="unchanged" if existed else "created",
            path=str(database),
            detail=f"SQLite schema {store.schema_version()}",
        ))
        catalog = RouteCatalog(db_path=database)
        if refresh_routes:
            refresh_result = catalog.refresh_all()
            actions.append(SetupAction(kind="routes", status="updated", detail="provider catalogues refreshed"))
        else:
            actions.append(SetupAction(kind="routes", status="skipped", detail="run routes --refresh when ready"))
        observed_route_count = len(catalog.get_routes(free_only=True))

    from .providers.registry import configured_routes
    provider_ready = not dry_run and bool(configured_routes(catalog.get_routes(free_only=True)))
    skill_ready = dry_run or actions[0].status in {"created", "updated", "unchanged"}
    ready = not dry_run and skill_ready and provider_ready and observed_route_count > 0
    cli_command = installed_cli_path()
    # The product this install is, so the commands handed back are the ones the
    # user can actually run: an account-fleet install must not be told to run
    # harness-fleet.
    cli_name = Path(cli_command).name or "harness-fleet"
    stdio = StdioServerConfig(
        command=cli_command,
        args=["serve", "--workspace-root", str(workspace), "--db", str(database)],
    )
    next_commands: list[list[str]] = []
    # The default surface is the local MCP server: the assistant you already use
    # drives it, nothing is exposed to the network, and no key is needed. The
    # CLI stays for scripting and CI.
    next_commands.append([
        cli_name, "mcp", "install", "--workspace-root", str(workspace),
        "--db", str(database), "--json",
    ])
    if not refresh_routes or observed_route_count == 0:
        next_commands.append([cli_name, "routes", "--db", str(database), "--refresh", "--json"])
    doctor_command = [
        cli_name, "doctor", "--db", str(database), "--workspace-root", str(workspace),
        "--scope", scope, "--json",
    ]
    if skill_root is not None:
        doctor_command.extend(["--skill-root", str(skill_root)])
    next_commands.extend([
        doctor_command,
        [cli_name, "init", "my-task", "--db", str(database), "--workspace-root", str(workspace), "--preset", "classify"],
    ])
    return SetupReport(
        ready=ready,
        scope=scope,
        workspace_root=str(workspace),
        database=str(database),
        skill_path=str(destination),
        actions=actions,
        stdio_server=stdio,
        route_refresh=refresh_result,
        next_commands=next_commands,
    )

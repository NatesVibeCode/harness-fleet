"""Build and exercise a wheel in a fresh venv, outside the source checkout."""
import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

SKILLS = {
    "harness-fleet": "harness_fleet/resources/harness_skill",
    "account-fleet": "harness_fleet/resources/account_skill",
    "partner-fleet": "harness_fleet/resources/partner_skill",
    "career-fleet": "career_fleet/resources/skill/career-fleet",
}
RESOURCE_DIRS = (*SKILLS.values(), "harness_fleet/migrations", "harness_fleet/data",
                 "harness_fleet/resources/lanes", "harness_fleet/resources/examples/account_research",
                 "harness_fleet/resources/board", "harness_fleet/resources/studio")
REQUIRED_RESOURCES = (
    *(f"{directory}/SKILL.md" for directory in SKILLS.values()),
    *(f"harness_fleet/resources/lanes/{lane}.json" for lane in ("account", "career", "partner")),
    "harness_fleet/resources/studio/index.html",
    "harness_fleet/resources/board/index.html",
    "harness_fleet/resources/examples/account_research/task.json",
    "harness_fleet/resources/examples/account_research/sample_accounts.csv",
    "harness_fleet/resources/examples/career_screening/task.json",
    "harness_fleet/resources/examples/career_screening/sample_employers.csv",
    "harness_fleet/migrations/001_control_plane.sql",
    "harness_fleet/data/routes.seed.json",
)


def source_snapshot(repository: Path, destination: Path, includes: list[str]) -> None:
    repository = repository.resolve()
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"], cwd=repository,
        capture_output=True, check=True,
    )
    selected: set[Path] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, _, stage = metadata.split()
        if stage != b"0" or mode not in (b"100644", b"100755", b"120000"):
            raise ValueError(f"unsupported or unmerged indexed path: {os.fsdecode(name)!r}")
        selected.add(Path(os.fsdecode(name)))
    for name in includes:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or ".git" in path.parts:
            raise ValueError(f"--include must name a repository-relative file: {name!r}")
        selected.add(path)
    if not selected:
        raise ValueError("clean source snapshot has no indexed files")
    for relative in sorted(selected):
        source = repository / relative
        if any((repository / parent).is_symlink() for parent in relative.parents):
            raise ValueError(f"source path has a symlink parent: {relative}")
        if not source.is_file():
            raise ValueError(f"snapshot source must be an existing file: {relative}")
        if source.is_symlink():
            target = source.resolve()
            if not target.is_relative_to(repository) or target.relative_to(repository) not in selected:
                raise ValueError(f"symlink target is outside the selected snapshot: {relative}")
    destination.mkdir()
    for relative in sorted(selected):
        source = repository / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def resource_manifest(snapshot: Path) -> dict[str, str]:
    missing = [name for name in REQUIRED_RESOURCES if not (snapshot / name).is_file()]
    if missing:
        raise ValueError(
            "clean source snapshot is missing advertised skills/resources: "
            + ", ".join(missing)
            + "; intended new candidate files require an explicit --include PATH (repeatable)"
        )
    return {
        path.relative_to(snapshot).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in RESOURCE_DIRS
        for path in sorted((snapshot / directory).rglob("*"))
        if path.is_file()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", choices=("harness-fleet",), required=True)
    parser.add_argument("--offline-system-deps", action="store_true", help="Reuse installed dependencies when offline; does not verify dependency installation")
    parser.add_argument("--include", action="append", default=[], metavar="PATH", help="Include one intended new candidate file, relative to the repository; repeat as needed, never recursive")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="fleet wheel check ") as directory:
        root = Path(directory)
        snapshot = root / "source"
        try:
            source_snapshot(repository, snapshot, args.include)
            manifest = resource_manifest(snapshot)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            parser.error(str(exc))
        wheel_dir = root / "wheels"
        build_options = ["--no-build-isolation", "--no-index"] if args.offline_system_deps else []
        subprocess.run([sys.executable, "-m", "pip", "wheel", str(snapshot), "--no-deps", "--wheel-dir", str(wheel_dir), *build_options], cwd=root, check=True)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=args.offline_system_deps).create(environment)
        bindir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        wheel, = wheel_dir.glob("*.whl")
        install_options = ["--no-deps", "--no-index", "--ignore-installed"] if args.offline_system_deps else []
        subprocess.run([str(python), "-m", "pip", "install", str(wheel), *install_options], cwd=root, check=True)
        workspace = root / "sales workspace"
        workspace.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith((
            "PYTHONPATH", "HARNESS_FLEET", "OPENROUTER", "OPENAI", "OLLAMA", "LMSTUDIO", "VLLM", "GROQ", "CEREBRAS", "OPENCODE",
        ))}
        cli = bindir / (args.distribution + (".exe" if os.name == "nt" else ""))
        subprocess.run([str(python), "-I", "-c", """
import hashlib
import importlib
import json
import sys
from pathlib import Path
for package in ("harness_fleet", "career_fleet"):
    module = importlib.import_module(package)
    assert Path(module.__file__).is_relative_to(Path(sys.prefix)), module.__file__
import harness_fleet
site = Path(harness_fleet.__file__).parent.parent
for name, digest in json.loads(sys.argv[1]).items():
    resource = site / name
    assert resource.is_file(), f"installed resource missing: {name}"
    assert hashlib.sha256(resource.read_bytes()).hexdigest() == digest, name
""", json.dumps(manifest)], cwd=workspace, env=env, check=True)

        def invoke(*command):
            return subprocess.run([str(cli), *command, "--json"], cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)

        def run(*command):
            completed = invoke(*command)
            if completed.returncode:
                raise AssertionError(f"{command}: {completed.stdout}\n{completed.stderr}")
            return json.loads(completed.stdout)

        setup = run("setup", "--workspace-root", str(workspace))
        assert Path(setup["stdio_server"]["command"]).parent.resolve() == bindir.resolve(), setup["stdio_server"]
        for skill, directory in SKILLS.items():
            for name, digest in manifest.items():
                if name.startswith(directory + "/"):
                    installed = workspace / ".agents/skills" / skill / Path(name).relative_to(directory)
                    assert installed.is_file(), installed
                    assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest, installed
        repeated = run("setup", "--workspace-root", str(workspace))
        assert all(action["status"] == "unchanged" for action in repeated["actions"] if action["kind"] == "skill")
        preset = run("init", "score-smoke", "--preset", "score")
        assert preset["revision"]
        assert any(task["task_name"] == "score-smoke" for task in run("tasks")["tasks"])
        demo = run("quickstart", "--demo", "--run-id", "portable-demo")
        assert demo["verified"] == 10
        lanes = run("lane", "list")
        assert {"account", "career", "partner"} <= {lane["name"] for lane in lanes["lanes"]}, lanes
        account = next(lane for lane in lanes["lanes"] if lane["name"] == "account")
        run("init", "account-acceptance", "--preset", account["preset"])
        source_text = "Acme implemented a Kafka migration for Northwind Bank and cut latency by 40%."
        (workspace / "account.csv").write_text("item_id,text\nacme,\"" + source_text + "\"\n", encoding="utf-8")
        outcome = run("run", "account-acceptance", "--input", "account.csv", "--id-column", "item_id",
                      "--text-column", "text", "--run-id", "account-offline", "--route", "demo/fake",
                      "--sessions", "1", "--max-attempts", "2")
        assert outcome["result"]["total_verified_records"] == 1, outcome
        accepted = run("lane", "report", "account-offline", "--lane", "account", "--sample", "1")
        assert accepted["records"] == 1 and accepted["lane"] == "account", accepted
        assert accepted["truth"] and all(quote["exact"] and quote["live"] == "not_checked" for quote in accepted["truth"]), accepted
        before = run("status", "account-offline")
        for _ in range(2):
            resumed = run("resume", "account-offline", "--route", "demo/fake")
            assert resumed["result"]["total_verified_records"] == 1, resumed
            assert run("status", "account-offline") == before
        after = run("lane", "report", "account-offline", "--lane", "account", "--sample", "1")
        # The report stamps its own generation time, so idempotency is
        # everything else being byte-identical across a resume.
        for report in (accepted, after):
            report.pop("generated_at", None)
        assert after == accepted, after
        run("export", "portable-demo", "--format", "csv", "--sort-by", "score", "--desc", "--top", "2", "--rank", "--output", "ranked.csv")
        with (workspace / "ranked.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 2
        assert [row["rank"] for row in rows] == ["1", "2"]
        assert all(row["primary_quote_text"] for row in rows)
        assert run("status", "portable-demo")["status"] == "completed"
        failing = invoke("run", "score-smoke", "--input", str(root / "missing-input.csv"), "--route", "demo/fake")
        # This CLI reports failures as JSON on stdout ({"ok": false, ...}),
        # so a safe failure is a nonzero exit naming the missing input there.
        assert failing.returncode != 0 and "not found" in failing.stdout, failing
        mode = "reused system dependencies" if args.offline_system_deps else "fresh dependencies"
        print(f"{args.distribution}: clean snapshot wheel, installed resources, skills, offline demo, resume, idempotent setup, lane report, and safe failure passed ({mode})")


if __name__ == "__main__":
    main()

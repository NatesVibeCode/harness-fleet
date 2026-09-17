#!/usr/bin/env python3
"""Unified evaluation and tuning tool for fleet lanes, rungs, filters, and sources.

Provides a single CLI location to inspect, simulate, benchmark, and audit lanes
and their filters without having to invoke multiple separate scripts.

Usage:
    tools/lane_tune.py gate --text "..." [--lane partner] [--evidence snippet|fetched]
    tools/lane_tune.py sim --candidate acme.com --snippet "..." [--page "..."] [--lane partner]
    tools/lane_tune.py surface <domain|url> [--lane partner] [--probe]
    tools/lane_tune.py benchmark
    tools/lane_tune.py audit <database_or_workspace> [--json]
    tools/lane_tune.py compare <before.db> <after.db> [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repository root is on sys.path
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from harness_fleet.tune import (
    audit_database,
    compare_databases,
    diagnose_gates,
    diagnose_gates_batch,
    evaluate_surface,
    format_audit_report,
    format_compare_report,
    format_batch_gate_report,
    format_benchmark_result,
    format_gate_report,
    format_sim_report,
    format_surface_report,
    load_batch_file,
    run_benchmark,
    simulate_candidate,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lane_tune",
        description="Unified evaluation and tuning tool for fleet lanes, gates, and sources",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. Gate Diagnostics
    gate_cmd = sub.add_parser("gate", help="Diagnose and tune gate filters on a snippet or page text")
    gate_cmd.add_argument("--text", help="Text to evaluate (or read from --file / stdin)")
    gate_cmd.add_argument("--file", help="File containing single text to evaluate")
    gate_cmd.add_argument("--batch", help="Path to JSONL/CSV file of candidate records to evaluate in batch")
    gate_cmd.add_argument("--lane", default="partner", help="Lane profile to evaluate against (default: partner)")
    gate_cmd.add_argument("--evidence", choices=["snippet", "fetched"], default="snippet",
                          help="Evidence grade: snippet (elimination only) or fetched (qualification)")
    gate_cmd.add_argument("--size-min", type=int, help="Override profile size_min")
    gate_cmd.add_argument("--size-max", type=int, help="Override profile size_max")
    gate_cmd.add_argument("--location", action="append", help="Override profile locations (repeatable)")
    gate_cmd.add_argument("--vertical", action="append", help="Override profile verticals (repeatable)")
    gate_cmd.add_argument("--json", action="store_true", help="Emit raw JSON report")

    # 2. Candidate Funnel Simulation
    sim_cmd = sub.add_parser("sim", help="Simulate a candidate progressing through a lane's ladder of rungs")
    sim_cmd.add_argument("--candidate", default="candidate.example", help="Candidate domain or entity id")
    sim_cmd.add_argument("--snippet", default="", help="Search snippet text for rung 1")
    sim_cmd.add_argument("--page", default="", help="Page body text for rung 2")
    sim_cmd.add_argument("--stories", default="", help="Stories / case studies text for rung 3")
    sim_cmd.add_argument("--lane", default="partner", help="Lane whose ladder to simulate (default: partner)")
    sim_cmd.add_argument("--json", action="store_true", help="Emit raw JSON report")

    # 3. Source & Surface Evaluator
    surf_cmd = sub.add_parser("surface", help="Inspect URL surface classification or domain surface coverage")
    surf_cmd.add_argument("target", help="URL to classify or domain to probe")
    surf_cmd.add_argument("--lane", default="partner", help="Target lane context (default: partner)")
    surf_cmd.add_argument("--probe", action="store_true", help="Perform live network probe for sitemaps")
    surf_cmd.add_argument("--json", action="store_true", help="Emit raw JSON report")

    # 4. Filter Benchmark
    bench_cmd = sub.add_parser("benchmark", help="Run synthetic benchmark suite against current gate logic")
    bench_cmd.add_argument("--corpus", help="Path to custom JSONL/JSON benchmark corpus file")
    bench_cmd.add_argument("--json", action="store_true", help="Emit raw JSON report")

    # 5. Run Database Audit
    audit_cmd = sub.add_parser("audit", help="Audit a run database or workspace")
    audit_cmd.add_argument("database", help="Path to SQLite run database (e.g. fleet.db or workspace)")
    audit_cmd.add_argument("--json", action="store_true", help="Emit raw JSON report")

    # 6. Before/after comparison
    compare_cmd = sub.add_parser(
        "compare", help="Two run databases, as a per-rung delta (change one thing, look)"
    )
    compare_cmd.add_argument("before", help="Run database from before the change")
    compare_cmd.add_argument("after", help="Run database from after the change")
    compare_cmd.add_argument("--json", action="store_true", help="Emit raw JSON report")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "gate":
        profile_overrides: dict[str, Any] = {}
        if args.size_min is not None:
            profile_overrides["size_min"] = args.size_min
        if args.size_max is not None:
            profile_overrides["size_max"] = args.size_max
        if args.location:
            profile_overrides["locations"] = args.location
        if args.vertical:
            profile_overrides["verticals"] = args.vertical

        if getattr(args, "batch", None):
            items = load_batch_file(args.batch)
            batch_rep = diagnose_gates_batch(
                items,
                evidence=args.evidence,
                lane_name=args.lane,
                profile=profile_overrides or None,
            )
            if args.json:
                print(json.dumps(batch_rep.as_dict(), indent=2))
            else:
                print(format_batch_gate_report(batch_rep))
            return 0

        text = args.text
        if not text and args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        elif not text and not sys.stdin.isatty():
            text = sys.stdin.read()
        if not text:
            print("error: gate requires --text, --file, --batch, or piped stdin", file=sys.stderr)
            return 2

        report = diagnose_gates(
            text,
            evidence=args.evidence,
            lane_name=args.lane,
            profile=profile_overrides or None,
        )
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
        else:
            print(format_gate_report(report))
        return 0

    elif args.subcommand == "sim":
        sim = simulate_candidate(
            candidate=args.candidate,
            snippet=args.snippet,
            page_text=args.page,
            stories_text=getattr(args, "stories", "") or "",
            lane_name=args.lane,
        )
        if args.json:
            print(json.dumps(sim.as_dict(), indent=2))
        else:
            print(format_sim_report(sim))
        return 0

    elif args.subcommand == "surface":
        surf = evaluate_surface(
            args.target,
            lane_name=args.lane,
            probe_network=args.probe,
        )
        if args.json:
            print(json.dumps(surf.as_dict(), indent=2))
        else:
            print(format_surface_report(surf))
        return 0

    elif args.subcommand == "benchmark":
        bench = run_benchmark(corpus_path=getattr(args, "corpus", None))
        if args.json:
            print(json.dumps(bench.as_dict(), indent=2))
        else:
            print(format_benchmark_result(bench))
        return 0

    elif args.subcommand == "audit":
        aud = audit_database(args.database)
        if args.json:
            print(json.dumps(aud, indent=2))
        else:
            print(format_audit_report(aud))
        return 0

    elif args.subcommand == "compare":
        diff = compare_databases(args.before, args.after)
        if args.json:
            print(json.dumps(diff, indent=2))
        else:
            print(format_compare_report(diff))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

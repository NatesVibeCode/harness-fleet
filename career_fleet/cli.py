"""Unified CLI for career-fleet: deterministic career and employer screening."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import textwrap
from pathlib import Path

from career_fleet import __version__
from career_fleet.community_sources import (
    COMMUNITY_CONFIG_FILENAME,
    COMMUNITY_SOURCE_TYPES,
    configured_sources,
    default_community_config,
    load_community_config,
    write_default_community_config,
)
from career_fleet.lanes import (
    run_lane1_sourcing,
    run_lane2_triage,
    run_lane3_systems,
    run_lane4_culture,
)
from career_fleet.profile import IdealEmployerProfile
from career_fleet.setup import install_skill
from career_fleet.store import CareerStore

# Reconfigure stdout/stderr on platforms (like Windows cp1252) where console encoding fails on unicode
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass


def _ok(msg: str) -> str:
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return f"✓ {msg}"
    except Exception:
        return f"[OK] {msg}"


def _bullet() -> str:
    try:
        "\u2022".encode(sys.stdout.encoding or "utf-8")
        return "•"
    except Exception:
        return "*"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _workspace_path(value: str | Path, workspace_root: str | Path = ".") -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return Path(workspace_root).expanduser().resolve() / candidate


def get_profile(
    path: str | None = None,
    store: CareerStore | None = None,
    workspace_root: str | Path = ".",
) -> IdealEmployerProfile:
    p = _workspace_path(path, workspace_root) if path is not None else _workspace_path("profile.json", workspace_root)
    if p.exists():
        profile = IdealEmployerProfile.load(p)
        if store is not None:
            store.save_profile(profile)
        return profile
    if path is None and store is not None:
        stored_profile = store.load_profile()
        if stored_profile is not None:
            return stored_profile
    raise FileNotFoundError(
        f"No profile found at {p} or in the selected database. Run 'career-fleet profile --init' or pass --profile PATH."
    )


def _open_store(db_path: str, workspace_root: str | Path = ".") -> CareerStore | None:
    resolved_db = _workspace_path(db_path, workspace_root)
    try:
        return CareerStore(resolved_db)
    except (OSError, sqlite3.Error) as exc:
        print(f"Error: Could not open database {resolved_db}: {exc}", file=sys.stderr)
        return None


def cmd_init(args):
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    db_path = _workspace_path(getattr(args, "db", "career_fleet.db"), workspace)
    store = _open_store(str(db_path), workspace)
    if store is None:
        return 1
    profile_path = workspace / "profile.json"
    try:
        if profile_path.exists():
            prof = IdealEmployerProfile.load(profile_path)
        elif not getattr(args, "init", False) and (stored_profile := store.load_profile()) is not None:
            prof = stored_profile
            prof.save(profile_path)
        else:
            prof = IdealEmployerProfile()
            prof.save(profile_path)
            print(_ok(f"Initialized default Ideal Employer Profile at {profile_path}"))
            print(
                "  Next: fill in wedge_capabilities, required_stack and hiring_catalysts — "
                "the lanes cannot qualify anything while they are empty.",
            )
        store.save_profile(prof)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: Could not initialize profile {profile_path}: {exc}", file=sys.stderr)
        return 1
    try:
        community_path, created = write_default_community_config(workspace / COMMUNITY_CONFIG_FILENAME)
    except (OSError, ValueError) as exc:
        print(f"Error: Could not initialize community source plan: {exc}", file=sys.stderr)
        return 1
    if created:
        print(_ok(f"Initialized pre-filled career source plan at {community_path}"))
    print(_ok(f"Initialized CareerStore database at {db_path}"))
    return 0


def cmd_profile(args):
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    # An explicit --path names a file to read. It used to be written to when it
    # did not exist, which turned a typo into a silent all-default profile (and
    # then every lane scored zero against it).
    explicit_path = getattr(args, "path", None) is not None
    prof_path = _workspace_path(getattr(args, "path", None) or "profile.json", workspace)
    profile_exists = Path(prof_path).exists()
    if explicit_path and not profile_exists and not getattr(args, "init", False):
        print(
            f"Error: no profile at {prof_path}. Pass --init to create one there, "
            "or drop --path to use the workspace profile.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "init", False) and profile_exists and not getattr(args, "force", False):
        print(f"Error: Profile already exists at {prof_path}; use --force to replace it.", file=sys.stderr)
        return 1

    store = _open_store(getattr(args, "db", "career_fleet.db"), workspace)
    if store is None:
        return 1
    try:
        if getattr(args, "init", False) or (not profile_exists and store.load_profile() is None):
            prof = IdealEmployerProfile()
            prof.save(prof_path)
            store.save_profile(prof)
            print(_ok(f"Wrote initial Ideal Employer Profile to {prof_path}"))
            return 0
        if profile_exists:
            prof = IdealEmployerProfile.load(prof_path)
        else:
            prof = store.load_profile()
            if prof is None:
                raise FileNotFoundError(
                    f"No profile found at {prof_path} or in the selected database. Run 'career-fleet profile --init'."
                )
            prof.save(prof_path)
        store.save_profile(prof)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: Could not load or store profile {prof_path}: {exc}", file=sys.stderr)
        return 1
    print("==========================================================================================")
    print(f"                      IDEAL EMPLOYER PROFILE: {prof.profile_name} (v{prof.version})")
    print("==========================================================================================")
    print("Capabilities:")
    for c in prof.wedge_capabilities:
        print(f"  {_bullet()} {c}")
    print("\nRequired Stack:")
    for s in prof.required_stack:
        print(f"  {_bullet()} {s}")
    print("\nNegative Stack:")
    for s in prof.negative_stack:
        print(f"  {_bullet()} {s}")
    print("\nHard Dealbreakers:")
    print(f"  {_bullet()} Max Headcount: {prof.dealbreakers.max_headcount}")
    print(f"  {_bullet()} Verified Headcount: {prof.dealbreakers.require_verified_headcount}")
    print(f"  {_bullet()} Policy:        {prof.dealbreakers.policy}")
    print(f"  {_bullet()} Disallowed:    {', '.join(prof.dealbreakers.disallowed_locations)}")
    print(f"  {_bullet()} Reject Wrapper:{prof.dealbreakers.reject_thin_wrappers}")
    print(f"  {_bullet()} Reject Quota:  {prof.dealbreakers.reject_pure_quota}")
    print(f"  {_bullet()} Candidate TZ:  {prof.dealbreakers.candidate_timezone or '-'}")
    print(f"  {_bullet()} TZ Overlap:    {prof.dealbreakers.min_timezone_overlap_hours}h")
    print("\nHiring Catalysts:")
    for c in prof.hiring_catalysts:
        print(f"  {_bullet()} {c}")
    print("\nTarget Leadership:")
    for leader in prof.target_leadership:
        print(f"  {_bullet()} {leader}")
    print(f"\nAnchor Exemplars:  {', '.join(prof.anchor_companies)}")
    print("==========================================================================================")
    return 0


def _community_config_for_args(args):
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    config_path = _workspace_path(
        getattr(args, "config", COMMUNITY_CONFIG_FILENAME),
        workspace,
    )
    if not config_path.exists():
        write_default_community_config(config_path)
    return config_path, load_community_config(config_path)


def _entry_value(args, entry, name, fallback=None):
    value = getattr(args, name, None)
    return entry.get(name, fallback) if value is None else value


def _run_discovery_entry(args, store, profile, entry):
    source_type = str(entry.get("source") or getattr(args, "source", "")).strip()
    target = getattr(args, "target", None) or entry.get("target")
    if not target:
        raise ValueError(f"source '{source_type}' requires --target or a configured preset")
    raw_max = getattr(args, "max", None)
    max_items = raw_max if raw_max is not None else entry.get("max_items", 50)
    return run_lane1_sourcing(
        store=store,
        source_type=source_type,
        target=str(target),
        max_items=int(max_items),
        profile=profile,
        query=_entry_value(args, entry, "query"),
        subreddit=_entry_value(args, entry, "subreddit"),
        reddit_rss=bool(_entry_value(args, entry, "reddit_rss", False)),
        subreddit_sort=_entry_value(args, entry, "subreddit_sort", "new"),
        se_tagged=_entry_value(args, entry, "se_tagged"),
        se_site=_entry_value(args, entry, "se_site", "stackoverflow"),
        se_answers=bool(_entry_value(args, entry, "se_answers", False)),
        discourse_url=_entry_value(args, entry, "discourse_url"),
        lemmy_instance=_entry_value(args, entry, "lemmy_instance", "https://programming.dev"),
        include_low_signal=bool(getattr(args, "include_low_signal", False)),
        delay=float(_entry_value(args, entry, "delay", 0.2)),
        timeout=float(_entry_value(args, entry, "timeout", 20.0)),
    )


def _print_discovery_result(res, label=None):
    prefix = f"[{label}] " if label else ""
    if res.get("status") != "success":
        print(f"{prefix}Error: Lane 1 discovery failed: {res.get('error', 'unknown error')}", file=sys.stderr)
        return
    print(_ok(
        f"{prefix}Lane 1 Discovery completed: {res['companies_discovered']} companies discovered, "
        f"{res['postings_added']} source records ingested."
    ))
    if res.get("community_signals_added"):
        print(_ok(
            f"{prefix}Career focus retained {res['community_signals_added']} community signals "
            f"({res.get('unlinked_signals', 0)} unlinked leads)."
        ))
    if res.get("low_signal_skipped"):
        print(f"{prefix}Career-focus filter omitted {res['low_signal_skipped']} low-signal records.")
    if res.get("pages_skipped"):
        print(f"{prefix}Skipped pages: {res['pages_skipped']}")
    for warning in res.get("warnings", []):
        print(f"{prefix}Warning: {warning}", file=sys.stderr)


def cmd_discover(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    profile = store.load_profile()
    if getattr(args, "profile", None):
        try:
            profile = get_profile(args.profile, store=store, workspace_root=getattr(args, "workspace_root", "."))
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"Error: Could not load profile: {exc}", file=sys.stderr)
            return 1
    try:
        source = args.source
        target = getattr(args, "target", None)
        preset = getattr(args, "preset", None)
        if source == "community" and target:
            raise ValueError("--target cannot be combined with --source community; edit the source plan or select one source")
        if source == "community" or (source in COMMUNITY_SOURCE_TYPES and (not target or preset)):
            _, config = _community_config_for_args(args)
            entries = configured_sources(config, source=source, preset=preset)
            if source != "community" and target and preset:
                entries = entries[:1]
        else:
            if not target:
                raise ValueError(f"source '{source}' requires --target")
            entries = [{"source": source, "target": target}]
        if not entries:
            raise ValueError("no enabled community source presets are configured")

        results = []
        for entry in entries:
            label = entry.get("id") if len(entries) > 1 else None
            result = _run_discovery_entry(args, store, profile, entry)
            results.append(result)
            _print_discovery_result(result, label=label)
        failures = [result for result in results if result.get("status") != "success"]
        return 1 if failures else 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"Error: Lane 1 discovery failed: {exc}", file=sys.stderr)
        return 1


def cmd_sources(args):
    workspace = Path(getattr(args, "workspace_root", ".")).expanduser().resolve()
    config_path = _workspace_path(
        getattr(args, "config", COMMUNITY_CONFIG_FILENAME),
        workspace,
    )
    try:
        if getattr(args, "init", False):
            config_path, created = write_default_community_config(config_path, force=False)
            if created:
                print(_ok(f"Wrote pre-filled career source plan to {config_path}"))
        config = load_community_config(config_path) if config_path.exists() else default_community_config()
    except (OSError, ValueError) as exc:
        print(f"Error: Could not load community source plan: {exc}", file=sys.stderr)
        return 1
    if not config_path.exists():
        print(f"Using built-in defaults; run 'career-fleet sources --init' to write {config_path}.")
    print(f"Career source plan: {config_path}")
    for entry in config["sources"]:
        state = "enabled" if entry.get("enabled", True) else "disabled"
        target = entry.get("target", "-")
        query = entry.get("query") or "preset default"
        print(f"  {entry['id']}: {entry['source']} ({state})")
        print(f"    target={target} query={query} max_items={entry.get('max_items', 50)}")
    return 0


def _empty_profile_warning(prof) -> str | None:
    """Say so when the profile cannot qualify anything.

    `init` writes a valid but empty profile, so every deterministic matcher has
    nothing to match and the lanes quietly score zero. Saying it out loud is the
    difference between "no companies qualify" and "your profile is blank".
    """
    if prof.wedge_capabilities or prof.required_stack or prof.hiring_catalysts:
        return None
    return (
        "Warning: this profile has no wedge_capabilities, required_stack or "
        "hiring_catalysts, so no company can qualify. Fill in profile.json (or "
        "point --profile at a populated one) and re-run."
    )


def cmd_triage(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    try:
        prof = get_profile(
            getattr(args, "profile", None),
            store=store,
            workspace_root=getattr(args, "workspace_root", "."),
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: Could not load profile: {exc}", file=sys.stderr)
        return 1
    warning = _empty_profile_warning(prof)
    if warning:
        print(warning, file=sys.stderr)
    res = run_lane2_triage(store, prof)
    print("==========================================================================================")
    print("                              LANE 2: GATEKEEPER TRIAGE                                    ")
    print("==========================================================================================")
    print(f"Total Companies Triaged: {res['total_triaged']}")
    print(f"Surviving Candidates:   {res['survivors']} (proceed to Lane 3)")
    print(f"Disqualified Dealbreakers: {res['disqualified']} (dropped)")
    print("==========================================================================================")
    return 0


def cmd_recon(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    try:
        prof = get_profile(
            getattr(args, "profile", None),
            store=store,
            workspace_root=getattr(args, "workspace_root", "."),
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: Could not load profile: {exc}", file=sys.stderr)
        return 1
    lane = getattr(args, "lane", "all")
    warning = _empty_profile_warning(prof)
    if warning:
        print(warning, file=sys.stderr)

    if lane in ("systems", "all"):
        res3 = run_lane3_systems(store, prof)
        print(_ok(f"Lane 3 Systems Wedge evaluation completed on {res3['evaluated']} companies."))

    if lane in ("culture", "all"):
        res4 = run_lane4_culture(store, prof)
        print(_ok(f"Lane 4 Culture Recon evaluation completed on {res4['evaluated']} companies."))
    return 0


def cmd_list(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    status_filter = getattr(args, "status", None)
    companies = store.list_companies(status=status_filter)

    print("==========================================================================================")
    print(f"                            TRACKED COMPANIES [{status_filter or 'ALL'}]")
    print("==========================================================================================")
    print(f"{'ID':<20} {'Name':<24} {'Status':<14} {'ATS / Domain':<26}")
    print("-" * 90)
    for c in companies:
        ats = f"{c.get('ats_provider') or '-'}:{c.get('ats_token') or '-'}" if c.get("ats_provider") else (c.get("domain") or "-")
        print(f"{c['id']:<20} {c['name'][:22]:<24} {c['status']:<14} {ats[:25]:<26}")
    print("==========================================================================================")
    print(f"Total: {len(companies)} companies.\n")
    return 0


def cmd_signals(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    linked = True if getattr(args, "linked", False) else (False if getattr(args, "unlinked", False) else None)
    signals = store.list_community_signals(
        source_type=getattr(args, "source", None),
        linked=linked,
        limit=getattr(args, "limit", 100),
    )
    print("==========================================================================================")
    print("                              CAREER COMMUNITY SIGNALS")
    print("==========================================================================================")
    if not signals:
        print("No community signals found.")
        return 0
    for signal in signals:
        company = signal.get("company_name") or "unlinked lead"
        signal_types = ", ".join(signal.get("signal_types") or []) or "unclassified"
        print(f"[{signal['source_type']}] {signal['title']}")
        print(f"  Score:   {signal['relevance_score']:.3f} ({signal_types})")
        print(f"  Company: {company}")
        print(f"  Source:  {signal['source_uri']}")
    print(f"Total: {len(signals)} signals.")
    return 0


def _dossier_evidence_text(dossier: dict) -> str:
    """The employer dossier as labelled sections, so evidence reads can judge it.

    Categories here mean what they mean everywhere else in the fleet: job
    postings are ATS requisitions, community signals are third-party, and the
    company's own description is first-party. Without the labels an evidence
    read would treat marketing and a forum thread as the same kind of thing.
    """
    sections: list[str] = []
    # Only real prose counts as first-party evidence: a name and a domain are
    # identifiers, and emitting them as a section would invent a practice page.
    company_bits = [str(dossier.get("description") or ""), str(dossier.get("notes") or "")]
    company_text = " ".join(bit for bit in company_bits if bit.strip())
    if company_text.strip():
        origin = dossier.get("domain") or "company"
        sections.append(f"=== SECTION: FIRST_PARTY_PRACTICE (URI: https://{origin}/) ===\n{company_text}")
    for job in dossier.get("jobs", []):
        body = " ".join(str(job.get(key) or "") for key in ("title", "location", "raw_text"))
        job_uri = job.get("job_url") or f"job:{job.get('id')}"
        sections.append(f"=== SECTION: ATS_REQUISITIONS (URI: {job_uri}) ===\n{body.strip()}")
    for signal in dossier.get("community_signals", []):
        body = " ".join(str(signal.get(key) or "") for key in ("title", "body", "url"))
        signal_uri = signal.get("url") or f"signal:{signal.get('id')}"
        sections.append(f"=== SECTION: COMMUNITY_AND_SOCIAL (URI: {signal_uri}) ===\n{body.strip()}")
    for evaluation in dossier.get("evaluations", []):
        try:
            quotes = json.loads(evaluation.get("quotes_json") or "[]")
        except (TypeError, ValueError):
            quotes = []
        body = " ".join(
            str(q.get("text") or "") if isinstance(q, dict) else str(q) for q in quotes
        )
        if body.strip():
            lane = evaluation.get("lane") or "evaluation"
            sections.append(f"=== SECTION: FIRST_PARTY_PRACTICE (URI: lane:{lane}) ===\n{body.strip()}")
    return "\n\n".join(sections)


def cmd_dossier(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    dossier = store.get_company_dossier(args.company)
    if not dossier:
        print(f"Error: Company '{args.company}' not found.", file=sys.stderr)
        return 1

    print("==========================================================================================")
    print(f"                         COMPANY DOSSIER: {dossier['name']} ({dossier['id']})")
    print("==========================================================================================")
    print(f"Domain:      {dossier.get('domain') or '-'}")
    print(f"Headcount:   {dossier.get('headcount') if dossier.get('headcount') is not None else '-'}")
    print(f"HQ:          {dossier.get('hq_location') or '-'}")
    print(f"Timezone:    {dossier.get('timezone') or '-'}")
    print(f"Status:      {dossier.get('status')}")
    if dossier.get("disqualification_reason"):
        print(f"Reason:      {dossier['disqualification_reason']}")
    print(f"Sources:     {len(dossier.get('jobs', []))} source records captured")
    if dossier.get("jobs"):
        print("\nSources:")
        for job in dossier["jobs"]:
            print(f"  [{job['id']}] {job.get('title') or 'Untitled'}")
            print(f"    Source:   {job.get('source_type') or 'manual/legacy'}")
            print(f"    Location: {job.get('location') or '-'}")
            print(f"    URL:      {job.get('job_url') or '-'}")
            if getattr(args, "show_source", False):
                print("    Source text:")
                print(textwrap.indent(job.get("raw_text") or "", "      "))
    print("\nEvaluations:")
    for ev in dossier.get("evaluations", []):
        try:
            quotes = json.loads(ev.get("quotes_json") or "[]")
        except (TypeError, ValueError):
            quotes = []
        print(f"  [{ev['lane']}] Verdict: {ev['verdict']} (Score: {ev['score']}) via {ev.get('model_used') or 'heuristic'}")
        print(f"    Rationale: {ev['rationale']}")
        if quotes:
            print("    Quotes:")
            for q in quotes:
                print(f"      {_bullet()} \"{q}\"")
    evidence_text = _dossier_evidence_text(dossier)
    if evidence_text.strip():
        from harness_fleet.evidence import entity_evidence

        read = entity_evidence(evidence_text)
        present = [kind for kind, ok in read["kinds"].items() if ok]
        missing = [kind for kind, ok in read["kinds"].items() if not ok]
        print("\nEvidence kinds present:")
        print(f"  {', '.join(present) or 'none'}")
        print("Evidence kinds missing:")
        print(f"  {', '.join(missing) or 'none'}")
        if read["contradictions"]:
            print("\nClaims that stand alone:")
            for finding in read["contradictions"]:
                print(f"  - {finding['claim']}")
                print(f"    {finding['counterpart']}")
                for uri in finding["uris"][:3]:
                    print(f"    source: {uri}")
    print("==========================================================================================")
    return 0


def cmd_export(args):
    store = _open_store(getattr(args, "db", "career_fleet.db"), getattr(args, "workspace_root", "."))
    if store is None:
        return 1
    qualified = [c for c in store.list_companies() if c["status"] == "qualified"]
    dossiers = [store.get_company_dossier(c["id"]) for c in qualified]
    for dossier in dossiers:
        if not dossier:
            continue
        for evaluation in dossier.get("evaluations", []):
            try:
                evaluation["quotes"] = json.loads(evaluation.get("quotes_json") or "[]")
            except (TypeError, ValueError):
                evaluation["quotes"] = []
    out_path = Path(getattr(args, "output", "qualified_targets.json")).expanduser()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(dossiers, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Error: Could not write export {out_path}: {exc}", file=sys.stderr)
        return 1
    print(_ok(f"Exported {len(dossiers)} qualified company dossiers to {out_path}"))
    return 0


def cmd_setup(args):
    try:
        report = install_skill(
            args.workspace_root, force=args.force, dry_run=bool(getattr(args, "dry_run", False))
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: Could not install Career Fleet skill: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report))
    elif report.get("dry_run"):
        print(_ok(
            f"Dry run: would {report['action']} the Career Fleet skill at "
            f"{report['skill_path']} ({report['would_write']} file(s))"
        ))
        if report.get("reason"):
            print(f"  {report['reason']}")
    else:
        print(_ok(f"Career Fleet skill {report['action']} at {report['skill_path']}"))
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="career-fleet",
        description="Deterministic Career & Employer Screening with Multi-Lane Checks.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)


    p_init = subparsers.add_parser("init", help="Initialize SQLite DB and profile template")
    p_init.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_init.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_init.set_defaults(func=cmd_init)

    p_prof = subparsers.add_parser("profile", help="Inspect or generate Ideal Employer Profile")
    p_prof.add_argument(
        "--path", default=None,
        help="Path to an existing profile.json to read (default: profile.json in the workspace)",
    )
    p_prof.add_argument("--db", default="career_fleet.db", help="SQLite database path for the stored profile")
    p_prof.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_prof.add_argument("--init", action="store_true", help="Generate fresh default profile")
    p_prof.add_argument("--force", action="store_true", help="Replace an existing profile when used with --init")
    p_prof.set_defaults(func=cmd_profile)

    p_disc = subparsers.add_parser("discover", help="Lane 1: Sourcing & discovery")
    p_disc.add_argument(
        "--source",
        required=True,
        choices=["yc", "greenhouse", "ashby", "lever", "site", "community", *COMMUNITY_SOURCE_TYPES],
        help="Company/job source, one community source, or all configured community sources",
    )
    p_disc.add_argument("--target", help="Batch, tag, board token, or origin URL; omitted for a community preset")
    p_disc.add_argument("--preset", help="Named entry from career_sources.json")
    p_disc.add_argument("--config", default=COMMUNITY_CONFIG_FILENAME, help="Visible community source plan")
    p_disc.add_argument("--max", type=_positive_int, default=None, help="Maximum items to ingest (preset default is 10)")
    p_disc.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_disc.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_disc.add_argument("--profile", help="Optional profile.json used to rank career signals")
    p_disc.add_argument("--query", help="Career-focused search query; defaults to --target for query-based sources")
    p_disc.add_argument("--subreddit", action="append", help="Reddit subreddit restriction (repeatable)")
    p_disc.add_argument("--reddit-rss", action="store_true", default=None, help="Use fresh Reddit RSS instead of archive search")
    p_disc.add_argument("--subreddit-sort", choices=["new", "hot", "top", "rising"], default=None)
    p_disc.add_argument("--se-tagged", action="append", help="Stack Exchange tag restriction (repeatable)")
    p_disc.add_argument("--se-site", default=None, help="Stack Exchange site")
    p_disc.add_argument("--se-answers", action="store_true", default=None, help="Include top Stack Exchange answers")
    p_disc.add_argument("--discourse-url", help="Discourse instance when --target is the career query")
    p_disc.add_argument("--lemmy-instance", default=None, help="Lemmy instance")
    p_disc.add_argument("--include-low-signal", action="store_true", help="Keep community records that do not match the career-focus filter")
    p_disc.add_argument("--delay", type=float, default=None, help="Delay between community fetches in seconds")
    p_disc.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")
    p_disc.set_defaults(func=cmd_discover)

    p_sources = subparsers.add_parser("sources", help="Show or write the pre-filled career source plan")
    p_sources.add_argument("--init", action="store_true", help="Write career_sources.json if it does not exist")
    p_sources.add_argument("--config", default=COMMUNITY_CONFIG_FILENAME, help="Visible community source plan")
    p_sources.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_sources.set_defaults(func=cmd_sources)

    p_trig = subparsers.add_parser("triage", help="Lane 2: Gatekeeper triage (dealbreakers)")
    p_trig.add_argument("--profile", default=None, help="Path to profile.json (default: ./profile.json; otherwise use the active IEP in SQLite)")
    p_trig.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_trig.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_trig.set_defaults(func=cmd_triage)

    p_rec = subparsers.add_parser("recon", help="Lanes 3 & 4: Systems wedge & culture recon")
    p_rec.add_argument("--lane", choices=["systems", "culture", "all"], default="all", help="Which lane to run")
    p_rec.add_argument("--profile", default=None, help="Path to profile.json (default: ./profile.json; otherwise use the active IEP in SQLite)")
    p_rec.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_rec.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_rec.set_defaults(func=cmd_recon)

    p_list = subparsers.add_parser("list", help="List tracked companies")
    p_list.add_argument("--status", choices=["discovered", "triaged", "qualified", "disqualified"], help="Filter by status")
    p_list.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_list.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_list.set_defaults(func=cmd_list)

    p_sig = subparsers.add_parser("signals", help="List career-focused community signals and unlinked leads")
    p_sig.add_argument("--source", choices=list(COMMUNITY_SOURCE_TYPES), help="Filter by community source")
    linked_group = p_sig.add_mutually_exclusive_group()
    linked_group.add_argument("--linked", action="store_true", help="Show signals linked to a company")
    linked_group.add_argument("--unlinked", action="store_true", help="Show signals needing company attribution")
    p_sig.add_argument("--limit", type=_positive_int, default=100, help="Maximum signals to show")
    p_sig.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_sig.add_argument("--workspace-root", default=".", help="Workspace root for relative paths")
    p_sig.set_defaults(func=cmd_signals)

    p_dos = subparsers.add_parser("dossier", help="Inspect company dossier")
    p_dos.add_argument("--company", required=True, help="Company ID")
    p_dos.add_argument("--show-source", action="store_true", help="Print complete captured source text")
    p_dos.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_dos.set_defaults(func=cmd_dossier)

    p_exp = subparsers.add_parser("export", help="Export qualified company dossiers")
    p_exp.add_argument("--output", default="qualified_targets.json", help="Output JSON path")
    p_exp.add_argument("--db", default="career_fleet.db", help="SQLite database path")
    p_exp.set_defaults(func=cmd_export)

    p_setup = subparsers.add_parser("setup", help="Install the bundled assistant skill into a workspace")
    p_setup.add_argument("--workspace-root", default=".", help="Workspace directory (default: current directory)")
    p_setup.add_argument("--force", action="store_true", help="Replace a different existing Career Fleet skill")
    p_setup.add_argument("--dry-run", action="store_true", help="Preview what setup would install without writing anything")
    p_setup.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_setup.set_defaults(func=cmd_setup)

    args = parser.parse_args()
    result = args.func(args)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())

# One repo, one version, lanes chosen at the MCP level

## The idea

Collapse the fleet of repos into **one public repo, one distribution, one
version**. The products stop being repos and become **lanes** — configuration
you pick when you use the tool, at the MCP level (and on the CLI), not something
you install separately.

This is not a cosmetic merge. It removes whole categories of work:

| Today | After |
| --- | --- |
| 3–4 repos kept byte-identical by a drift checker | one codebase; drift checks become unnecessary |
| a `branding.py` per distribution | one brand; lanes carry the differences |
| 4–5 skills (product + engine), each with its own stale-command risk | one procedure skill; lanes are config (S7) |
| version strings to sync across distributions, a tag ladder per repo | one version, no ladder (see `versioning.md`) |
| "which fleet do I install?" | one install; pick your lane |
| product-specific forks risk reappearing | impossible — there is nothing to fork |

## What a lane becomes

A lane is already defined (`lanes/<lane>.json`, closed schema: seeds, queries,
sources, channels, title/remote filters, preset, tier bar, output). Today it is
a workspace file; after the merge the package **ships a set of lanes**
(`account`, `career`, `partner`, plus the generic ones) and a user may add their
own. Nothing about the model changes — the file just stops being per-repo.

## Picking a lane at the MCP level

One server, lane-aware tools, so an assistant chooses the lane for the task
instead of the user choosing a binary:

- `lane` as an argument on the pipeline tools (`harness_fleet_research`,
  `harness_fleet_lane_report`, …), defaulting to a configured lane.
- a lane-listing tool (`harness_fleet_lanes`) so the assistant can see what is
  available and what each lane is for.
- the lane used for a run is recorded in the run, so a result is never ambiguous
  about which lane produced it.

CLI equivalent: `--lane <name>` (already wired on `research`; extend to the rest).

## Risks and how they are handled

1. **Personal data must never enter the public repo.** The career side has real
   databases, a CRM snapshot and a private research worker checkout. After the
   merge there is *one* public repo, so the rule is absolute: no `*.db`, no
   `runs/`, no workspace artifacts — `.gitignore` plus a test that fails if a
   database or run directory is tracked. Career's *lane* ships; career's *data*
   never does.
2. **Losing history.** A brand-new repo means a fresh history (one import
   commit) unless we import the existing history. Recommendation: fresh history
   — it also gives the clean "no tag ladder" start, and the old repos stay as
   read-only archives for reference.
3. **Anyone who bookmarked or cloned a product repo.** PyPI was never published,
   so no install path is broken. The old repos get archived with a note pointing
   at the new one, not deleted outright until the new repo is proven.
4. **Name.** One product needs one name. Options: keep `harness-fleet` as the
   umbrella (history and remotes already exist), or take a new name for the
   fresh start. Yours to call.

## Steps, with their checks

| # | Step | Check |
| --- | --- | --- |
| R1 | Decide name, repo, and whether history is imported | a decision recorded in this file |
| R2 | Create the repo from the current engine, plus `lanes/` presets and one skill | `pip install` from a clean venv, no extras, `--version` reports one version |
| R3 | Lane-aware MCP tools + `--lane` on every command that needs it | a lane-driven run end to end through MCP; the lane is recorded in the run |
| R4 | Guard: no data files tracked, no run artifacts, no workspace registries | a test fails if `*.db`, `runs/`, or `source_registry.json` is tracked |
| R5 | Migrate docs: one README, one skill, the three plans, the versioning policy | `tests/test_docs_commands.py` green; every documented command parses |
| R6 | Archive the old repos with a pointer to the new one | the pointer is the first line of their READMEs |
| R7 | First release only when R2–R6 hold | one tag, one release, clean-venv install verified |

## Why this supersedes other queued work

- Partner Finder's extraction (`partner-product.md`) becomes "add a lane and a
  preset", not "make a new repo".
- The skills consolidation (S7) becomes structural: there is only one skill.
- The boundaries plan's drift contract stays valuable *within* the repo (module
  boundaries, locked surfaces) but stops being a cross-repo job.
- The versioning decision gets easier: one identity, no per-product sync, no
  ladder.

## Status

## Status: the merge is done, in the existing repo

Name: **harness-fleet** (confirmed; it is ours on PyPI). History: kept — a fresh
repo turned out to buy nothing the archive does not.

| Step | State |
| --- | --- |
| Lanes ship with the tool | **done** — `account`, `career`, `partner` as package data, selectable through `harness_fleet_lanes` and `lane` on `harness_fleet_run` |
| account-fleet absorbed | **done** — it had nothing unique but its own database; repository archived |
| career-fleet absorbed | **done** — `career_fleet/`, its skill (3 copies), examples and 9 test files moved in; `career-fleet` declared as a second entry point so the commands its docs describe exist; repository archived |
| Docs guard | **strengthened** — it reads this distribution's console scripts and checks each surface's documented commands with that surface's own parser |
| Career's data | **stayed out** — databases, profile and target files are refused by `test_shipped_lanes.py` |
| One skill (S7) | **done** — the `harness-fleet` skill is the single procedure and now carries the lanes table; each product skill opens by declaring its lane (`This is the `career` lane`) and defers the workflow to it, keeping only what is lane-specific |
| Single release | **not done** — deliberately; see versioning.md |

Harness carries everything: **1301 passed, 78 skipped**, ruff and mypy clean.

What is left is documentation, not code: S7's skill collapse, and one release when
there is something worth handing someone.

---

## Adding a surface (the whole recipe)

With one repo, a new surface costs **one JSON file, a preset, a paragraph, and a
proof run**. Partner is the first one under this model; anything nobody has
thought of yet works the same way.

1. **The lane** — `lanes/<name>.json`: seeds and queries, backends and channels,
   title/location/remote filters, the preset it scores with, the tier bar it
   demands, and how it presents results (`top`, `min_score`). Closed schema, so
   a lane cannot smuggle in a mechanism override.
2. **A preset, only if needed** — if no existing checklist fits, add one in
   `task.py`. The *concepts* stay central (`CLAIM_CONCEPTS`): a lane names its
   items so they map onto them (`q2_stack_delivery` → `stack_delivery`). Points
   and `evidence_terms` are the legitimate edge-specific parts.
3. **Channels, only if needed** — a source the engine cannot speak to is a file
   in `<workspace>/sources/`: declarative JSON for "fetch this list, follow the
   items", a Python module for anything else. Its category is validated against
   the taxonomy.
4. **Nothing to do about unknown domains** — they promote themselves as they are
   seen, with provenance, and `sources demote` is the correction. A new surface
   therefore gets better on its own as it runs.
5. **One paragraph in the shared skill** plus a row in its lanes table. No new
   skill, and no sub-skill unless the lane has genuinely different *procedure*
   (S7's criterion).
6. **Prove it** — `lane report` on a frozen sample, then one real run whose
   truth sample a person can open.

**Never, for any surface:** the evidence bar, confidence levels, tier minimums,
the aggregation rule, or connector internals. Those are shared; changing one is
a fleet-wide mechanism change that applies to every lane, by design.

**Cost of a new surface:** zero new repos, zero branding, zero drift jobs, zero
version bumps, and no skill to keep true.


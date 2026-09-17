# The Tuning Plan

How a lane gets *good* without the shared mechanisms changing. Companion to
[boundaries-plan.md](boundaries-plan.md): that one says what is central, this
one says how the per-lane configuration on top of it is set, measured and kept
honest.

> **See also:** [partner-product.md](partner-product.md) — the Partner Finder extraction (rename, own public repo, its own skill and lane). Queued, not started.

The one-line rule:

> Mechanisms are shared and locked. Everything that differs between lanes is
> configuration, and configuration changes only when a measurement says it
> should.

---

## 1. What is tunable, and what is not

**Tunable (per lane, a file):**

| Knob | Example |
| --- | --- |
| seed entities / queries | a company, a vendor, a set of `site:` queries |
| sources and channels | ATS boards, vendor hubs, feeds, `sources/*.py` channels |
| filters | `title_include` (`enterprise sales`, `sales ops`), `title_exclude`, remote/location, `min_chars`, evidence grade |
| preset / checklist | which task the lane scores with, and its `evidence_terms` vocabulary |
| weights and half-lives | per-domain source weights, per-item recency decay |
| tier bar | which tier the lane requires, and what it does when unmet |
| registry and channels | which domains are promoted, which channels are installed |
| output | top N, columns, rank, min score, confidence floor |

**Not tunable — a lane may not touch these, ever:**

- what a claim requires (`CLAIM_CONCEPTS`) and which categories may carry it (`QUALIFYING_CATEGORIES`)
- the confidence levels and their meaning
- how evidence aggregates (one qualifying source, no continuum)
- tier minimums (`TIER_MINIMUMS`)
- connector internals, the registry growth pass, the DAG executor, the scoring pipeline

If a lane needs one of those changed, that is a *mechanism* change: it lands in
`contracts.py`, is locked, and applies to every lane.

---

## 2. Where a lane lives

```
lanes/<lane>.json          # the configuration (this plan's subject)
tasks/<preset>.json        # the checklist it scores with (already exists as presets)
sources/                   # channels this lane installs (already exists)
source_registry.json       # domains this lane has promoted (already exists)
```

A lane file is closed-schema validated like everything else, carries a
`revision`, and is addressed as `--lane <name>` on the shared commands. No lane
file may contain code; if a source needs code it is a channel, and channels are
already the sanctioned escape hatch.

---

## 3. How tuning is measured (otherwise it is guessing)

Every lane run produces a `lane report` with five numbers, and a change is only
kept if it moves one of them without wrecking another:

1. **Yield** — sources attempted vs captured, per backend and per channel. A
   lane pointed at the wrong surface shows up here first (career's community
   sources: 25 signals, 0 postings).
2. **Coverage** — per entity, which claim kinds are present; and the share of
   entities meeting the lane's tier bar.
3. **Support quality** — of the scored claims, how many have a qualifying
   source, how many were refused and why.
4. **Truth sample** — N randomly chosen scored entities per run: quote
   character-exactness, quote still present on the live page, and claim↔quote
   addressing. Reported as rates, with the URLs, so a person can spot-check.
5. **Cost and time** — routes used, attempts, wall time per 100 entities.

Comparisons are only valid on a **frozen sample**: a saved source set and a
saved scoring input, so two configs are judged on identical inputs.

---

## 4. The tuning loop (offline, bounded)

1. Run the lane on the frozen sample → `lane report`.
2. Change **one** knob, and write down the hypothesis ("ATS boards with a
   title filter beat community sources for role coverage").
3. Re-run on the same sample.
4. Keep the change only if the target metric improved and no other metric
   regressed beyond noise; otherwise revert it. Record the outcome either way.

The loop is human-driven and off the scoring path: no lane may learn at scoring
time, for the same reason the registry growth pass does not.

---

## 5. Steps, with their checks

| # | Step | Check |
| --- | --- | --- |
| S1 | Surface the new capabilities: `sources list\|propose\|promote`, `sources channels`, and the same over MCP | CLI parses, verbs work on a temp registry, MCP exposes them, docs test stays green |
| S2 | Lane file: closed schema + loader + `--lane` on `research`/`discover`/`fetch`/`run` | an invalid lane file fails with a named field; a valid one drives a run |
| S3 | `lane report`: the five measurements, printed and written next to the run | a run on the frozen sample produces the report; numbers match the artifacts |
| S4 | Two reference lanes tuned as proofs: **account** (seed a company → partner/customer targets → scored list) and **career** (enterprise sales/ops, remote, via ATS boards + title filters) | each lane beats its starting config on coverage on the frozen sample |
| S5 | Real end-to-end proof per product against its own lane | one real run per product, with its `lane report` attached, plus a truth sample a person can open |
| S6 | Lock it: lane schema + loader tests, mechanisms untouched | drift still `PASS 3/3`, lanes are data, `contracts.py` unchanged by any lane work |

---

## 6. Rules of engagement (same as the boundaries plan)

1. No step is done without its check, quoted.
2. A lane never changes a mechanism; a mechanism change never hides in a lane.
3. Every kept tuning change names the metric it moved and the sample it moved on.
4. No silent zeroes: a source that yields nothing says why.
5. Report the gaps: what a lane still cannot see is part of the report.

---

## 7. Status ledger

| Step | State | Evidence |
| --- | --- | --- |
| S1 Surface the capabilities | **done** | CLI (`sources list\|propose\|promote\|channels`) and MCP (`harness_fleet_sources`, `harness_fleet_promote_source`) both drive the registry and channels; the live smoke promoted a domain and showed it classifying as evidence, and an unknown category is refused with the valid list. Tests: `test_sources_cli.py` (6) + an MCP round trip through the stdio server, including the refusal. Suite: 1176 passed, 78 skipped; drift PASS. |
| S2 Lane file | **done** | `harness_fleet/lanes.py`: closed-schema `Lane` (queries, backends, channels, title filters, remote, preset, tier, require_kinds, top, min_score, revision); unknown keys refused so a lane cannot smuggle in a mechanism override; each bad field is named. `--lane` on `research` supplies queries/sources/preset/output and *applies* the lane's filters, reporting the drop count. Locked in drift; `tests/test_lanes.py` (6) + `tests/test_lane_flow.py` (4). |
| S3 lane report | **done** | `harness_fleet/lane_report.py` + `lane report <run_id>` (CLI) and `harness_fleet_lane_report` (MCP): yield, coverage vs the lane's bar, support quality with the engine's own refusal reasons, a seeded truth sample (exact offsets / live / addressed; fetcher injectable so tests stay offline), and cost. Writes `runs/<id>/lane_report.json`; `--freeze DIR` saves the input + registry snapshot for like-for-like comparison. `research` now persists `discovery_report.json`, which is what gives yield a real denominator. Verified on a live lane run; 19 tests. |
| S4 Reference lanes | **done** | all three lanes ran live and were measured. Account: 6-12 dossiers, 33-50% clearing its bar, 15/15 sampled quotes exact and addressed. Career: 2-3 dossiers, 67-100% clearing `delivery_hiring`, 100% quote precision. Partner: 1-5 dossiers, 0% clearing the tier-1 floor — its queries surface vendor award pages, several of which are unfetchable (`partner.microsoft.com` serves an incomplete TLS chain) or disallowed, and the records that do land carry delivery proof without our stack. That is a tuning case with a number attached, not a claim. |
| S5 Per-product proof | not started | — |
| S6 Lock | **done** | drift `PASS` on the single checkout (the sibling repositories are archived; `_default_repos` returns this one). Locked: contracts, evidence bar, lanes, lane report, and the `EXACT_FILES` allowlist. |

---

## 8. Step S7 — one skill, lane config inside it, sub-skills only when earned

The skills must follow the same boundary as the code: one procedure, per-lane
configuration. Today each product ships a full playbook
(`account-fleet`, `career-fleet`, `partner-fleet`) plus the engine skill
(`harness-fleet`), and the per-product copies are exactly where the stale-command
bugs came from — account's own README telling users to run a binary it does not
ship, career documenting a `setup --dry-run` that did not exist.

**Structure to land:**

* **One procedure skill** — the actual know-how, written once: seed a lane →
  discover → bundle per entity → score → read the evidence readout *with its
  confidence* → export the ranked list → see what is missing → grow sources
  (registry and channels promote themselves; `demote` corrects) → run a review
  round when a tier bar is unmet.
* **Per-lane configuration, not per-lane prose** — `lanes/<lane>.json` holds
  sources, filters, preset, tier bar and output shape. The skill *reads*
  the lane file; it never restates it, so a lane change is a config change and
  nothing else has to be kept true by hand.
* **Per-product install name only** — `account-fleet` / `career-fleet` skills
  remain the thing a user's assistant finds by name, but they become thin
  pointers: "the procedure is the shared skill; your lane is X".

**The criterion for a sub-skill:** a sub-skill is justified only when a lane has
genuinely different *procedure*. Different queries, checklist, tier bar or
output shape are configuration, not procedure. On the current audit all four
skills run the identical procedure, so there are **no sub-skills**; if a lane
later grows a real extra step (a human verification gate, a paid data source
with its own rules), that lane earns a sub-skill and no other lane does.

**Check:** every documented command still parses against the real CLI
(`tests/test_docs_commands.py`), plus a new assertion that each
installed product skill declares its lane and defers procedure to the shared
skill. Written down here rather than migrated immediately: the change touches
`skills/*/SKILL.md` and their references together with the tests
that pin the packaged copies byte-identical, and a rushed doc migration is how
the wrong-binary breakage happened the first time.


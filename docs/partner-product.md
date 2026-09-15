# Partner Finder — extraction into its own public product

Goal: `partner-fleet` becomes its own public distribution, renamed, with its own
skill, built the same way as the other two products: **one shared engine, one
per-product `branding.py`, one lane config, and nothing forked.**

## Naming (provisional — needs your call)

| Thing | Proposed | Note |
| --- | --- | --- |
| Product / repo | **Partner Finder** (`partner-finder`) | your suggestion; the `-fleet` suffix goes away for this one unless you want it kept |
| Python distribution | `partner-finder` | `pip install partner-finder` |
| CLI | `partner-finder` | one binary, the shared surface |
| Skill | `partner-finder` | installed as `.agents/skills/partner-finder/` |
| Lane | `lanes/partner.json` | the only product-specific file |

Say the word if you prefer `partner-finder-harness`, `partners`, or keeping
`partner-fleet`; the mechanics below are identical either way.

## What already exists (the extraction inventory)

| Piece | Today | Where it goes |
| --- | --- | --- |
| Partner pipeline | `harness_fleet/partner_sourcing.py` (638 lines), `partner.py` (97) | stays in the **shared engine** — it is the mechanism, not the product |
| Source plan | `harness_fleet/data/partner_sources.json` (286) | shared engine (data, not branding) |
| Skill | `skills/partner-fleet/` + `.agents/skills/partner-fleet/` + `harness_fleet/resources/partner_skill/` | becomes the product's own skill, procedure per S7 with `lanes/partner.json` carrying the specifics |
| Examples | `examples/partner_research/` | moves to the new repo |
| Tests | `test_partner_sourcing.py`, `test_partner_research.py`, `test_vendor_stories.py` | split: engine tests stay, product tests move |
| CLI | `partners` command + `partner-research` preset in `harness_fleet/cli.py` / `task.py` | stays shared (all products may use it); the product decides to lead with it |
| Docs | `docs/tuning-plan.md`, `test_docs_commands.py`, `test_setup.py`, READMEs reference `partner-fleet` | updated as part of the rename |

## Steps, with their checks

| # | Step | Check |
| --- | --- | --- |
| P1 | Pick the name (yours) and add `branding.py` for the new distribution | `partner-finder --version` reports its own name; MCP server and `setup` name the installed product (already dynamic) |
| P2 | New repo from the engine, exactly like `account-fleet`: same `harness_fleet/` (drift-locked), `branding.py`, `pyproject.toml`, `lane` file | drift compares **four** repos; the new one is byte-identical on every locked file |
| P3 | Rename inside the skill, docs and packaged copies (`skills/partner-finder/…`) | `tests/test_docs_commands.py` green in every repo — no documented command that does not parse, no foreign binary |
| P4 | The skill: one procedure, lane config inside (S7 rule — no sub-skill unless the lane has genuinely different *procedure*) | the skill declares its lane and defers procedure to the shared playbook |
| P5 | Public repo hygiene: README, FREE-ACCESS, SECURITY, CONTRIBUTING, LICENSE, examples, `pip install` verified from a clean venv | install works with no extras; `research --lane partner` runs offline on the demo route |
| P6 | First real proof run: seed a vendor (e.g. Databricks/AWS partner hubs) → qualified partner list, with `lane report` attached | one real run, five measurements, a truth sample a person can open |

## Boundary rules that apply (from the other two plans)

1. The engine stays shared and locked; only `branding.py` and the lane file differ.
2. No forked module. If the product needs new behaviour, it lands in the engine and becomes available to every product.
3. The skill is one procedure plus lane config; per-lane prose is a bug.
4. Nothing ships with a documented command that does not exist (P3's check).
5. This is "soon", not now: recorded here so it is executed deliberately rather
   than in a rush, and so the rename happens once across skills, docs and
   packaging instead of piecemeal.

---
name: harness-fleet
description: Run repeatable bulk classification, extraction, summarization, and triage across free and local LLM workers with closed fields, exact quote offsets, SQLite checkpoints, bounded attempts, and explicit route-price evidence. Use when every returned claim must be typed and traceable to its source; do not use for open-ended agent delegation.
---

# Harness Fleet

Deliver a validated `harness_fleet_v2` packet from typed input while SQLite retains the exact task revision, queue state, attempts, verified results, routes, and receipts.

Start with `command -v harness-fleet`. On a fresh system, run `harness-fleet setup --workspace-root "$PWD" --refresh-routes --json`, then execute its typed `next_commands` in order.

## Lanes: pick your product

There is one procedure. What differs between products is a **lane** — a config
file, not a different tool:

| Lane | What it is for | Preset | Bar |
| --- | --- | --- | --- |
| `account` | target accounts for a technical ICP | `account-research` | tier_2 |
| `career` | enterprise sales/ops roles, remote, by title | `triage` | tier_3 |
| `partner` | implementation partners from vendor stories | `partner-research` | tier_1 |

## Two stages: find, then go and look

A search returns pages *about* a company, which is rarely the evidence a lane's
bar asks for — the stack a firm delivers, who it is hiring and what it charges
live on its own site, its hiring board and the stories its vendors publish. So a
run has two stages:

1. **Discovery** — search the lane's queries, and (for lanes that declare
   `stories`) enumerate the customer stories vendors publish about their
   customers. One query returns a handful of hits; a vendor's own story index
   holds hundreds, and every story is prose the vendor published about a named
   company. This is where volume comes from.
2. **The walk** — for each entity still short of the lane's bar, visit the
   surfaces that carry the missing kinds: its sitemap (which answers for every
   domain tested and names the real case-study URLs), its case studies and
   services pages, its hiring board through its JSON API, the vendor stories
   that name it, and the communities discussing it. `--no-enrich` stops after
   stage 1.

Every surface is aimed rather than swept: only entities short of the bar are
walked, only for the kinds they are missing, and what a surface contributes to
one dossier is bounded — with the surplus counted and reported, never dropped
quietly.

Surfaces that answer are declared once in `harness_fleet/data/source_surfaces.json`;
so are the ones that turned out not to (clutch.co and g2.com answer 403 for
every domain, Bluesky's public search 403s, YouTube transcripts are gated) —
each removal is recorded with its reason so nobody re-adds one from memory.

```bash
harness-fleet lane list                    # the lanes this install can run
harness-fleet research --lane partner --stories 80   # hundreds of candidates from vendor story indexes
harness-fleet research --lane account --no-enrich    # search only: skip the walk
harness-fleet research --lane career          # the whole pipeline for one lane
harness-fleet lane report <run_id> --lane career   # what it actually produced
```

Over MCP: `harness_fleet_lanes` lists what this install can run, and every
pipeline tool takes `lane`, so the assistant picks the product for the task.

A lane carries seeds, queries, sources, title/remote filters, the preset it
scores with, its tier bar and its output shape. Add your own by dropping
`lanes/<name>.json` in the workspace; it overrides a shipped lane of the same
name. What a lane may **not** do is change a mechanism — the evidence bar,
confidence levels, tier minimums or the scoring pipeline are shared by every
lane, on purpose.

## Choose the operation

- To define or change extraction fields, read [references/task-contracts.md](references/task-contracts.md).
- To test, run, resume, monitor status, evaluate routes, export, inspect routes, or configure MCP, read [references/operations.md](references/operations.md).
- For tabular data, use CSV input (`--id-column`, `--text-column`) and export (`--format csv|jsonl`). HTML, PDF, TXT, and MD are also accepted as single-item inputs.
- For status queries, run `harness-fleet status <run_id>` or use MCP `harness_fleet_status`; do not resume or start runs for monitoring.
- To benchmark routes against task samples before large runs, use `harness-fleet eval TASK --input FILE --concurrency 4` or MCP `harness_fleet_eval`.
- For a zero-key proof in 30s, run `harness-fleet quickstart --demo` (deterministic `demo/fake` provider, no API keys).
- For schema bootstrapping from labels, use `harness-fleet init NAME --from-example labels.csv [--label-column label]`.
- To choose which harnesses and models a run may use, start `harness-fleet studio` (localhost-only settings companion). It shows each harness's own login command, lists the models that harness reports in `free` or `specific` mode, and saves the selection to SQLite; `harness-fleet settings` prints it back. The page then shows the exact `run` command for that selection (`--from-studio`). It never handles credentials, never edits a scoring contract, and never starts a run.

## Invariants

1. SQLite is the system of record. JSON, JSONL, and CSV are typed import/export formats.
2. Use registered task names. Import declarative `TaskSpec` JSON only when a file is explicitly supplied; never load Python task plugins.
3. Require `claims_schema.type: object` and `additionalProperties: false`.
4. Treat model output as untrusted until candidate-output, claims-schema, deterministic quote normalization, and canonical `ModelOutput` checks pass.
5. The model may omit quote offsets only when its exact quote text occurs once in the named slice. Repeated text requires explicit offsets.
6. A route name containing `free` proves nothing. Zero-price runs use only `price_observed_zero` routes. Local endpoints (`localhost`/`127.0.0.1`) default to zero cost; external OpenAI-compatible endpoints require explicit pricing or are treated as unknown cost.
7. Every harness runs tool-less JSON prompts through its own normally installed CLI. Use task-local lockdown where the harness documents one (OpenCode deny-all config with no task MCP servers and sharing disabled; Cursor `--sandbox enabled`); otherwise prompt-level JSON-only. Approval-bypass flags never appear, and continue/resume commands are never executed: the engine fresh-runs every prompt.
8. For MCP, set one absolute `--workspace-root`. Keep its SQLite database and every file path below that root.
9. Read exported packets through `read_packet`; it checks the embedded TaskSpec digest and revalidates every record's claims. Do not bypass `harness_fleet_v2` validation.
10. Model selection uses Bayesian-smoothed historical scoring. Every intermediate attempt failure, schema error, ungrounded quote, rate limit, and latency is recorded immutably in `inference_attempts`.
11. Mechanical discovery enforces a 70% source-capture floor by default. Its report includes per-backend hit/capture counts, and fetched records retain the backend, query, and originating URL.

## Workflow

1. Run `harness-fleet doctor --json`, then inspect `harness-fleet routes --json`. Refresh only when requested or needed; refresh contacts providers and appends route evidence.
2. For a zero-key proof, run `harness-fleet quickstart --demo --run-id demo --json` (writes `runs/demo/clean_packet.json` + `.csv` via `demo/fake`).
3. For local or private models (Ollama, vLLM, LM Studio), add the route with `harness-fleet routes add <id> --provider <provider> --free`.
4. Create or select a task (`--preset score|filter|classify|extract|triage|summarize` or `--from-example labels.csv`), then run `harness-fleet validate TASK --input FILE [--only-ids FILE] --json` before inference. Long documents are sliding-window sliced (lossless overlapping windows, sentence-snapped, `partial:true`) with exact offsets; `validate` warns when truncation occurs.
5. Run one batch with `harness-fleet test TASK --input FILE --json`.
6. Optionally benchmark candidate routes with `harness-fleet eval TASK --input FILE --concurrency 4 --json` to update Bayesian ranking priors (now parallelized per-sample/per-route).
7. Start the bounded campaign with an explicit run ID, session count, attempt ceiling, and optional policy (`--free-only` for verified zero-cost, `--zdr`, `--provider`, `--exclude-provider`, `--max-request-cost`, `--only-ids` for multi-stage compounding filters).
8. Monitor progress in real time with `harness-fleet status RUN_ID --watch` or machine-readable `harness-fleet status RUN_ID --json` (stream-friendly).
9. On interruption, inspect the stored run and call `harness-fleet resume RUN_ID`; do not reconstruct or restart its batches from input files.
10. Export and validate the packet with `harness-fleet export RUN_ID [--format json|csv|jsonl] [--sort-by CLAIM] [--desc] [--top N] [--rank] [--filter CLAIMFILTER_JSON]`. Use `harness-fleet db backup <path>` for safe SQLite copies. Report database path, run ID, packet path, verified/failed counts, attempts, and route/cost evidence.

Never call a worker session an independent coding-agent session. `--sessions` is bounded batch concurrency inside one harness-fleet campaign.

## Related skills

- `account-fleet` — turn an ICP into scored target accounts (`--preset account-research`).
- `partner-fleet` — turn ecosystem requirements into scored implementation partners (`--preset partner-research`).

Setup installs these next to this skill in `.agents/skills/`.

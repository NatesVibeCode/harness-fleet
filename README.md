# harness-fleet

[![CI](https://github.com/NatesVibeCode/harness-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/NatesVibeCode/harness-fleet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

> **Run repeatable, evidence-grounded LLM batch jobs over free and local models. Every claim is typed, and every quote is checked against the source at exact character offsets. Scores and interpretations still need human review.**

Harness Fleet turns a pile of text into answers you can check. You describe the job — *score these accounts*, *pull the pricing out of these pages*, *flag the ones that matter* — and it returns a typed result for every record with the exact sentence it relied on. It runs on free or local AI models by default, keeps all state in a local file, and ships the assistant playbooks for every fleet product, so one install gives your AI client the account, partner, and career skills too.

*One install, one CLI: `harness-fleet`. The account, career and partner products are **lanes** — configuration over the same engine — and every skill they need ships here. There is no second distribution to install.*

**New here?** [One-click install](#one-click-install) → [60-second demo](#quickstart--60-second-demo-no-api-keys) → [which lane do I want?](#which-lane-do-i-want)

## Start here (no coding needed)

1. **Install.** macOS/Linux: `./install.sh` · Windows: `powershell -ExecutionPolicy Bypass -File install.ps1`
   It makes its own private Python environment, sets up a workspace, and connects Claude Desktop or Cursor for you.

   Prefer a terminal one-liner to the installer? This puts the CLI on your PATH without cloning anything:
   ```bash
   uv tool install "git+https://github.com/NatesVibeCode/harness-fleet"
   # or, without uv:  python3 -m pip install "git+https://github.com/NatesVibeCode/harness-fleet"
   ```
2. **Watch it work.** The installer finishes by scoring a set of example records with a built-in fake model — no accounts, no API keys, no cost — and tells you where the results landed.
3. **Get free model access** — about two minutes, no credit card needed: **[FREE-ACCESS.md](FREE-ACCESS.md)**. Until one of those is connected there is nothing for a real run to use.
4. **Ask your assistant.** Restart Claude Desktop (or Cursor) and describe the job in plain words, e.g. *"score these 200 accounts and show me the strongest 25 with quotes."* The bundled skill picks the right commands.

Rather click than type? `harness-fleet studio` opens a local page for choosing which AI tools and models your runs are allowed to use.

## Which lane do I want?

One engine — typed claims, SQLite checkpoints, character-exact quote verification — and one command per job. A **lane** is the whole of a product's specificity: what it looks for, which sources it asks, which checklist it scores with, what bar it demands. It is a JSON file, so tuning a lane is editing a file rather than installing something else.

| If you want to… | Run | Skill that drives it |
| --- | --- | --- |
| Score, classify, extract, or triage **your own** text at volume | `harness-fleet run` | `harness-fleet` |
| Turn an ICP into **scored target accounts** | `harness-fleet research --lane account` | `account-fleet` |
| Find and rank **employers hiring for a role** | `harness-fleet research --lane career` | `career-fleet` |
| Find **implementation partners and SIs** | `harness-fleet research --lane partner` | `partner-fleet` |

`harness-fleet research` runs the whole pipeline in one command — discover, bundle evidence per entity, score, export — and `harness-fleet lane report <run_id> --lane <name>` then measures what that run actually produced: yield per search source, how many records clear the lane's evidence bar, which claims a quote carries and which were refused, a re-fetched truth sample, and cost. Lanes ship in the package; drop `<workspace>/lanes/<name>.json` to override one or add your own. See [Lanes](#lanes) below.

Every skill is installed into your workspace by `harness-fleet setup` — see [Assistant skills](#assistant-skills-what-installs-where).

## Contents

- [Which lane do I want?](#which-lane-do-i-want) · [Lanes](#lanes)
- [One-click install](#one-click-install) · [Quickstart — 60-second demo](#quickstart--60-second-demo-no-api-keys)
- [30-Second Example: raw accounts in → scored, grounded CSV out](#30-second-example-raw-accounts-in--scored-grounded-csv-out)
- [Why you can trust the output](#why-you-can-trust-the-output) · [Core capabilities](#core-capabilities) · [Source quality](#source-quality)
- [SQLite control plane](#sqlite-control-plane) · [Commands](#commands) · [Assistant skills](#assistant-skills-what-installs-where)
- [MCP server](#mcp-server) · [Harness studio (local UI)](#harness-studio-local-ui)
- [Migrating from free-fleet](#migrating-from-free-fleet) · [Verification & testing](#verification--testing)

## Migrating from free-fleet

Version 0.3.0 renames the `free-fleet` distribution to `harness-fleet` (the old `bulk-lanes` name is gone). Back up your database first, then:

```bash
free-fleet db backup free-fleet.db.bak  # back up with the OLD CLI first (SQLite backup API, WAL-safe)
mv free-fleet.db harness-fleet.db
harness-fleet setup --workspace-root .   # re-installs the skill
harness-fleet mcp install                # re-installs client configs
```

Old packets (`free_fleet_v2` / `bulk_lanes_v2`) no longer read; re-export them from SQLite before upgrading. The `FREE_FLEET_DB` / `BULK_LANES_DB` / `ACCOUNT_FLEET_DB` variables are replaced by the single `HARNESS_FLEET_DB`. There is no downgrade path — restore your backup to go back.

Python 3.10+ is required. This is a command-line tool with an optional AI-assistant integration. It scores source text you supply, and it also goes and gets that text: `harness-fleet research --lane <name>` searches the open web, fetches the pages, bundles the evidence per entity and scores it in one command. The bundled skill for each lane guides a connected assistant through the same pipeline.

Install and try the offline demo below before running a real list. Real research requires a configured model provider and your own qualification criteria.

---

## 30-Second Example: Raw Accounts In → Scored, Grounded CSV Out

Suppose you have a list of target companies in `accounts.csv`:

```csv
company,careers_text
stripe.com,"We are hiring a Staff Engineer to lead migration off legacy v1 billing pipeline to Kafka..."
hyper_ai,"Looking for Senior Backend Engineer hitting latency limits at 50k QPS on Postgres cluster..."
pinecone.io,"Hiring Infrastructure Engineer scaling vector search across multi-tenant clusters..."
```

### 1. Initialize the account research preset and run

```bash
# Initialize the typed account-research preset (evidence checklist, identified_gap; score/fit_tier derived)
harness-fleet init research-demo --preset account-research

# Process the accounts through free model routes (zero API spend)
harness-fleet run research-demo --input accounts.csv --id-column company --text-column careers_text --run-id campaign-01
```

### 2. Export the top 25 ranked accounts

```bash
harness-fleet export campaign-01 --format csv --sort-by score --desc --top 25 --rank --output ranked_accounts.csv
```

### 3. Output (`ranked_accounts.csv`)

```csv
rank,item_id,score,identified_gap,fit_tier,primary_quote_text
1,stripe.com,92,"Legacy billing migration",tier_1,"lead migration off legacy v1 billing pipeline to Kafka"
```

Illustrative values only; real exports also include source URLs, digests, and quote details. Set your ICP and scoring rubric in the task's `TaskSpec` — a preset plus a task JSON file, edited by hand or by your AI client using the bundled skill. An exact source quote proves the text exists, not that a company will buy your product.

**Or let it fetch the text for you.** If you do not already have the sources, the lane pipelines search the open web, fetch the pages, bundle what each company's own pages say, and score it — one command, same verified output:

```bash
harness-fleet research --lane account --max-results 12 --top 25   # dossiers -> accounts_ranked.csv
harness-fleet lane report <run_id> --lane account                 # what that run actually produced
```

---

## Why You Can Trust the Output

Every output row is gated through deterministic checks *before* it is committed to SQLite. If any check fails, the batch rotates to the next route — nothing unverified is exported.

1. **Deterministic Quote Verification**: Cited quotes are checked against the raw source text at character-level precision and resolved to canonical `[start, end]` offsets. Sections with evidence terms also expose numbered candidate spans the worker cites by id instead of free-searching; verification recomputes the same span table, so offsets are code-owned. Fabricated or altered quotes fail grounding and trigger immediate route rotation. *(This proves all cited quotes are verbatim source substrings; whether a claim is truly entailed by its quote remains model-generated.)*
2. **Closed JSON Schemas**: Outputs adhere strictly to closed JSON Schemas defined in `TaskSpec`. Models cannot add fields, emit markdown, or drift out of schema.
3. **Derived Scores and Tiers, Not Double Judgment**: Scoring tasks collect an evidence-bound `checklist` of true/false answers, and the pipeline computes `score` (summed points, capped at 100), `fit_tier` (85+ → `tier_1`, 70+ → `tier_2`, 50+ → `tier_3`, else `unfit`), and `passed`. A mismatched derived value fails validation and rotates routes.
4. **Weighted, Time-Decayed Evidence**: Every true answer needs a supporting quote tagged with `supports`, and each answer scores its points scaled by source weight (configurable per-domain rules, longest match wins) and recency decay (per-item half-lives — hiring signals stale in weeks, company fundamentals in months). Untagged truth scores zero, so weak evidence can only lower a score, never inflate one.
5. **Intelligent Route Scoring**: Bayesian-smoothed scoring by verification rate, grounding accuracy, malformed-JSON rate, and latency — not round-robin. Best routes are tried first.
6. **Non-Destructive Rate-Limit Handling**: On `429` or `5xx`, the route is cooled down and the batch is retried immediately on the next lane with **0 attempt burn**.
7. **Rescore Lineage, Not Overwrites**: Fresh evidence arrives as new runs linked by `parent_run_id`; every verified record lands in `score_history`, and `harness-fleet history ENTITY` shows the score trajectory across rounds. Old scores are never rewritten — a stale 40 stays visible next to the new 85 and the evidence that moved it.
8. **Zero-Price Circuit Breaker & Spend Ceilings**: For zero-price runs, pricing is observed from provider receipts; a non-zero charge trips the breaker and disables the route. For paid runs, `--max-request-cost` enforces per-request caps. Cost ceilings fail closed on undeclared pricing.

> **Live proof:** `harness-fleet status <run_id> --watch` streams batch progress and per-route `Verified / Rate limits / Latency`. Fabricated quotes show up instantly as `grounding_failed` and the next lane is tried.

---

## One-click install

From a fresh checkout, one command sets up everything — no virtual environments or pip to
worry about.

macOS / Linux:

```bash
./install.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

That one command:

- Creates a private Python 3.10+ environment (`.venv`) and installs `harness-fleet` into it.
- Sets up a workspace at `~/harness-fleet-workspace` and runs the offline demo there.
- Registers the `harness-fleet` MCP tools with Claude Desktop (or Cursor).
- If `OPENROUTER_API_KEY` is set in your shell, passes it into that client config too (values are never printed), so OpenRouter routes work from the desktop app.

Pass a different workspace folder if you want one, e.g. `./install.sh ~/my-harness-workspace`.
Restart Claude Desktop (or Cursor) after it finishes.

---

## Quickstart — 60-Second Demo (No API Keys)

```bash
git clone https://github.com/NatesVibeCode/harness-fleet.git
cd harness-fleet
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

# Run in your own workspace; output files are written in the current directory.
mkdir my-workspace
cd my-workspace
harness-fleet setup
harness-fleet quickstart --demo --run-id demo-01

# Outputs:
#   runs/demo-01/clean_packet.json   (self-validating packet)
#   runs/demo-01/clean_packet.csv    (flat CSV)
```

On Windows PowerShell, replace the two virtual-environment commands with `py -m venv .venv` and `.venv\Scripts\Activate.ps1`. If activation is restricted, run `..\.venv\Scripts\harness-fleet.exe` directly from `my-workspace`.

The demo uses synthetic scores for ten bundled sample accounts and makes no model API calls. It verifies installation and export, not research quality. Demo routes are excluded from real campaigns unless explicitly selected.

### Real Workspace

```bash
mkdir my-workspace && cd my-workspace
harness-fleet setup --workspace-root . --refresh-routes
```

For a real run, configure one of the seven CLI harnesses (`opencode`, `claude`, `codex`, `cursor`, `grok`, `muse`, `antigravity`) with your own provider access, set `OPENROUTER_API_KEY`, or register a running local model, for example `harness-fleet routes add ollama/your-installed-model --provider ollama --free`. Refreshing routes alone does not authenticate you. `harness-fleet doctor` checks configuration; `harness-fleet test research-demo --input accounts.csv --id-column company --text-column careers_text --provider ollama` tests a real batch before a large campaign.

Each named provider uses its own settings: `OLLAMA_BASE_URL`, `LMSTUDIO_BASE_URL`, `GROQ_API_KEY`, and so on. `OPENAI_COMPATIBLE_BASE_URL` and `OPENAI_COMPATIBLE_API_KEY` configure only `--provider openai_compatible`. Environment variables must be available to the process running the CLI or MCP server; `.env` files are not loaded automatically.

`setup` installs both the account-fleet research skill and the harness-fleet execution skill in the workspace's `.agents/skills` directory. Keep the virtual environment in place when using the generated MCP configuration. Install account-fleet and harness-fleet in separate environments: they share the `harness_fleet` Python package and entry-point names, so one fleet per environment.

### Presets

Create typed tasks instantly with built-in presets:

```bash
harness-fleet init score-demo --preset score             # Evidence checklist + pipeline-derived 0-100 score
harness-fleet init filter-demo --preset filter           # Boolean qualification pass/fail gate
harness-fleet init account-demo --preset account-research # Evidence checklist + derived ICP score/tier + gap extraction
harness-fleet init triage-demo --preset triage           # Priority (high/medium/low) + reason
harness-fleet init classify-demo --preset classify       # Categorical labels + summary
harness-fleet init extract-demo --preset extract         # Named entities + summary
harness-fleet init summarize-demo --preset summarize     # Supported fact summaries
```

Validate and test before launching large runs:

```bash
# Validate task spec and input without making any API calls
harness-fleet validate score-demo --input input.jsonl

# Test a single real batch
harness-fleet test score-demo --input input.jsonl
```

---

## Examples

Runnable starting points live in [`examples/`](examples/) — each folder has a `README.md`, a typed `task.json`, and sample input you can feed straight to `run`:

| Example | Preset | Sample input |
| --- | --- | --- |
| [`examples/account_research/`](examples/account_research/) | `account-research` | `sample_accounts.csv` |
| [`examples/partner_research/`](examples/partner_research/) | `partner-research` | `sample_partners.csv`, `sample_partners_multisource.csv` |
| [`examples/saas_intelligence/`](examples/saas_intelligence/) | `score` | `sample_data.jsonl` |
| [`examples/security_cve_triage/`](examples/security_cve_triage/) | `triage` | `sample_data.jsonl` |

```bash
harness-fleet init my-task --preset partner-research --db ./harness-fleet.db
harness-fleet validate my-task --db ./harness-fleet.db \
  --input examples/partner_research/sample_partners.csv \
  --id-column domain --text-column research --json
```

## Core Capabilities

### 1. CSV In / Scored, Ranked CSV Out
Directly process tabular data and export sorted, ranked deliverables with exact source quotes:

```bash
# Run on CSV specifying ID and text columns (or let harness-fleet auto-detect them)
harness-fleet run score-demo --input accounts.csv --run-id accts-01

# Export ranked deliverable: sorted by score descending, top 25, with 1-indexed rank column
harness-fleet export accts-01 --format csv --sort-by score --desc --top 25 --rank --output ranked_target_accounts.csv
```

### 2. The Compounding Filter (Chaining Layers)
Run multi-stage funnel filtering without running monolithic prompts or wasting model compute:

```bash
# Layer 1: Filter down to survivors
harness-fleet run l1-task --input 1000_candidates.csv --run-id l1
harness-fleet export l1 --format csv --filter '{"all": [{"field": "passed", "value": true}]}' --output l1_survivors.csv

# Layer 2: Only run on survivor IDs from Layer 1
harness-fleet run l2-task --input tech_docs.csv --only-ids l1_survivors.csv --run-id l2
harness-fleet export l2 --format csv --filter '{"all": [{"field": "passed", "value": true}]}' --output l2_survivors.csv

# Final Layer: Score survivors and rank top candidates
harness-fleet run l3-task --input gap_analysis.csv --only-ids l2_survivors.csv --run-id l3
harness-fleet export l3 --format csv --sort-by score --desc --top 25 --rank --output ranked_deliverable.csv
```

Filters compose deterministically at export — a ClaimFilter document with `all`
clauses (AND), `any` branches (OR), and ops `==, !=, >=, <=, >, <, in, not_in`:

```bash
harness-fleet export l3 --format csv \
  --filter '{"all": [{"field": "score", "op": ">=", "value": 70}, {"field": "passed", "value": true}]}' \
  --output qualified.csv
```

Discovery pre-filters mechanically too:
`fetch --title-include engineer --title-exclude manager --exclude-stack mainframe --min-chars 200`.

### 2b. DAG Workflows (multi-stage funnels without CSV round-trips)
Each workflow is a DAG of typed nodes — `run` (one Engine campaign = one SQLite run),
`filter` (deterministic ID sets from a run snapshot), `export` (packet/CSV). Edges carry
IDs and run references in-process, so funnels keep full drill-through (offsets, digests)
at every hop instead of degrading through CSV files:

```json
{
  "name": "funnel",
  "nodes": [
    {"kind": "run", "id": "l1", "task": "filter-task", "input": "candidates.csv",
     "policy": {"allowed_routes": ["demo/fake"], "free_only": true}},
    {"kind": "filter", "id": "l1f", "from_run": "l1",
     "filter": {"all": [{"field": "passed", "value": true}]}, "top": 50},
    {"kind": "run", "id": "l2", "task": "score-task", "input": "docs.csv", "ids_from": ["l1f"]},
    {"kind": "export", "id": "out", "from_run": "l2", "format": "csv",
     "sort": {"field": "score"}, "top": 25, "rank": true}
  ]
}
```

```bash
harness-fleet dag --spec funnel.json --dry-run --json   # validate + print order
harness-fleet dag --spec funnel.json --dag-id campaign-01 --json
```

Node run IDs are deterministic (`<dag-id>-<node-id>`), so re-running resumes completed
`run` nodes from SQLite while `filter`/`export` re-execute. Lineage (spec digest, run IDs,
counts, artifacts) lands in `runs/<dag-id>/dag.json`. Cycles, unknown references, and
wrong-kind edges fail closed at parse time.

### 3. Live Run Monitoring
Track queue progress, worker concurrency, and route-level metrics in real time:

```bash
harness-fleet status <run_id> --watch
```

Output:
```
============================================================
Run: triage-01  |  Task: customer-triage  |  Status: RUNNING
Progress: [=========================>              ] 62.5% (650/1040)
============================================================
Batches:
  Pending:    15
  Leased:      4
  Done:       65
  Failed:      0

Route Performance:
  openrouter:qwen/qwen-2.5-72b-instruct:free
    Attempts: 45 | Verified: 44 | Rate limits: 1 | Latency: 1.2s
  openrouter:meta-llama/llama-3.3-70b-instruct:free
    Attempts: 24 | Verified: 23 | Rate limits: 0 | Latency: 1.8s
```

Fleet progress above is proven and durable (leases, attempts, receipts). Running lanes can additionally self-report presence with the shared advisory vocabulary (`started`, `milestone`, `blocked`, `done`) implemented by work-coordination: claimed, ephemeral, and never proof of completion — the complement to receipts, not a substitute.

### 3. Continuous Route Evaluation
Benchmark available routes against test datasets to determine which models excel at your specific task:

```bash
harness-fleet eval customer-triage --input test-samples.csv --id-column id --text-column comment
```

Output:
```
========================================================================================
Route Evaluation Benchmark
Task: customer-triage  |  Samples: 20
========================================================================================
Route                                      Success   Grounding   Score    Avg Latency
----------------------------------------------------------------------------------------
openrouter:qwen/qwen-2.5-72b-instruct:free   100.0%     100.0%    0.982          1.15s
openrouter:meta-llama/llama-3.3-70b-free      95.0%      90.0%    0.871          1.82s
opencode:llama3                               80.0%      85.0%    0.742          2.40s
```
Evaluation benchmarks automatically update route selection priors for subsequent runs.

### 4. Local Models & Generic OpenAI-Compatible Providers
Run bulk workloads completely locally with **Ollama**, **LM Studio**, **vLLM**, or fast cloud inference providers like **Groq** and **Cerebras**:

```bash
# Register your local or custom route in the catalog
harness-fleet routes add ollama/llama3.2:latest --provider ollama --free

# Or configure environment variables
export OPENAI_COMPATIBLE_BASE_URL="http://localhost:11434/v1"
export OPENAI_COMPATIBLE_API_KEY="ollama"
export OPENAI_COMPATIBLE_MODEL="llama3.2:latest"

# Run with local provider selection
harness-fleet run my-task --input data.csv --id-column id --text-column text --provider ollama
```

Endpoints on `localhost` or `127.0.0.1` are automatically marked free (`cost = 0.0`). For third-party cloud OpenAI-compatible endpoints, specify costs explicitly (`--input-cost` / `--output-cost`) or leave them as unknown-cost to prevent accidental misclassification.

### 5. Explicit Data & Privacy Policy
Enforce zero data retention (ZDR), prohibit provider data collection, limit request spend, and control upstream routing on a per-run basis:

```bash
harness-fleet run my-task \
  --input sensitive-data.jsonl \
  --zdr \
  --no-data-collection \
  --provider openrouter \
  --exclude-provider opencode \
  --openrouter-providers Anthropic,Together \
  --max-request-cost 0.05
```

You can pass `--openrouter-providers` as a comma-separated list or as repeatable `--openrouter-provider` flags.

---

## Source Quality

Mechanical `discover` and `fetch` runs enforce a default 70% source-capture
floor and report backend/query provenance for fetched records. Use `--json`
for the `source_quality` report; lower the floor with
`--min-source-coverage 0` only for an intentional sparse-source audit.

---

## Lanes

A lane is one JSON file — the whole of a product's specificity, and nothing of the
mechanism. Three ship in the package:

| Lane | Answers | Scores with | Bar |
| --- | --- | --- | --- |
| `account` | Which accounts are doing the work (and hiring for it) | `account-research` | the tier ladder, floor `tier_3` |
| `partner` | Which implementation partners can generate revenue with us | `partner-research` | the tier ladder, floor `tier_2` |
| `career` | Which employers are hiring for this kind of role | `career-research` | `delivery_hiring` |

```jsonc
{
  "name": "partner",
  "description": "Implementation partners and consultancies, from vendor-published stories",
  "seeds": ["snowflake"],              // anchors the lane starts from
  "queries": ["\"partner of the year\" (\"case study\" OR \"customer story\")"],
  "backends": ["ddgs", "hn"],          // search surfaces, or a channel you installed
  "channels": [],                      // <workspace>/sources/*.json|py you can also search
  "title_include": [], "title_exclude": [], "remote": false,   // deterministic filters
  "preset": "partner-research",        // the checklist it scores with
  "tier": "tier_1",                    // evidence floor, or null to gate on require_kinds
  "require_kinds": [],                 // evidence kinds every scored record must carry
  "top": 25, "min_score": null,        // presentation
  "revision": 1
}
```

**How a lane returns hundreds.** Two stages, and the first one is not search.
Lanes that declare `stories` enumerate the customer stories each vendor
publishes about its customers — Snowflake, Databricks, Elastic, Datadog,
MongoDB — and take one candidate per story: a vendor's own index holds hundreds,
and every story is prose the vendor published about a named company. The second
stage then *goes and looks*: for each entity still short of the bar, it visits
that entity's own surfaces for exactly the evidence it is missing — its sitemap
first (which answered for every domain tested and names the real case-study
URLs), then its case studies, services, partners and blog, its hiring board
through its JSON API, the vendor stories that name it, and the communities
discussing it. `--no-enrich` stops after the first stage; `--stories N` and
`--enrich-pages N` set the volume.

```bash
harness-fleet research --lane partner                 # hundreds of candidates, then the walk
harness-fleet research --lane partner --no-enrich     # search and story indexes only
harness-fleet lane report <run_id>                    # what that run actually produced
```

- **Unknown keys are refused.** A lane cannot smuggle in a mechanism override; if it
  needs a source the engine cannot speak to, that is a channel, and if it needs a claim
  the contracts do not know, that is a contract change that applies to every lane.
- **Tiers nest.** `tier_1` requires `delivery_proof`, `independent_validation` and
  `stack_delivery`; `tier_2` requires the first two; `tier_3` requires `stack_delivery`.
  A lane's `tier` is the *floor*: a record clearing any tier at or above it has met the
  bar. A lane whose rows are pages rather than companies sets `"tier": null` and names
  the evidence it does demand in `require_kinds`.
- **Find them with `harness-fleet lane list`** (add `--json` for scripts), which prints each lane's preset, evidence bar and where it came from.
- **The funnel shrinks the world before anything is spent.** Every captured candidate
  runs the cheapest gates first — what kind of company it is, how big, where, serving
  whom — and stops at the first failure. A product vendor, a six-person shop or a firm
  in the wrong country is gone for the cost of the search result that named it, before
  a page fetch or a model call. The run reports where the world shrank, by gate and by
  name; `--no-funnel` keeps everything:

  ```
  5 candidates -> 2 needing retrieval
  (1 at kind (e.g. vendor.io), 1 at size, 1 at location, 1 unresolved on kind, 2 unresolved on vertical)
  ```

  Three rules make it honest rather than merely fast. A **snippet may eliminate; only a
  fetched page may qualify** — a search result is somebody else's summary, so a
  qualifier seen there leaves the gate unknown and the candidate alive, having earned a
  page fetch rather than a pass. **Unknown is neither a pass nor a failure**, and is
  recorded with what would resolve it. And **the gates match substance, not wording**:
  one editable synonym vocabulary means a firm saying "advisory" matches a profile
  saying "consultancy", "banking" matches "fintech", and "London" matches "United
  Kingdom".
- **Firmographics lead the profile.** `ideal_partner_profile.json` carries the size
  range, territory and verticals the funnel runs on. The run gates on the profile and
  the lane together — whichever bound is stricter wins, so a lane's floor cannot erase
  a stricter one from the profile. Name a profile explicitly with `--profile PATH`; with
  none authored, the lane's own gates still apply, so a first run with no setup
  eliminates the obviously wrong companies.

  ```json
  {
    "target_ecosystem": "Snowflake",
    "partner_size_min": 50,
    "partner_size_max": 400,
    "target_territories": ["United Kingdom"],
    "target_industries": ["fintech"]
  }
  ```
- **Each lane states its own ladder**: which rung reads a search result, which rung
  spends a page visit, and what a candidate must satisfy to earn the next. The shipped
  lanes read the result first, then the firm's own `about`/`services`/`careers` pages to
  confirm the cheap gates, then its `case_studies`/`partners` pages for the vertical —
  the one gate no snippet can settle.
- **Workspace overrides shipped.** A file at `<workspace>/lanes/<name>.json` replaces
  the packaged lane of the same name, and the run records which one it used.
  `harness-fleet lane report <run_id> --lane <name>` measures the result.

---

## SQLite Control Plane

`harness-fleet` uses SQLite in WAL mode with `BEGIN IMMEDIATE` atomic leases. If a worker crashes or a laptop closes, the run can be resumed seamlessly:

```bash
harness-fleet resume <run_id>
```

Free routes are used by default. A paid route approved in an earlier session must be requested again with `--route <route-id>`.

- **Resumable**: Batches are committed upon verification. Completed work is never repeated.
- **Fault-Tolerant**: Stale worker leases are automatically recovered after timeout.
- **Concurrent**: Multiple worker processes can safely lease batches simultaneously without collisions.
- **Auditable**: Every attempt, model receipt, cost observation, and verification failure is recorded immutably in `inference_attempts`.

---

## Commands

| Command | Purpose |
|---|---|
| `quickstart` | One-command offline demo (no keys) that writes a verified packet + CSV |
| `setup` | Bootstrap a portable workspace with bundled skills and SQLite database |
| `doctor` | Check SQLite, installed CLIs, provider authentication, and available routes |
| `routes` | List or refresh discovered model routes (`--refresh`) |
| `routes add` | Register an explicit custom or local model route (`--free`, `--input-cost`) |
| `cooldowns` | Inspect active rate-limit route cooldowns or clear them (`--clear`, `--route`) |
| `tasks` | List registered task definitions |
| `init` | Create a typed task from a preset (`score`, `filter`, `account-research`, `career-research`, `triage`, `classify`, `extract`, `summarize`) |
| `profile` | Manage the typed Ideal Company Profile (`--init`, `--path`, `--force`) that research presets score against |
| `init --from-example` | Infer a draft `claims_schema` from a labeled CSV (`--from-example labels.csv --label-column label`) |
| `validate` | Check task schema and input formatting without inference (`--only-ids`) |
| `test` | Run one real batch through candidate models |
| `run` | Create and execute a SQLite-backed resumable run (`--only-ids` for compounding filter) |
| `resume` | Resume an unfinished run from its SQLite queue |
| `rescore` | Re-score an existing run's records against a new input round (`--input`, `--run-id`) |
| `dag` | Run a multi-stage workflow from a DAG spec (`--spec`, `--dry-run`) instead of chaining CSV rounds |
| `status` | Show real-time progress, attempts, and route stats (`--watch`, `--json`) |
| `eval` | Benchmark routes on sample inputs and update route ranking priors (`--concurrency`) |
| `calibrate` | Fit scoring points/weights/half-lives against labeled samples (`--apply` to register the revision) |
| `sessions` | Inspect recorded worker sessions and audit logs |
| `history` | Score trajectory for one entity across runs (`history ENTITY`) |
| `export` | Export a validated packet (`--format json\|csv\|jsonl`, `--sort-by`, `--desc`, `--top`, `--rank`, `--filter`) |
| `db backup` | SQLite backup to file (safe while running) |
| `schema` | Print admitted JSON Schemas or database contracts |
| `mcp install` | One-command Claude/Cursor setup (auto-wires `claude_desktop_config.json` / `mcp.json`; `--env NAME` copies a shell variable, e.g. `OPENROUTER_API_KEY`, into the client config) |
| `serve` | Run the Model Context Protocol (MCP) server over stdio |
| `board` | Serve the read-only results board for a run: every attribute, the checklist behind each score, verbatim quotes, and provenance (`--run-id`, `--port`, `--open`, `--json`) |
| `settings` | Print, or `--clear`, the harness/model selection the studio saved (`run --from-studio` uses it) |
| `studio` | Serve the localhost settings companion (pick harnesses and models; saves the selection to SQLite) |
| `research` | One command from a question to a ranked deliverable: discover → bundle per entity → score → export (`--lane`, `--query`, `--backend`, `--max-results`, `--min-chars`, `--top`) |
| `lane list` | The lanes this install can run: preset, evidence bar, query count, and whether the lane came from the package or your workspace |
| `--stories N` | Vendor-published customer stories to gather per vendor as candidate entities (a lane declares its own default) |
| `--enrich-pages N` · `--no-enrich` | Pages kept per surface when walking an entity's own site, or skip the walk entirely |
| `lane report` | Measure a finished run against its lane: yield per source, records meeting the evidence bar, claims carried vs refused with reasons, a re-fetched truth sample, cost (`RUN_ID`, `--lane`, `--sample`, `--freeze`) |
| `sources` | The learned source registry: `list`, `propose`, `promote`, `demote`, `channels` — which hosts count as evidence, and why |
| `discover` | Broad web search (`ddgs`, self-hosted SearXNG, HN Algolia, YC, Reddit, Stack Exchange, Discourse, Lobsters, Lemmy, Dev.to) to an accounts file |
| `fetch` | Fetch URLs, sitemaps, site crawls, ATS boards (Greenhouse/Ashby/Lever), YC profiles, HN/Reddit threads, or Q&A forums to an accounts file |
| `partners enrich` | Deepen one partner you already have: its own site, hiring board, vendor registry and independent mentions, bundled into a single dossier row (`DOMAIN`, `--max-pages`, `--no-fetch`, `--output`). Partner candidates come from `research --lane partner` |

Pass `--json` to any command for machine-readable JSON output. `--free-only` is the explicit zero-cost filter (replaces implicit `max-cost=0` sentinel). Long documents are warned when truncated (`partial` slices).


---

## Assistant skills (what installs where)

Skills are the playbooks your AI client reads to drive this CLI. `harness-fleet setup` installs every skill this distribution bundles into `<workspace>/.agents/skills/`, so a connected assistant finds them without you copying anything:

| Skill | Installed as | Drives |
| --- | --- | --- |
| `harness-fleet` | `.agents/skills/harness-fleet/` | This CLI: task contracts, runs, export, MCP, troubleshooting |
| `account-fleet` | `.agents/skills/account-fleet/` | `--preset account-research`: an ICP in, scored target accounts out |
| `partner-fleet` | `.agents/skills/partner-fleet/` | `--preset partner-research`: ecosystem requirements in, scored implementation partners out |
| `career-fleet` | `.agents/skills/career-fleet/` | `research --lane career`: which employers are hiring for a role, remote and filtered |

Every skill is plain markdown with a `SKILL.md` plus a `references/` folder (`operations.md`, `task-contracts.md`, discovery playbooks, scoring-rubric guides, MCP recipes). Read them straight from this repo under `skills/`, or preview what setup would install:

```bash
harness-fleet setup --workspace-root "$PWD" --dry-run --json
```

## MCP Server

`harness-fleet` includes a Model Context Protocol (MCP) server for integration into Cursor, Claude Desktop, Antigravity, and other agent environments:

**One-command install (recommended for GTM folks):**
```bash
harness-fleet mcp install --workspace-root "$PWD"  # auto-detects Claude/Cursor, writes mcpServers entry
# Also hand the desktop app a provider key (repeatable or comma-separated)
harness-fleet mcp install --env OPENROUTER_API_KEY
# Preview first
harness-fleet mcp install --dry-run --json
harness-fleet doctor --workspace-root "$PWD" --json  # verify
# Restart Claude/Cursor to load
```

Desktop apps do not inherit your shell environment, so keys such as `OPENROUTER_API_KEY` (or `GROQ_API_KEY`, `OLLAMA_BASE_URL`, …) must be passed explicitly with `--env NAME`; the CLI writes the value into the client config and only ever prints the variable name, never the value. Unset names are skipped with a warning.

Manual entry:
```json
{
  "mcpServers": {
    "harness-fleet": {
      "command": "harness-fleet",
      "args": ["serve", "--workspace-root", "/absolute/path/to/workspace"]
    }
  }
}
```

---

## Harness Studio (Local UI)

`harness-fleet studio` serves a localhost-only settings companion over the same SQLite
control plane (default `http://127.0.0.1:8080`; honors `--workspace-root`, `--port`, and
`--db`). For a disposable preview workspace, run `scripts/studio_preview.sh`. The page is
read from disk per request, so UI edits only need a browser refresh; Python changes need a
restart.

It is deliberately small: pick harnesses, then pick from *their* models. Runs happen from
the CLI or MCP, which read the same catalogue.

- **Harnesses start off.** Click to include. Each card shows whether its binary is on
  `PATH` and that harness's own login command (`opencode providers`, `codex login`,
  `cursor-agent login`, …) or, for API providers, the environment variable it needs. The
  studio never handles credentials.
- **Models are only listed for what you selected.** `Free models` uses every verified
  `price_observed_zero` route of the selected harnesses; `Specific models` lists their
  models so you choose. Unpriced routes are never picked automatically.

The scoring contract, tasks, and runs are edited through the CLI or an agent — their
endpoints (`/api/tasks`, `/api/tasks/<name>/scoring`, `/api/runs`, `/api/routes/refresh`)
remain available to scripts. The server refuses cross-origin requests so a page you visit
cannot drive it.

```bash
scripts/studio_preview.sh                          # disposable workspace on :8099
harness-fleet studio --workspace-root "$PWD" --port 8080
```

Agents verifying the UI with screenshots: see [AGENTS.md](AGENTS.md) — use playwright,
never raw Chrome, and give pytest runs explicit timeouts.

---

## Verification & Testing

Run the test suite:

```bash
pytest -q
```

All core components (Bayesian route scoring, SQLite control plane, rate limit cooldowns, OpenAI-compatible provider, CSV IO, real-time status monitoring, and route evals) are covered by automated unit and integration tests.

---

## License

MIT. See [LICENSE](LICENSE).

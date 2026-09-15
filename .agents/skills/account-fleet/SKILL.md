---
name: account-fleet
description: Turn conversational ICPs into scored target account deliverables using free models, public job posts (Ashby, Greenhouse), and character-exact quote verification. Use when discovering, researching, scoring, and ranking target accounts with traceable source evidence.
---

# Account Fleet: Target Account Research & Scoring Skill

## This is the `account` lane

The workflow is the shared one — read the `harness-fleet` skill for the
procedure (discover, bundle per entity, score, read the evidence with its
confidence, export, then `lane report` to see what it produced). This file only
carries what is specific to the `account` lane:

- **Lane config:** `lanes/account.json` — seeds, queries, sources, filters.
- **Preset:** `account-research` · **Bar:** `tier_2` · **Output:** top 25.
- **Looks for:** companies doing the work, and companies hiring for it.

Run it with `harness-fleet research --lane account`, or over MCP by passing
`lane: "account"` to the pipeline tools.

Turn a founder or seller's conversational Ideal Customer Profile (ICP) into a ranked pipeline of qualified target accounts, where every single qualification is backed by a verbatim quote from an active job post or engineering document.

The account-side profile is a typed `IdealCompanyProfile`. Keep its editable JSON
in `ideal_company_profile.json`, and persist the active content-addressed revision
in SQLite with `account-fleet profile`. Task revisions remain the execution rubric;
each run records the selected profile revision when one is available.

---

## The Outbound Account Execution Pipeline

```
0. Context Inference & Gap Interview ──> 1. ICP Deconstruction ──> 2. Discovery Queries ──> 3. Task Spec & Rubric
                                                                                                  │
                                                                                                  ▼
5. Ranked CSV Export                 <── 4. Python Substring Gate <── Free Model Fleet
```

---

## Phase 0: Autonomous Context Inference & Targeted ICP Calibration

Never jump into blind web searches with vague descriptions, and **never interrogate the user with questions about facts you can discover autonomously**.

The agent harness must follow a two-step context protocol:

### Step 1: Autonomous Discovery (History, Memory, & Repo First)
Before asking the operator anything, inspect:
1. **Provided Context**: Use relevant prior context available in the current conversation or explicitly provided by the user. Do not assume another computer has a particular history folder or search unrelated local conversations.
2. **Workspace Codebase**: Read `README.md`, package descriptors (`pyproject.toml`, `package.json`), integration code (`connectors/`, `providers/`), and git commits (`git log -n 15`).
3. **Resolve the 6 Target Signals**:
   - *Architectural Layer*: Where does the product sit? (e.g. database proxy, eBPF agent, CI/CD runner).
   - *Required Tech Stack*: What must the prospect run? (e.g. PostgreSQL, Kafka, AWS, Kubernetes).
   - *Negative Exclusions*: Incompatible architectures (e.g. pure NoSQL/Firebase, serverless-only).
   - *Target Personas*: Who feels the pain? (e.g. Staff Infra, DBRE, Head of Platform).
   - *Breaking Point Catalyst*: What scale or failure forces a purchase? (e.g. 50k QPS latency limits, $10k/mo Redis bills).
   - *Anchor Logos*: 2–3 dream accounts or happy existing customers.

### Step 2: Gap Analysis & Targeted Interview
- **The Invariant**: NEVER ask the operator about signals already confirmed from repo or session context.
- Summarize the inferred company profile to the user with clear evidence.
- Ask **only** for signals that are genuinely missing or ambiguous (typically the breaking point catalyst or 2–3 anchor logos).

See [references/icp-interview.md](references/icp-interview.md) for detailed thought processes, signal definitions, and interview templates.

After the focused interview, save the confirmed profile before discovery:

```bash
account-fleet profile --init
# edit ideal_company_profile.json
account-fleet profile
```

---

## Phase 1: Deconstruct the ICP into 3 Technical Signals

Never evaluate an account on vague firmographics alone. When a user describes their product or target customer, deconstruct it into three mandatory technical signals:

1. **Target Architecture / Tech Stack**:
   - What infrastructure or frameworks must the prospect run? (e.g. `Kafka`, `ClickHouse`, `Postgres`, `Kubernetes`, `Snowflake`, `PyTorch`).
2. **Active Bottleneck / Technical Pain**:
   - What exact problem proves they have urgent need?
   - *Migration*: Moving off legacy v1 systems or migrating between databases.
   - *Scale / Latency*: Hitting QPS ceilings, memory limits, or query timeouts.
   - *Cost / Overhead*: Spiking cloud bills, cluster maintenance overhead.
   - *Security / Compliance*: SOC2, HIPAA, data residency, GDPR requirements.
3. **Hiring / Budget Urgency**:
   - What roles indicate they are spending budget to solve this *right now*? (e.g. `Staff Infrastructure Engineer`, `Data Platform Lead`, `Senior DevOps`).

See [references/icp-decomposition.md](references/icp-decomposition.md) for full breakdown templates.

---

## Phase 2: Formulate Discovery Queries

The highest-intent public signal comes from Applicant Tracking Systems (ATS) where companies state their actual architecture and pain points in unedited job posts.

Search these domains directly:

| Source | Target Domain | Example Query |
|---|---|---|
| **Ashby** (Modern Tech/AI) | `jobs.ashbyhq.com` | `site:jobs.ashbyhq.com "Kafka" ("migration" OR "billing")` |
| **Greenhouse** (High-Growth) | `boards.greenhouse.io` | `site:boards.greenhouse.io "Postgres" ("latency" OR "50k QPS")` |
| **Lever** (Tech / Scaleups) | `jobs.lever.co` | `site:jobs.lever.co "ClickHouse" "scaling"` |
| **Engineering Docs** | `docs.*` / `blog.*` | `site:company.com/blog "architecture" "migration"` |

Compile discovered items into `accounts.csv` with columns:
- `item_id`: Company domain or identifier (e.g. `stripe.com`)
- `text`: Raw unedited job posting description or engineering blog excerpt
- `source_uri`: Direct URL of the live posting (e.g. `https://jobs.ashbyhq.com/stripe/...`)

See [references/discovery-playbook.md](references/discovery-playbook.md) for discovery scripts and search operators.

---

## Phase 3: Build the Task Spec & 0–100 Scoring Rubric

Register a typed task with an explicit 0–100 rubric. Every high score must cite an exact quote.

### Standard Claims Schema:
```json
{
  "type": "object",
  "properties": {
    "score": {
      "type": "integer",
      "minimum": 0,
      "maximum": 100,
      "description": "ICP qualification fit score from 0 to 100"
    },
    "identified_gap": {
      "type": "string",
      "description": "Concise summary of the verified technical bottleneck or initiative"
    },
    "fit_tier": {
      "enum": ["tier_1", "tier_2", "tier_3", "unfit"],
      "description": "tier_1 (85-100), tier_2 (70-84), tier_3 (50-69), unfit (<50)"
    },
    "reasoning": {
      "type": "string",
      "description": "Short explanation grounded in the cited source evidence"
    }
  },
  "required": ["score", "identified_gap", "fit_tier", "reasoning"],
  "additionalProperties": false
}
```

### Standard Rubric:
- **Tier 1 (85–100)**: Active, explicit initiative cited directly in source text (e.g. "migrating legacy billing to Kafka"). Must cite exact bottleneck quote.
- **Tier 2 (70–84)**: Relevant tech stack present and senior hiring underway, but specific migration is implied rather than explicitly stated.
- **Tier 3 (50–69)**: Right industry/firmographic fit, but tech stack is standard or unverified.
- **Unfit (<50)**: Uses incompatible architecture or outside the target domain.

See [references/scoring-rubric-guide.md](references/scoring-rubric-guide.md) for task templates and instructions.

---

## Phase 4: Execute with Free Models & Exact Substring Verification

Run the batch campaign through free model routes (OpenRouter / OpenCode / Local Ollama):

```bash
# Initialize task
account-fleet init target-research --preset account-research

# Run campaign across accounts.csv
account-fleet run target-research \
  --input accounts.csv \
  --id-column item_id \
  --text-column text \
  --run-id campaign-01 \
  --profile ideal_company_profile.json \
  --free-only \
  --sessions 4
```

Profile use is explicit. Pass `--profile PATH` to select and persist a profile,
or pass `--use-active-profile` to attach the active profile from SQLite. Without
either option, no profile is attached, so generic tasks are not changed by
account-specific onboarding state.

### The Invariant: Substring Verification Gate
Every model claim must include an exact quote. Under the hood:
```python
assert quote in raw_text
```
- If a cited quote is fabricated, paraphrased, or changed by one character, the check fails and the attempt rotates. A real quote does not prove that the score or claimed gap follows from it; review high-priority accounts against the source and the agreed ICP.
- If verified, the exact character range `[start, end]` and source URI are committed to SQLite.

---

## Phase 5: Export & Present the Ranked Deliverable

Export the top-scoring accounts sorted descending, ready for sales reps or CRM import:

```bash
account-fleet export campaign-01 \
  --format csv \
  --sort-by score \
  --desc \
  --top 25 \
  --rank \
  --output ranked_target_accounts.csv
```

### Format of the Final Deliverable:
| Rank | Account | Score | Identified Gap | Verbatim Quote Proof | Source URL |
|---|---|---|---|---|---|
| #1 | `stripe.com` | 98 | Legacy billing Kafka migration | “leading the migration of our legacy billing service to Apache Kafka” | `jobs.ashbyhq.com/stripe/...` |
| #2 | `hyper_ai` | 94 | Postgres QPS latency limits | “hitting latency limits at 50k QPS on Postgres cluster” | `boards.greenhouse.io/...` |

See [references/mcp-recipes.md](references/mcp-recipes.md) for prompt recipes in Claude Desktop, Codex, and Cursor.

## Related skills

- `harness-fleet` — the shared engine underneath every fleet playbook: task contracts, runs, export, MCP, troubleshooting.
- `partner-fleet` — turn ecosystem requirements into scored implementation partners (`--preset partner-research`).

Setup installs these next to this skill in `.agents/skills/`.

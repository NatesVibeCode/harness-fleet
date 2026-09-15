# The Boundaries Plan

How the fleet decides things: what is declared once, what each layer is allowed
to decide for itself, and how every claim, score and insight moves between
those layers without losing its meaning. Written to be executed, not admired:
each step names its boundary, its deliverable and the check that proves it.

> **See also:** [partner-product.md](partner-product.md) — the Partner Finder extraction (rename, own public repo, its own skill and lane). Queued, not started.

Applies to all three public distributions (`harness-fleet`, `account-fleet`,
`career-fleet`), which share one engine and differ only in branding and
product-specific extras.

---

## 0. The rule this plan exists to enforce

> Every rule that two layers must agree on is declared **once**, centrally.
> A layer may only be specific about the things it alone can know.

The failure mode this prevents is the one we kept hitting: a rule declared at
discovery, re-declared at scoring, assumed at presentation, and therefore
silently inconsistent at one of the three altitudes. Loose ends are not bugs to
patch individually; they are rules that were never given a home.

---

## 1. The altitude map (who decides what)

| Altitude | Decides | Must never decide |
| --- | --- | --- |
| **Central contracts** (`contracts.py`) | what a source category is and whether it corroborates; what each evidence kind proves; what a claim requires before a quote may carry it; how confidence is priced; what each tier may claim; what to search for when a kind is missing | anything about one product's queries, presets, checklist names or weights |
| **Taxonomy** (`sources.py`) | which URL is which category; which hosts are entities vs sources vs noise | scoring, confidence, presentation |
| **Reading** (`evidence.py`) | what a document/section says: sections, kinds present, contradictions, diffs, claim addressing | fetching, model calls, points |
| **Weights & tasks** (`task.py`, per-product presets) | item names, points, evidence vocabulary, half-lives, source weights | whether a quote addresses a claim (central), confidence pricing (central) |
| **Pipeline** (`engine.py`, `models.py`) | deriving score/tier/passed from claims **as priced by the contracts**; refusing unaddressed support | inventing evidence, re-defining requirements |
| **Orchestration** (`dag.py`) | order, branching, bounded loops, lineage | fetching policy, scoring rules |
| **Gathering** (`discover.py`) | how to find and read sources; what cannot be evidence (noise hosts) | what a claim requires, how strong a source is |
| **Presentation** (`board.py`, `export.py`, CLI output) | how an already-decided fact is shown, including its confidence | recalculating anything |
| **Product edges** (per-repo `branding.py`, presets, skill docs) | the product's name, its query set, its preset, its extra commands | any rule listed as central |

**Boundary test that must exist for each row:** the layer imports the central
declaration rather than restating it (asserted, not assumed).

---

## 2. The claim lifecycle (the spine)

```
source URL ──classify──▶ category ─┐
                                    ├─▶ can this source carry this claim? (central)
quote text ────────────────────────┘        │
                                             ▼
                              addressed? ──no──▶ contributes 0, reason recorded
                                             │yes
                                             ▼
                         strength = source weight × recency decay (existing)
                                             │
                                             ▼
                       confidence = level priced by category + corroboration (central)
                                             │
                                             ▼
                 score = Σ(points × strength × confidence weight), capped by minimums
                                             │
                                             ▼
        tier = band the score earns, then capped by TIER_MINIMUMS with reasons
                                             │
                                             ▼
                    presentation carries: claim, quote, source, confidence, reasons
```

Boundary: **only the pipeline computes scores**; only the **contracts** decide
what counts; only **presentation** formats. A missing piece anywhere in this
chain must produce a *recorded reason*, never a silent zero.

---

## 3. Execution steps

Each step: boundary → deliverable → check. Steps are ordered so earlier ones
make later ones verifiable.

### Step 1 — Close the contract boundary (audit for re-declarations)
- **Boundary:** central vs every other altitude.
- **Deliverable:** every scoring/validation rule that exists in more than one
  layer moved into `contracts.py`; the other layers import it. Remove the
  leftover regexes/kind lists that duplicate a contract (`COMMERCIAL_RE`,
  `CERT_RE`, `GROWTH_RE`, `OUTCOME_RE`, `DELIVERY_VERB_RE` currently live in
  `evidence.py` *and* shape rules in `contracts.py`).
- **Check:** a test that fails if a layer declares a duplicate rule
  (`test_contracts.py::test_no_layer_redeclares_a_central_rule`), plus the full
  suite green in three repos.

### Step 2 — Make confidence visible (presentation boundary)
- **Boundary:** pipeline decides, presentation shows.
- **Deliverable:** every scored record's evidence readout carries, per claim:
  the quote, its source, its category, its **confidence level** and any refusal
  reason; `runs/<id>/evidence.json`, the board payload and the board page show
  it. Export carries the same fields in CSV/JSONL.
- **Check:** unit test on the readout shape; a board payload test; a rendered
  page check (playwright, no JS errors) showing a weak vs verified claim.

### Step 3 — Give the review loop a real yield
- **Boundary:** orchestration decides *what* to look for; gathering decides *how*.
- **Deliverable:** `KIND_QUERIES` gains per-kind source targeting (vendor hubs,
  ATS boards, directories) rather than open-web text; a review round records
  which query produced which new source, so yield is measurable.
- **Check:** the live review round on a real account adds at least one source
  and flips `unsatisfied` to empty, or the run records precisely why not.

### Step 4 — One entry point per product (parity boundary)
- **Boundary:** shared surface vs product extras.
- **Deliverable:** each distribution's console script points at the shared
  CLI; product-specific commands (career's lanes) become additions on that
  surface, not a parallel CLI. Docs updated in the same commit.
- **Check:** the command surfaces are provably identical (30/30/30 today) and
  every documented command parses (`test_docs_commands.py`).

### Step 5 — Verify the UIs and the outputs (verification boundary)
- **Boundary:** what tests prove vs what a person must spot-check.
- **Deliverable:** studio (harness, account) and board (all three) rendered
  from real runs, screenshots + zero JS errors; one real scored deliverable per
  product with quotes, confidence and tier caps; the spot-check findings written
  next to the artifacts.
- **Check:** screenshots exist, each shows real scored data, and each finding
  names the artifact a person can open.

### Step 6 — Lock it so it cannot loosen again
- **Boundary:** what drift enforces across the fleet.
- **Deliverable:** `contracts.py`, `evidence.py`, `models.py`, `dag.py`,
  `cli.py`, `discover.py`, `board.py`, `task.py`, `sources.py` byte-identical
  and drift-locked; `branding.py` the only per-product file.
- **Check:** drift `PASS` in three repos, plus a negative control (introduce a
  divergence, watch it fail, restore).

---

## 4. Rules of engagement (no cheating)

1. **No step is done without its check.** "Implemented" means the check ran and
   its output is quoted.
2. **No rule gets a second home.** If a rule is needed twice, it moves to the
   contract; it is never copied.
3. **No silent zeroes.** Every refusal, cap or missing piece records a reason
   a person can read.
4. **No claiming parity without the parity check.** Identical surfaces are
   proven by comparison, not by intent.
5. **No local special-casing.** A local checkout is "just another consumer":
   if the public path needs a workaround, the workaround is the bug.
6. **Report the gaps.** Every step ends with what it did *not* cover.

---

## 5. Status ledger (updated as steps execute)

| Step | State | Evidence |
| --- | --- | --- |
| 1 Close the contract boundary | **done** | the five scoring signals had homes in both `evidence.py` and `contracts.py`; `evidence.py` now imports them (`evidence.OUTCOME_RE is contracts.OUTCOME_RE`). `tests/test_contracts.py` fails on a re-declaration — proven by inserting a real copy of `GROWTH_RE` into `evidence.py` and watching it fail, then restoring |
| 2 Confidence visible | **done** | `evidence.claim_confidence()` reports kind → confidence → weight → category → how it is shown; `entity_evidence` carries `confidences` + `weakest`; the run readout, board payload and board drawer all show it. Rendered check: first-party-only run shows `delivery proof: weak` beside `Scored Tier 1, evidence supports Tier 2`; a vendor story upgrades the same kinds to `corroborated`/`verified`. Zero JS errors |
| 3 Review loop yield | partial | node runs end to end, diagnoses `{'acme.com': ['independent_validation']}`, generates the targeted query, records `nothing_new` when the search yields nothing attributable. Yield not yet improved |
| 4 One entry point per product | partial | identical 30-command surfaces proven; career's console script still points at its own CLI |
| 5 Verify UIs and outputs | partial | harness board verified twice (evidence caps, then confidence); studio and account/career boards still unverified |
| 6 Lock it | mostly done | drift 3/3; `contracts.py`, `evidence.py`, `dag.py`, `cli.py`, `discover.py`, `board.py`, `task.py`, `sources.py` locked; `branding.py` is the only per-product file |

### Step 7 (added) — Confidence prices the score, not just the label
- **Boundary:** the contract decides what evidence is *worth*; the pipeline only
  multiplies. `SOURCE_TRUST`, `VAGUE_SPECIFICITY`, `CORROBORATION_STEP`,
  `UNTRUSTED_CEILING` and `aggregate_support()` live in `contracts.py`;
  `support_strengths` now aggregates every supporting source per claim instead
  of taking the strongest one.
- **Model:** strength = aggregate over sources of `trust(category) ×
  specificity(quote) × source_weight × recency`. The best source sets the base,
  each further source adds a diminishing amount, trust caps the ceiling, and a
  vague-but-addressed quote is worth `VAGUE_SPECIFICITY`.
- **State: model landed, expectations not migrated.** The change is deliberate
  and it invalidates tests that encoded the previous contract
  ("score = points × source weight"): 11 failures across the three repos —
  `test_calibrate*` (fits weights inside the old strength model),
  `test_mechanical_calibration::test_support_strengths_take_max_backing_weight`
  (asserts the rule that was replaced),
  `test_scoring_integrity::test_weighted_score_revalidates_with_its_own_strengths`
  (premise: 100 unweighted → 50 at weight 0.5; now also scaled by trust),
  `test_run_evidence::test_an_unsupported_tier_is_capped_and_the_gap_is_named`
  (the score no longer reaches tier_1, so no cap is exercised),
  `test_cli::test_rescore_links_lineage_and_history` and
  `test_dag::test_calibrate_apply_threads_revision_into_run` (assert score 100).
  **Migration progress (5 red per repo, one root cause):** migrated and green —
  the "take max backing weight" test (now asserts aggregation), both engine score
  tests (they derive their expectation from the model), the weighted-score
  revalidation premise, rescore lineage/history (now asserts an ATS-sourced round
  outranks a generic-page round), and the tier-cap test (the cap is asserted
  where it is decided; the earned score is asserted separately). **Still red:**
  `tests/test_calibrate.py` (4) and
  `tests/test_dag.py::test_calibrate_apply_threads_revision_into_run` — the
  calibration fixtures and labels encode the pre-model arithmetic
  (`score = points × source weight`), so the fit can no longer reach `mae == 0`.
  The fix is to regenerate those labels from the forward model (the same
  `support_strengths` the engine uses) and re-derive what "recovers known
  points/weights/half-lives" means under contract-priced strengths. Not done.

---

## 6. Step 8 — the default surface is a local MCP, the CLI is the power tool

**Boundary:** the *transport* is a surface decision, not a scoring decision; the
engine must behave identically whichever way it is driven.

Why MCP first: the person using this already has an assistant (Claude, Cursor,
Codex, Grok). An MCP server means no shell, no flags, no keys, and no new UI to
learn — and because it runs over stdio on the user's machine, the only thing
that leaves is the outbound fetch of public sources. The assistant drives the
same engine the CLI drives.

- `setup` now leads with `mcp install` in its `next_commands`; the CLI remains
  for scripting, CI and debugging.
- The per-install server name comes from the installed entry point, so an
  account install is listed as `account-fleet`, not `harness-fleet`.
- **Provider choice stays pluggable at the edge:** the routes already cover
  opencode, codex, claude, cursor, grok, muse and antigravity, so "align the
  sensitive step with my own Codex/Claude/Grok harness" is a route policy, not a
  second pipeline. Free zero-price routes stay the default because they need no
  key; a user's own harness is an opt-in pin.
- **Gap to close:** the MCP tool list predates the newest capabilities. It must
  expose the same operations the CLI does — research (discover → bundle → score
  → export), the evidence readout with per-insight confidence, and the review
  round — so nothing is CLI-only by accident.

### Legacy ledger (what to simplify out)

| Item | Where | Decision |
| --- | --- | --- |
| career's own product CLI as the installed entry point | `career_fleet/cli.py` + `pyproject.toml` | retire in favour of the shared surface; its lanes become commands on it |
| career's forked engine modules | was `harness_fleet/*` | already deleted, replaced by the shared files |
| `career-lanes` entry-point alias | `branding.py` | keep (cheap resolution), revisit if unused |
| `free-fleet`/`bulk-lanes` migration notes and `*.db` names | READMEs, `FREE-ACCESS.md` | docs stay (historical), code has no support for them |
| duplicated `discover`/`fetch` source tables | `discover.py` | one table per direction, already the case; verify no second list appears |
| `partner_sourcing.py` vs `bundler.py` overlap | harness-only files | fold the shared taxonomy use into `sources.py`/`contracts.py`; keep the partner pipeline thin |
| per-repo copy of `FREE-ACCESS.md`, `SECURITY.md`, `CONTRIBUTING.md` | all three | keep (each distribution needs its own), but the content should describe the shared engine once |

---

## 7. Step 9 — The evidence model's complexity ceiling, and what to add (if anything)

Prompted by a fair challenge: is the hand-rolled model at the limit of what it
can carry, and would standard libraries help? Evaluated against the four places
complexity actually lives here.

| Where complexity lives | Current approach | Verdict |
| --- | --- | --- |
| Fitting scoring parameters | hand-rolled coordinate descent (`calibrate.py`) | **Change the model, not the solver.** The five red tests are not a solver problem: the constants being fitted sit next to hand-chosen ones in a forward model whose labels were generated by the older arithmetic. See the reframing below. |
| Aggregating evidence | hand-rolled arithmetic in `contracts.py` (`trust × specificity`, diminishing corroboration, `UNTRUSTED_CEILING`) | **Raise the ceiling with semantics, not dependencies.** A Beta/log-odds update in pure Python expresses "many weak sources are an indicator, a trusted source is proof, and the uncertainty is a number" exactly, and stays testable. |
| Aggregating data at bulk scale | pure-Python loops over records, SQLite | `polars` (or `pandas`) behind an **optional extra** for the bulk path (thousands of accounts, per-source yield, run-to-run diffing, calibration datasets). Not a default: the install must stay light for the person who just wants their list. |
| Orchestration | 550-line dependency-free DAG with resume + lineage | **Keep.** Airflow/Prefect/Dagster/celery/rq/ray/dask all contradict local-first, single-process, no-broker. `transitions`/`python-statemachine` only if the lifecycle outgrows a dict of states. |

**The reframing that matters:** `SOURCE_TRUST`, `VAGUE_SPECIFICITY`,
`CORROBORATION_STEP` and `UNTRUSTED_CEILING` are not laws, they are my initial
estimates. They belong in the same parameter set that `calibrate` fits from
labelled examples — points, source weights and half-lives already are. That
makes the five red calibration tests the *right* failure: the model gained
parameters that nothing fits yet, and until they are fitted, "recovers known
points" is asking the fit to reproduce labels generated by a different model.

**Adoption policy (decided):**
1. Default install stays `pydantic`, `jsonschema`, `httpx`, `mcp` + discovery
   extras. No analysis, orchestration or distribution dependency by default.
2. Optional extras carry the heavy tools: `[analysis]` = `polars` + `pandera`
   for bulk frames, `[fit]` = `numpy`/`scipy` if the pure-Python solver proves
   insufficient. Both must be optional for the code that needs them and absent
   for the code that does not.
3. No orchestrator, no broker, no cluster. The DAG is ours and stays small.
4. Every parameter that affects a score is either fitted from labels or declared
   in `contracts.py` with its provenance stated — no silent constants.


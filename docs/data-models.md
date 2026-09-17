# The data models

One model per lane. A lane's model is **the fields its rows must carry** — the
score is a by-product, the fields are the product — plus the gates that decide
who is worth spending a fetch on.

Sources for this document are this session's corrections. Where a line is your
stated requirement it says **[stated]**; where I inferred it from the code or
from what the gates can actually resolve it says **[inferred]**.

---

## The shared skeleton

Every model answers the same four questions, in the same order:

1. **Population** — what kind of thing is this, and what disqualifies it.
2. **Firmographics** — size, location, territory. Decided before any semantics,
   because nobody at a company of the wrong size gets approval.
3. **Semantics** — what they actually do, gathered from *their own* website.
4. **Fields** — what survives onto the row, each with the page and quote behind
   it.

And every claim on a row carries: the **source page**, the **quote**, the
**source category** (who published it) and its **evidence grade** — `snippet`
(a search result, can eliminate, can never qualify) or `fetched` (a page we
read, can qualify).

---

## 1. Partner — the Ideal Partner Profile (IPP)

### Population **[stated]**

**System integrators: GSI, RSI and SI.** Not software companies. And not the
platform vendors themselves — Snowflake, Databricks, Elastic, Datadog, MongoDB
and AWS are *practice axes*, never candidates and never sources.

- **Disqualify:** a product company (its own platform, pricing, demos); a
  platform vendor; a customer of a platform vendor, which is an account.
- **Qualify:** a firm that delivers implementation work for clients.
- **Exclusion list to maintain [inferred]:** the platform vendors' own domains.
  They keep entering through search because `{tech}` terms name them.

### GSI / RSI / SI is a class, derived **[inferred from your list]**

Nobody writes "regional system integrator" on their site. The class comes from
what the gates already collect:

| class | headcount | territory |
|---|---|---|
| GSI | 10,000+ | many countries / global |
| RSI | ~200–10,000 | a region: EMEA, APAC, LATAM, North America — or a cluster: UK + Nordics, DACH |
| SI | < ~200 | one country or metro |

Open: whether GSI is in scope (you listed it) or excluded as already-known (you
said you don't need to be told about Accenture). And whether "regional" means a
named region or a country cluster.

### The fields — what a partner row must carry **[stated]**

> "we care about what verticals they work in, what their expertise is, what
> software companies they partner with"

That is the core three. Everything else is supporting detail for the
recommendation.

| field | read from | evidence kind | why it matters |
|---|---|---|---|
| **verticals they work in** | their case studies, industries page | `named_clients`, `delivery_proof` | whether they sell into the industries you serve |
| **expertise** — what they actually do | services / what-we-do, case studies | `stack_delivery`, `delivery_proof` | the practice you would be plugging into |
| **software partners they carry** | their partners / certifications page | `certification` | whether your product fits a shelf they already stock |
| integrator class (GSI/RSI/SI) | derived: headcount + territory | — | the population you asked for, stated on the row |
| headcount | about, careers, snippets | — | size gate, and the class |
| territory / locations | footer, contact, about | — | location gate, and the class |
| service model | services page, wording | — | turnkey SI · migration · managed service · staff aug |
| named clients | case studies | `named_clients` | proof the practice is real |
| client outcomes | case studies | `delivery_proof` | metrics, not capability claims |
| commercial terms | services / about | `commercial_terms` | minimum engagement, rates, headcount |
| independent validation | vendor story naming them, community | `independent_validation` | somebody other than them vouching |
| delivery hiring | their ATS board | `delivery_hiring` | live capacity in the practice you care about |
| published engineering | blog, GitHub org | `engineering_output` | whether the practice is technical or a reseller |
| growth signals | news | `growth_signal` | new practice, office, acquisition |
| third-party mentions | community | `independent_validation` | what the market says |

### The recommendation the row drives **[stated, in the preset]**

`revenue_hypothesis`: how you would make money with them — co-sell, referral,
reseller, or subcontract — tied only to cited evidence.

### The bar **[inferred — a decision you own]**

Today `tier_2`: `delivery_proof` + `stack_delivery`, both of which the ladder
carries from the firm's own case studies and services pages.

It was `tier_1`, whose extra requirement is `independent_validation`. That was
written when review directories supplied it; they answer 403 now, and the only
surviving carriers are vendor story indexes and community mentions — which is
the ecosystem this lane exists to look *past*. The lane finds integrators
directly, so it cannot prove a third party vouched for them, and a bar the
sources cannot reach is a bar that returns nothing. The floor is the honest one
until a carrier that is not a vendor's index exists.

### DAG shape

The ladder *is* the pipeline. `lane_spec` compiles it — the same function a run
calls — into one node per rung, and each node writes the table it produced into
the run's own SQLite database: `rung_rows` (the population, the verdicts, who it
passes on), `rung_text` (the prose each verdict stood on) and `rung_items` (what
a walk gathered). The next node queries those tables rather than parsing a file,
and CSV is an export you ask for. Each write is an *attempt* keyed by `run_seq`,
so re-running a rung adds an answer rather than replacing one.

Nothing prunes itself: a fleet run a hundred times is a hundred attempts per
node. `harness-fleet db stats` says what the store is holding and
`harness-fleet db prune --keep-attempts N` drops all but the newest attempts —
the newest is the answer, so pruning never changes one.

Two tables outlive the run:

- **`entity_state`** — the running list. One row per entity, forever: what is
  currently believed (last verdict, the gate that stopped it, what is still
  open), **the score and the facts the scoring node produced**, and how many
  times it has been seen. Every node folds its rows in as it finishes, so the
  ledger's answer and the deliverable's answer are the same answer.
  `harness-fleet ledger` reads it, sorted by score.
- **`entity_events`** — append-only, one row per entity per node per attempt, so
  "when did they first appear" and "why did we drop them" stay answerable after
  the state row has moved on. An elimination stands until something actually
  re-qualifies the entity; a node that merely carried it forward cannot revive
  it, and a node that only went and looked records its visit without touching
  the verdict.

Score trajectories have two writers and one reader. The engine prices every
campaign into `score_history`; the ledger records what each node decided. A
trajectory is the union of the two — `harness-fleet history <entity>` and
`harness-fleet ledger --trend` read the same rows, so they cannot disagree about
a firm depending on which door its score came through. `harness-fleet dag --lane partner --from-items captured.jsonl --emit-spec`
prints exactly this:

```
g0-result    gate     0 fetches · kind, size, location
x0-result    resolve  one search per open candidate: "<name>" address / headcount
c0-result    gate     the same questions, on what the search said
r1-surface   retrieve home, about, services, partners, careers
g1-surface   gate     kind, size, location — on pages the run opened
r2-stories   retrieve case_studies, partners, blog, news, vendor_stories
g2-stories   gate     vertical
s-score      score    reads the firms still standing, runs the campaign,
                      writes score + tier + facts onto the run and the list
  ▼
export → row carrying the fields below
```

A candidate eliminated at any node is absent from every table above it, and a
candidate whose open gates the search settled is never walked at all. The
per-lane picture, node by node and rung by rung, is `docs/funnels.md`.

---

## 2. Account — the Ideal Company Profile (ICP)

### Population **[stated]**

**Your competitors' customers.** The firms that buy in your space. This is why
vendor customer-story indexes are the *right* source for this lane and the wrong
one for partners: a story names the company that bought the platform.

- **Disqualify [inferred]:** directories, aggregators, listing pages — pages that
  are not a company at all.
- **Qualify:** a company that operates in the space and buys in it. It may be a
  product company; that is not a disqualifier here.

### Firmographics before semantics **[stated]**

Size, location and territory first. Kind is *not* asked: `allows: "any"`.

### The fields **[inferred from the ICP and the preset]**

| field | read from | evidence kind |
|---|---|---|
| what they run (stack) | their engineering blog, case studies, job posts | `stack_delivery` |
| triggers / initiatives named in their own words | blog, press, job posts | `delivery_proof`, `dated_events` |
| vertical | their site, their customers | `named_clients` |
| headcount, territory | about, snippets | — |
| hiring in the relevant function | their ATS board | `delivery_hiring` |
| growth | news | `growth_signal` |

**Gap, today:** `account-research` defines **no `answers`** at all, so an account
row carries a score and a gap and *none of these fields*. The profile
(`IdealCompanyProfile`) already names them — `required_stack`,
`negative_stack_exclusions`, `trigger_pain_phrases`, `target_roles`,
`anchor_logos` — they just have nowhere to land on the row.

---

## 3. Career — the Ideal Employer Profile

### Population **[stated: "just like there is an ideal employer profile"]**

Employers hiring for the roles the lane describes. The row is a **role**, and the
employer is its context.

### The fields **[inferred]**

| field | read from | evidence kind |
|---|---|---|
| the requisition itself | the posting | `delivery_hiring` |
| role, seniority | the posting | — |
| location / remote | the posting | — |
| compensation | the posting | `commercial_terms` |
| what the employer runs | posting + their engineering pages | `stack_delivery` |

### The bar **[inferred]**

`require_kinds: ["delivery_hiring"]` — strictly an ATS requisition. That is a
hard bar: it is why this lane returns single digits, and `delivery_hiring` is
carried only by the `ats` surface, which **no rung currently reads**.

---

## 4. Cross-lane contracts

These are shared by all three models, declared once in `contracts.py`:

- **Evidence kinds** (10): delivery_proof · named_clients · stack_delivery ·
  independent_validation · delivery_hiring · commercial_terms · certification ·
  engineering_output · growth_signal · dated_events
- **Source categories**: a vendor's own story · a directory/audit profile · a
  community post · an ATS requisition · the entity's own case study · the
  entity's own practice page · general web
- **Which category may carry which kind** — the evidence bar. A blog cannot
  prove hiring; only a requisition can.
- **Confidence**: `reported` when a qualifying source carries the claim,
  `unsupported` otherwise. There is no trust continuum and no averaging.
- **Grades**: `snippet` (eliminates only) · `fetched` (may qualify)
- **Outcomes per gate**: pass · fail · unknown — unknown is neither, and is
  carried forward with what would resolve it
- **Verdicts**: qualified · lead · eliminated, each with the gate and reason

---

## 5. The gap between this spec and the code

| model | spec says | code does |
|---|---|---|
| partner | GSI/RSI/SI, verticals + expertise + shelf on the row | queries are SI-shaped and no longer search for a vendor's product; the product markers decide `read_kind`, so a vendor's own site fails the kind gate; the floor is `tier_2`, which the firm's own pages carry; **the class (GSI/RSI/SI) is still not written onto the row** |
| account | competitor's customers with stack, triggers, vertical | the ladder gathers them — first-party surfaces plus the searches that settle headcount and country — but **`account-research` defines no `answers`**, so nothing lands on the row |
| career | requisition rows with role, comp, employer | `ats` is named on the posting rung, so `delivery_hiring` is reachable; firmographics come from the employer profile |

Three things this table said are fixed; what it says now is what is left. Each
lane's own reachability check, kind by kind, is the OK/GAP block in
`docs/funnels.md` — computed from the same lane data a run reads.

---

## 6. Who produces what — the data points per DAG node

Each model's fields come from a named rung. Nothing is gathered "just in case":
a rung runs because a gate is open or a field is missing, and only the surfaces
that carry them are read.

### Partner

| node / rung | evidence | data points it produces |
|---|---|---|
| search (SI queries) | `snippet` | candidate identity, and whatever the result states: headcount, location, kind |
| rung `result` | `snippet` | **integrator class** (derived), size, territory — and eliminations |
| `resolve` (one search) | `snippet` | **size and territory as published facts**, for a candidate whose result did not state them |
| rung `surface` | `fetched` | **expertise** (services / what-we-do), **service model**, headcount, territory, **software partners they carry** (partners page), hiring signals (careers) |
| rung `stories` | `fetched` | **verticals they work in**, named clients, client outcomes, published engineering, growth signals, **independent validation** (vendor stories naming them) |
| `s-score` node | `fetched` | checklist, `identified_practice`, `revenue_hypothesis`, the 14 `answers` fields, and the tier the evidence supports: `target_stack`, `service_model`, `industry_verticals`, `delivery_coverage`, `vendor_alliances`, `case_study_outcome`, `client_logos`, `hiring_signals`, `commercial_terms`, `revenue_motion`, `third_party_mentions`, `engineering_output`, `growth_signals`, `evidence_categories` |

### Account

| node / rung | evidence | data points it produces |
|---|---|---|
| search | `snippet` | candidate identity, headcount, location |
| rung `result` | `snippet` | eliminations on size and territory |
| `resolve` (one search) | `snippet` | **headcount and country**, settled without a page fetch |
| rung `surface` | `fetched` | **what they run** (stack), firmographics confirmed |
| rung `stories` | `fetched` | **vertical**, triggers and initiatives in their own words, named clients, outcomes |
| score | — | checklist, `identified_gap`, `fit_tier`, reasoning — **and no structured fields today**, because `account-research` defines no `answers` |

### Career

| node / rung | evidence | data points it produces |
|---|---|---|
| search | `snippet` | candidate role page, employer name |
| rung `result` | `snippet` | eliminations on territory |
| `resolve` (one search) | `snippet` | **where the employer is**, which is what the lane's only gate asks |
| rung `posting` | `fetched` | role, seniority, **location / remote**, compensation, what the employer runs, and the requisition that satisfies `delivery_hiring` |
| score | — | `priority`, `reason` — the row is a role, and the fields above are its context |

**The rule this makes visible:** a data point only exists on a row if some rung
reads a surface that carries it. The bar check in `docs/funnels.md` is the same
test applied to evidence kinds; this is the same test applied to fields.

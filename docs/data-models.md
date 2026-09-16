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

Today `tier_1`: `delivery_proof` + `independent_validation` + `stack_delivery`.
`independent_validation` was written when review directories supplied it; those
answer 403 now, so its surviving carriers are vendor stories and community
mentions — and **no rung reads either**, which is why the lane cannot qualify
anything. Either a rung names `vendor_stories`, or the floor drops to `tier_2`.

### DAG shape

```
search (SI-shaped queries, {tech} × {vertical})
  │
  rung: result   snippet · kind(size, territory) · 0 fetches
  │              eliminates a product company; never qualifies
  rung: surface  fetched · their site: home, about, services, partners, careers
  │              qualifies firmographics; expertise + shelf live here
  rung: stories  fetched · case_studies, partners, blog, news
  │              settles vertical; names clients and outcomes
  ▼
bundle per firm → score → row carrying the fields above
```

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
| partner | GSI/RSI/SI, vendors excluded, verticals + expertise + shelf on the row | queries are SI-shaped; `read_kind` still lets a product vendor through by matching "our customers"; no vendor exclusion; no class field; `tier_1` unreachable |
| account | competitor's customers with stack, triggers, vertical | no `answers` fields at all; 2 queries, story indexes off |
| career | requisition rows with role, comp, employer | `delivery_hiring` unreachable — no rung reads `ats` |

The questions the lanes ask are now right. What the lanes can *gather*, and what
their rows *carry*, is not yet what this document describes.

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
| rung `surface` | `fetched` | **expertise** (services / what-we-do), **service model**, headcount, territory, **software partners they carry** (partners page), hiring signals (careers) |
| rung `stories` | `fetched` | **verticals they work in**, named clients, client outcomes, published engineering, growth signals, **independent validation** (vendor stories naming them) |
| score | — | checklist, `identified_practice`, `revenue_hypothesis`, and the 14 `answers` fields: `target_stack`, `service_model`, `industry_verticals`, `delivery_coverage`, `vendor_alliances`, `case_study_outcome`, `client_logos`, `hiring_signals`, `commercial_terms`, `revenue_motion`, `third_party_mentions`, `engineering_output`, `growth_signals`, `evidence_categories` |

### Account

| node / rung | evidence | data points it produces |
|---|---|---|
| search | `snippet` | candidate identity, headcount, location |
| rung `result` | `snippet` | eliminations on size and territory |
| rung `surface` | `fetched` | **what they run** (stack), firmographics confirmed |
| rung `stories` | `fetched` | **vertical**, triggers and initiatives in their own words, named clients, outcomes |
| score | — | checklist, `identified_gap`, `fit_tier`, reasoning — **and no structured fields today**, because `account-research` defines no `answers` |

### Career

| node / rung | evidence | data points it produces |
|---|---|---|
| search | `snippet` | candidate role page, employer name |
| rung `result` | `snippet` | eliminations on territory |
| rung `posting` | `fetched` | role, seniority, **location / remote**, compensation, what the employer runs, and the requisition that satisfies `delivery_hiring` |
| score | — | `priority`, `reason` — the row is a role, and the fields above are its context |

**The rule this makes visible:** a data point only exists on a row if some rung
reads a surface that carries it. The bar check in `docs/funnels.md` is the same
test applied to evidence kinds; this is the same test applied to fields.

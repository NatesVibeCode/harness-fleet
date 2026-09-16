# The funnels, as they ship

Generated from the lane files, the surface plan and the contracts — not written
by hand — so this picture cannot drift from what a run does. Regenerate with
`tools/render_funnels.py`.

## How to read one

- **population gate** — the kind question the lane asks of a candidate. `services`
  asks "is this a delivery firm"; `any` asks nothing.
- **bar** — what a scored row has to be able to prove.
- **rung** — one step of the ladder. `evidence=snippet` costs no page fetch;
  `evidence=fetched` reads pages. Each rung judges what the ones below returned,
  and only what passes continues, so a candidate that fails early never spends a
  fetch.
- **gates** — the questions a rung puts to the candidate.
- **read** — the surfaces a rung fetches, expanded to what that name actually
  means.
- **earns** — what passing buys: the next rung, or nothing further.
- **OK / GAP** — whether the bar's evidence kinds can be gathered by the ladder
  as written. A GAP is a row that can never clear its own bar.

══════════════════════════════════════════════════════════════════════════════
  ACCOUNT LANE - Target accounts for a technical ICP: a competitor's customers — the firms that buy in this space, and what they run
══════════════════════════════════════════════════════════════════════════════
  population gate : allows='any'   (asks no kind question)
  bar             : tier_3
  scored with     : account-research    output: top 25
  volume          : 2 queries, no story indexes

  +-- ENTRY ----------------------------------------------------------+
  | keyword search: 2 template(s) -> 2 queries
  | backends: ddgs, hn
  +-------------------------------------------------------------------+
                      |  candidates: one row per company
                      v
──────────────────────────────────────────────────────────────────────────────
  RUNG 1 . result   evidence=snippet  (0 page fetches)
  gates     : size, location
    read    : nothing - judges what the search already returned
  earns     : eliminates on firmographics, or earns one page visit — it never qualifies
                      |
                      v  only what passed result continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 2 . surface   evidence=fetched  (reads pages)
  gates     : size, location
    read    : about         paths: /about, /about-us, /company
    read    : services      paths: /services, /what-we-do, /solutions...
    read    : careers       paths: /careers, /jobs, /join-us
  earns     : qualifies the cheap gates; the stack they actually run lives here
                      |
                      v  only what passed surface continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 3 . stories   evidence=fetched  (reads pages)
  gates     : vertical
    read    : case_studies  paths: /case-studies, /case-study, /our-work...
    read    : blog          paths: /blog, /insights, /resources...
    read    : news          paths: /news, /newsroom, /press
  earns     : settles which industries the work is actually in
──────────────────────────────────────────────────────────────────────────────
                      v
  +-- OUT ------------------------------------------------------------+
  | eliminated : recorded with the gate and reason, never fetched
  | lead       : unresolved on something a fetch could settle, gap named
  | qualified  : every gate it could be resolved on passed
  +-------------------------------------------------------------------+
                      |  survivors, bundled per company
                      v
  BAR . tier_3 needs: stack_delivery
    OK  stack_delivery           carried by services, case_studies, blog, code   <- rung reads: services, case_studies, blog

  FIELDS THE ROW CARRIES . preset account-research
    claims: checklist, score, identified_gap, fit_tier, reasoning
    answers: none defined - this preset returns claims only

══════════════════════════════════════════════════════════════════════════════
  CAREER LANE - Enterprise sales and operations roles, remote. The lane states what it wants; which boards carry it is learned, not listed.
══════════════════════════════════════════════════════════════════════════════
  population gate : allows='any'   (asks no kind question)
  bar             : none + require_kinds=['delivery_hiring']
  scored with     : triage    output: top 50
  volume          : 4 queries, no story indexes

  +-- ENTRY ----------------------------------------------------------+
  | keyword search: 4 template(s) -> 4 queries
  | backends: ddgs, hn
  +-------------------------------------------------------------------+
                      |  candidates: one row per company
                      v
──────────────────────────────────────────────────────────────────────────────
  RUNG 1 . result   evidence=snippet  (0 page fetches)
  gates     : location
    read    : nothing - judges what the search already returned
  earns     : drops a posting whose employer is not the kind of company this lane wants
                      |
                      v  only what passed result continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 2 . posting   evidence=fetched  (reads pages)
  gates     : location
    read    : careers       paths: /careers, /jobs, /join-us
    read    : about         paths: /about, /about-us, /company
  earns     : confirms the employer from the posting page itself
──────────────────────────────────────────────────────────────────────────────
                      v
  +-- OUT ------------------------------------------------------------+
  | eliminated : recorded with the gate and reason, never fetched
  | lead       : unresolved on something a fetch could settle, gap named
  | qualified  : every gate it could be resolved on passed
  +-------------------------------------------------------------------+
                      |  survivors, bundled per company
                      v
  BAR . require_kinds needs: delivery_hiring
    GAP delivery_hiring          carried by ats   <- NO RUNG READS THIS
  !! delivery_hiring cannot be gathered by this ladder as written

  FIELDS THE ROW CARRIES . preset triage
    claims: priority, reason
    answers: none defined - this preset returns claims only

══════════════════════════════════════════════════════════════════════════════
  PARTNER LANE - Net-new systems integrators and consultancies, found directly and qualified layer by layer
══════════════════════════════════════════════════════════════════════════════
  population gate : allows='services'   (asks: is this a delivery firm?)
  bar             : tier_1
  scored with     : partner-research    output: top 25
  volume          : 34 queries, no story indexes

  +-- ENTRY ----------------------------------------------------------+
  | keyword search: 6 template(s) x 2 term set(s) -> 34 queries
  |   tech: Kafka, Snowflake, Databricks, dbt, Kubernetes
  |   vertical: fintech, healthcare, retail, logistics
  | backends: ddgs, hn
  +-------------------------------------------------------------------+
                      |  candidates: one row per company
                      v
──────────────────────────────────────────────────────────────────────────────
  RUNG 1 . result   evidence=snippet  (0 page fetches)
  gates     : kind, size, location
    read    : nothing - judges what the search already returned
  earns     : eliminates a product vendor, or earns one page visit — it never qualifies
                      |
                      v  only what passed result continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 2 . surface   evidence=fetched  (reads pages)
  gates     : kind, size, location
    read    : home          paths: /
    read    : about         paths: /about, /about-us, /company
    read    : services      paths: /services, /what-we-do, /solutions...
    read    : partners      paths: /partners, /partnerships, /alliances
    read    : careers       paths: /careers, /jobs, /join-us
  earns     : qualifies the cheap gates on their own pages, landing first, where a snippet may not
                      |
                      v  only what passed surface continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 3 . stories   evidence=fetched  (reads pages)
  gates     : vertical
    read    : case_studies  paths: /case-studies, /case-study, /our-work...
    read    : partners      paths: /partners, /partnerships, /alliances
    read    : blog          paths: /blog, /insights, /resources...
    read    : news          paths: /news, /newsroom, /press
  earns     : settles the vertical gate, which only the case studies can answer
──────────────────────────────────────────────────────────────────────────────
                      v
  +-- OUT ------------------------------------------------------------+
  | eliminated : recorded with the gate and reason, never fetched
  | lead       : unresolved on something a fetch could settle, gap named
  | qualified  : every gate it could be resolved on passed
  +-------------------------------------------------------------------+
                      |  survivors, bundled per company
                      v
  BAR . tier_1 needs: delivery_proof, independent_validation, stack_delivery
    OK  delivery_proof           carried by case_studies, services   <- rung reads: case_studies, services
    GAP independent_validation   carried by vendor_stories, community   <- NO RUNG READS THIS
    OK  stack_delivery           carried by services, case_studies, blog, code   <- rung reads: services, case_studies, blog
  !! independent_validation cannot be gathered by this ladder as written

  FIELDS THE ROW CARRIES . preset partner-research
    claims: checklist, score, identified_practice, revenue_hypothesis, source_diversity_count, fit_tier, reasoning
    answers (14): target_stack, service_model, industry_verticals, delivery_coverage, vendor_alliances, case_study_outcome, client_logos, hiring_signals, commercial_terms, revenue_motion, third_party_mentions, engineering_output, growth_signals, evidence_categories
             required by the preset: yes

## What this says today

**partner** — the population question is right (`services`: a delivery firm, not
a product vendor) and the query breadth is real (6 templates over 5 technologies
and 4 verticals = 34 queries). But the bar is `tier_1`, which requires
`independent_validation`, and that evidence is carried by `vendor_stories` and
`community` — **neither of which any rung reads**. As written the lane cannot
qualify anything. Story indexes are also off (`stories: 0`), so candidates come
from search alone.

**account** — the population question is right for its definition (`any`: a
competitor's customers are buyers, and a buyer may be a product company). But it
carries **no structured fields at all**: `account-research` defines no `answers`,
so a scored row has claims and a score and no verticals, no stack, no named
clients. It also gathers almost nothing — 2 queries and no story indexes.

**career** — the bar is `delivery_hiring`, carried by `ats`, and **no rung reads
it**. Same shape of gap as partner: the lane cannot gather the evidence its own
bar requires.

The pattern across all three: the *questions* are now right and the *coverage* is
not. A bar names evidence the ladder never reads, or a lane asks for fields its
preset never defines.

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

Each lane also carries its ladder as a table — rung, evidence grade, cost,
gates, surfaces read, and what passing earns — which is the form to compare
lanes in.

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
    read    : ats           channel: greenhouse, ashby, lever
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
    OK  delivery_hiring          carried by ats   <- rung reads: ats

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
    read    : vendor_stories6 vendor story indexes (snowflake, databricks, elastic, ...)
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
    OK  independent_validation   carried by vendor_stories, community   <- rung reads: vendor_stories
    OK  stack_delivery           carried by services, case_studies, blog, code   <- rung reads: services, case_studies, blog

  FIELDS THE ROW CARRIES . preset partner-research
    claims: checklist, score, identified_practice, revenue_hypothesis, source_diversity_count, fit_tier, reasoning
    answers (14): target_stack, service_model, industry_verticals, delivery_coverage, vendor_alliances, case_study_outcome, client_logos, hiring_signals, commercial_terms, revenue_motion, third_party_mentions, engineering_output, growth_signals, evidence_categories
             required by the preset: yes
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

  RUNGS AS A TABLE

  | # | rung | evidence | cost | gates | reads | earns |
  |---|---|---|---|---|---|---|
  | 1 | **result** | `snippet` | 0 fetches | size, location | nothing | eliminates on firmographics, or earns one page visit — it never qualifies |
  | 2 | **surface** | `fetched` | pages | size, location | `about`, `services`, `careers` | qualifies the cheap gates; the stack they actually run lives here |
  | 3 | **stories** | `fetched` | pages | vertical | `case_studies`, `blog`, `news` | settles which industries the work is actually in |

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
    read    : ats           channel: greenhouse, ashby, lever
  earns     : confirms the employer from the posting page itself

  RUNGS AS A TABLE

  | # | rung | evidence | cost | gates | reads | earns |
  |---|---|---|---|---|---|---|
  | 1 | **result** | `snippet` | 0 fetches | location | nothing | drops a posting whose employer is not the kind of company this lane wants |
  | 2 | **posting** | `fetched` | pages | location | `careers`, `about`, `ats` | confirms the employer from the posting page itself |

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
    OK  delivery_hiring          carried by ats   <- rung reads: ats

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
    read    : vendor_stories6 vendor story indexes (snowflake, databricks, elastic, ...)
  earns     : settles the vertical gate, which only the case studies can answer

  RUNGS AS A TABLE

  | # | rung | evidence | cost | gates | reads | earns |
  |---|---|---|---|---|---|---|
  | 1 | **result** | `snippet` | 0 fetches | kind, size, location | nothing | eliminates a product vendor, or earns one page visit — it never qualifies |
  | 2 | **surface** | `fetched` | pages | kind, size, location | `home`, `about`, `services`, `partners`, `careers` | qualifies the cheap gates on their own pages, landing first, where a snippet may not |
  | 3 | **stories** | `fetched` | pages | vertical | `case_studies`, `partners`, `blog`, `news`, `vendor_stories` | settles the vertical gate, which only the case studies can answer |

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
    OK  independent_validation   carried by vendor_stories, community   <- rung reads: vendor_stories
    OK  stack_delivery           carried by services, case_studies, blog, code   <- rung reads: services, case_studies, blog

  FIELDS THE ROW CARRIES . preset partner-research
    claims: checklist, score, identified_practice, revenue_hypothesis, source_diversity_count, fit_tier, reasoning
    answers (14): target_stack, service_model, industry_verticals, delivery_coverage, vendor_alliances, case_study_outcome, client_logos, hiring_signals, commercial_terms, revenue_motion, third_party_mentions, engineering_output, growth_signals, evidence_categories
             required by the preset: yes
## What this says today

**No lane has a GAP any more** — every evidence kind a bar names is now carried
by a surface some rung reads. Three things changed to get there:

- **partner** — `vendor_stories` is named on the stories rung, which makes
  `tier_1`'s `independent_validation` reachable. It is cheap now: the address
  filter reads the handful of stories that could name the firm, not the whole
  index.
- **career** — `ats` is named on the posting rung, which makes `delivery_hiring`
  reachable. That bar was unreachable, which is why the lane returned single
  digits however it was tuned.
- **account and career firmographics** — the ICP and the employer profile now
  carry typed `size_min`, `size_max`, `target_territories` and
  `target_industries`, so an operator has somewhere to state theirs.

**What is still missing**, in the order I would take it:

1. **The account row carries no fields.** `account-research` defines no
   `answers`, so a scored account has a score and a gap and no verticals, no
   stack, no named clients. The ICP names them (`required_stack`,
   `trigger_pain_phrases`, `target_roles`, `anchor_logos`) and they have nowhere
   to land.
2. **The platform vendors are not excluded by name.** The classifier now fails
   a vendor's own homepage — "book a demo", "our platform", "pricing plans" beat
   "our customers" — but snowflake.com and friends enter through `{tech}` terms
   and nothing rejects the domain outright. Belt and braces: an exclusion list.
3. **GSI / RSI / SI is not a field.** The partner profile has `partner_kind`,
   and the class is derivable from headcount and territory, but nothing writes
   it onto the row.
4. **An empty profile means rung one eliminates nothing.** That is the honest
   behaviour — a gate with no threshold is not run — but a run should say so out
   loud rather than quietly walking every candidate. Right now the only place it
   shows is this document.

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
- **DAG** — the nodes the ladder compiles to, and the table each one writes. A
  `gate` puts a rung's questions; a `resolve` answers the firmographics one flat
  search can settle; a `retrieve` spends the page visits a rung declared. The
  nodes are the run: each one writes its population and the prose behind every
  verdict into the run's own database (`rung_rows`, `rung_text`), where the next
  node queries it. CSV is an export you ask for, not the store.

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
    read    : about           paths: /about, /about-us, /company
    read    : services        paths: /services, /what-we-do, /solutions...
    read    : careers         paths: /careers, /jobs, /join-us
  earns     : qualifies the cheap gates; the stack they actually run lives here
                      |
                      v  only what passed surface continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 3 . stories   evidence=fetched  (reads pages)
  gates     : vertical
    read    : case_studies    paths: /case-studies, /case-study, /our-work...
    read    : blog            paths: /blog, /insights, /resources...
    read    : news            paths: /news, /newsroom, /press
  earns     : settles which industries the work is actually in

  THE DAG THIS LADDER COMPILES TO

  g0-result     gate     0 fetches
                reads captured.jsonl   | writes rung_rows + rung_text (the text it gated on)
  x0-result     resolve  2 search(es) per open candidate
                reads g0-result        | writes rung_rows + rung_text (what the search said)
  c0-result     gate     0 fetches
                reads x0-result        | writes rung_rows + rung_text (the text it gated on)
  r1-surface    retrieve pages: about, services, careers
                reads c0-result        | writes rung_rows + rung_text + its own items table
  g1-surface    gate     0 fetches (reads what the walk brought back)
                reads r1-surface       | writes rung_rows + rung_text (the text it gated on)
  r2-stories    retrieve pages: case_studies, blog, news
                reads g1-surface       | writes rung_rows + rung_text + its own items table
  g2-stories    gate     0 fetches (reads what the walk brought back)
                reads r2-stories       | writes rung_rows + rung_text (the text it gated on)

  | node | kind | rung | asks / fields | surfaces | writes |
  |---|---|---|---|---|---|
  | `g0-result` | gate | result | size, location | - | `rung_rows`, `rung_text` |
  | `x0-result` | resolve | - | searches: location, size -> `"{name}" {field}` | - | `rung_rows`, `rung_text` |
  | `c0-result` | gate | result | size, location | - | `rung_rows`, `rung_text` |
  | `r1-surface` | retrieve | surface | reads pages | `about`, `services`, `careers` | `rung_rows`, `rung_text`, items table |
  | `g1-surface` | gate | surface | size, location | `about`, `services`, `careers` | `rung_rows`, `rung_text` |
  | `r2-stories` | retrieve | stories | reads pages | `case_studies`, `blog`, `news` | `rung_rows`, `rung_text`, items table |
  | `g2-stories` | gate | stories | vertical | `case_studies`, `blog`, `news` | `rung_rows`, `rung_text` |

  a search settles location, size at result, so no page is fetched for them

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
    read    : careers         paths: /careers, /jobs, /join-us
    read    : about           paths: /about, /about-us, /company
    read    : ats             channel: greenhouse, ashby, lever
  earns     : confirms the employer from the posting page itself

  THE DAG THIS LADDER COMPILES TO

  g0-result     gate     0 fetches
                reads captured.jsonl   | writes rung_rows + rung_text (the text it gated on)
  x0-result     resolve  1 search(es) per open candidate
                reads g0-result        | writes rung_rows + rung_text (what the search said)
  c0-result     gate     0 fetches
                reads x0-result        | writes rung_rows + rung_text (the text it gated on)
  r1-posting    retrieve pages: careers, about, ats
                reads c0-result        | writes rung_rows + rung_text + its own items table
  g1-posting    gate     0 fetches (reads what the walk brought back)
                reads r1-posting       | writes rung_rows + rung_text (the text it gated on)

  | node | kind | rung | asks / fields | surfaces | writes |
  |---|---|---|---|---|---|
  | `g0-result` | gate | result | location | - | `rung_rows`, `rung_text` |
  | `x0-result` | resolve | - | searches: location -> `"{name}" {field}` | - | `rung_rows`, `rung_text` |
  | `c0-result` | gate | result | location | - | `rung_rows`, `rung_text` |
  | `r1-posting` | retrieve | posting | reads pages | `careers`, `about`, `ats` | `rung_rows`, `rung_text`, items table |
  | `g1-posting` | gate | posting | location | `careers`, `about`, `ats` | `rung_rows`, `rung_text` |

  a search settles location at result, so no page is fetched for them

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
  bar             : tier_2
  scored with     : partner-research    output: top 25
  volume          : 34 queries, no story indexes

  +-- ENTRY ----------------------------------------------------------+
  | keyword search: 6 template(s) x 2 term set(s) -> 34 queries
  |   tech: Kafka, data platform, data engineering, dbt, Kubernetes
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
    read    : home            paths: /
    read    : about           paths: /about, /about-us, /company
    read    : services        paths: /services, /what-we-do, /solutions...
    read    : partners        paths: /partners, /partnerships, /alliances
    read    : careers         paths: /careers, /jobs, /join-us
  earns     : qualifies the cheap gates on their own pages, landing first, where a snippet may not
                      |
                      v  only what passed surface continues
──────────────────────────────────────────────────────────────────────────────
  RUNG 3 . stories   evidence=fetched  (reads pages)
  gates     : vertical
    read    : case_studies    paths: /case-studies, /case-study, /our-work...
    read    : partners        paths: /partners, /partnerships, /alliances
    read    : blog            paths: /blog, /insights, /resources...
    read    : news            paths: /news, /newsroom, /press
  earns     : settles the vertical gate on the firm's own case studies, and takes delivery proof from the outcomes they publish — a vendor's story index is where the industry lives, not a place to go looking for integrators

  THE DAG THIS LADDER COMPILES TO

  g0-result     gate     0 fetches
                reads captured.jsonl   | writes rung_rows + rung_text (the text it gated on)
  x0-result     resolve  2 search(es) per open candidate
                reads g0-result        | writes rung_rows + rung_text (what the search said)
  c0-result     gate     0 fetches
                reads x0-result        | writes rung_rows + rung_text (the text it gated on)
  r1-surface    retrieve pages: home, about, services, partners, careers
                reads c0-result        | writes rung_rows + rung_text + its own items table
  g1-surface    gate     0 fetches (reads what the walk brought back)
                reads r1-surface       | writes rung_rows + rung_text (the text it gated on)
  r2-stories    retrieve pages: case_studies, partners, blog, news
                reads g1-surface       | writes rung_rows + rung_text + its own items table
  g2-stories    gate     0 fetches (reads what the walk brought back)
                reads r2-stories       | writes rung_rows + rung_text (the text it gated on)

  | node | kind | rung | asks / fields | surfaces | writes |
  |---|---|---|---|---|---|
  | `g0-result` | gate | result | kind, size, location | - | `rung_rows`, `rung_text` |
  | `x0-result` | resolve | - | searches: location, size -> `"{name}" {field}` | - | `rung_rows`, `rung_text` |
  | `c0-result` | gate | result | kind, size, location | - | `rung_rows`, `rung_text` |
  | `r1-surface` | retrieve | surface | reads pages | `home`, `about`, `services`, `partners`, `careers` | `rung_rows`, `rung_text`, items table |
  | `g1-surface` | gate | surface | kind, size, location | `home`, `about`, `services`, `partners`, `careers` | `rung_rows`, `rung_text` |
  | `r2-stories` | retrieve | stories | reads pages | `case_studies`, `partners`, `blog`, `news` | `rung_rows`, `rung_text`, items table |
  | `g2-stories` | gate | stories | vertical | `case_studies`, `partners`, `blog`, `news` | `rung_rows`, `rung_text` |

  a search settles location, size at result, so no page is fetched for them

  RUNGS AS A TABLE

  | # | rung | evidence | cost | gates | reads | earns |
  |---|---|---|---|---|---|---|
  | 1 | **result** | `snippet` | 0 fetches | kind, size, location | nothing | eliminates a product vendor, or earns one page visit — it never qualifies |
  | 2 | **surface** | `fetched` | pages | kind, size, location | `home`, `about`, `services`, `partners`, `careers` | qualifies the cheap gates on their own pages, landing first, where a snippet may not |
  | 3 | **stories** | `fetched` | pages | vertical | `case_studies`, `partners`, `blog`, `news` | settles the vertical gate on the firm's own case studies, and takes delivery proof from the outcomes they publish — a vendor's story index is where the industry lives, not a place to go looking for integrators |

──────────────────────────────────────────────────────────────────────────────
                      v
  +-- OUT ------------------------------------------------------------+
  | eliminated : recorded with the gate and reason, never fetched
  | lead       : unresolved on something a fetch could settle, gap named
  | qualified  : every gate it could be resolved on passed
  +-------------------------------------------------------------------+
                      |  survivors, bundled per company
                      v
  BAR . tier_2 needs: delivery_proof, stack_delivery
    OK  delivery_proof           carried by case_studies, services   <- rung reads: case_studies, services
    OK  stack_delivery           carried by services, case_studies, blog, code   <- rung reads: services, case_studies, blog

  FIELDS THE ROW CARRIES . preset partner-research
    claims: checklist, score, identified_practice, revenue_hypothesis, source_diversity_count, fit_tier, reasoning
    answers (14): target_stack, service_model, industry_verticals, delivery_coverage, vendor_alliances, case_study_outcome, client_logos, hiring_signals, commercial_terms, revenue_motion, third_party_mentions, engineering_output, growth_signals, evidence_categories
             required by the preset: yes

## What this says today

**No lane has a GAP.** Every evidence kind a bar names is carried by a surface
some rung reads, and the three that were not are fixed in the lane data rather
than in the engine:

- **partner** carries the bar its own sources can prove: the floor is `tier_2`
  (`delivery_proof` + `stack_delivery`), read off the firm's case studies and
  services pages. It was `tier_1`, which also asks for
  `independent_validation` — carried only by vendor story indexes and community
  mentions, which is the ecosystem this lane looks past rather than samples.
- **career** names `ats` on the posting rung, which is what makes
  `delivery_hiring` reachable.
- **account and career firmographics** — the ICP and the employer profile carry
  typed `size_min`, `size_max`, `target_territories` and `target_industries`, so
  an operator has somewhere to state theirs.

**What is still missing**, in the order I would take it:

1. **The account row carries no fields.** `account-research` defines no
   `answers`, so a scored account has a score and a gap and no verticals, no
   stack, no named clients. The ICP names them (`required_stack`,
   `trigger_pain_phrases`, `target_roles`, `anchor_logos`) and they have nowhere
   to land.
2. **GSI / RSI / SI is not a field.** The partner profile has `partner_kind`, and
   the class is derivable from headcount and territory, but nothing writes it
   onto the row.
3. **Known firms are not excluded.** Nothing stops a run from proposing a firm
   the operator already works with; that wants a list or a rule, and it is a
   decision rather than a mechanism.

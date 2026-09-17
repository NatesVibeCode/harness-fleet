# The scoring system

Shaped by the most recent corrections. Where I inferred rather than was told, it
is marked **[inferred]** so it can be corrected rather than inherited.

---

## 1. What the system is for

**Take a world of possibilities and shrink it.**

The input is a keyword search. The output is a short list a person can act on.
Every stage exists to *eliminate*, and elimination is the cheap operation;
retrieval is the expensive one and has to be earned. A row is not a number, it
is a decision: who to approach, as what, and why.

The failure this replaces: fetching a full dossier for every candidate and then
scoring it. That pays the highest cost first, on candidates that were never
going to qualify, and it is why a run could spend forty minutes and produce
nothing.

---

## 2. The funnel

Cheapest and most decisive first. **A candidate stops at the first gate it
fails**, and nothing further is spent on it.

The funnel is a graph, not a stage inside a command: a `gate` node per rung, a
`resolve` node wherever a rung says one flat search may settle a firmographic,
and a `retrieve` node for the page visits a rung declared. Each node writes the
population it was given and the verdicts it produced (into the run's database),
so where the world shrank is a query rather than an inference. A candidate
eliminated anywhere is absent from every table above it.

| # | gate | the question | resolved from |
|---|---|---|---|
| 0 | keyword search | did it match at all | — |
| 1 | **kind / ICP** | is this the right *kind* of company | snippet, then its home page |
| 2 | **size** | can they buy or partner at this scale | snippet, about page |
| 3 | **location** | are they in territory | snippet, footer, contact page |
| 4 | **vertical** | do they serve the industries you serve | **case studies** — expensive |
| 5 | **semantics** | stack, expertise, app partners, triggers, outcomes | services, case studies, partners page |
| 6 | fields | what the row must carry | everything already fetched |

**Why this order.** A person at a company of the wrong size never gets approval,
however keen they are. So semantic evidence cannot rescue a firmographic
failure, and there is no reason to spend retrieval discovering how well-matched
the semantics are at a company that cannot buy. Firmographics decide *whether we
look*; semantics decide *what we found*.

---

## 3. Evidence grades, and the asymmetry between them

- **snippet** — a search result. Somebody else's summary of the company. Free.
- **fetched** — a page we actually read. Costs a request.

**A snippet may eliminate; only a fetched page may qualify. [inferred]**

A snippet saying "enterprise software platform" or a country outside territory
is enough to rule a company out — elimination does not need proof. A snippet
that *reads* like a good fit is a lead, not a pass: it has earned a page fetch,
nothing more. The fleet already separates `indicator` (snippet) evidence from
`fetched` evidence and treats the first as a lead; the gates use the same
distinction.

A flat search often resolves several gates at once — size, location, kind are
frequently all in one result — which is why rung 0 is worth running before any
fetch.

---

## 4. Three outcomes per gate, never two

| outcome | meaning | effect |
|---|---|---|
| **pass** | resolved, and it clears | retrieve a little more |
| **fail** | resolved, and it does not | eliminate, record the gate and reason |
| **unknown** | not determinable from what we have read | carry forward unresolved, say so |

Some attributes are easy to find and some are not — vertical usually only
appears in the case studies, headcount is often absent entirely. An unknown is
**not** a pass and **not** a failure: dropping it would be a silent zero,
passing it would invent evidence.

### Candidate verdicts

- **qualified** — passed every gate it could be resolved on
- **lead** — unresolved on something expensive, with the gap named
- **eliminated** — a gate failed, with the gate and the reason

---

## 5. Profiles — one per lane, and they lead with firmographics

Three exist and are the same pattern:

| lane | profile | population |
|---|---|---|
| accounts | `IdealCompanyProfile` | **my competitors' customers** — firms that buy in my space |
| partners | `IdealPartnerProfile` | **system integrators, not software companies** |
| career | `IdealEmployerProfile` | employers hiring for the role |

A profile declares, **in gate order**:

1. **kind** — what this lane is looking for, and what disqualifies it (a
   software company is a partner-lane disqualifier)
2. **size** — a range with a floor and a ceiling, both disqualifying
3. **locations** — territories in scope
4. **verticals** — industries served
5. **semantics** — stack, service model, adjacent competencies, app partners,
   client segment, tier
6. **disqualifiers** — explicit, and checked as early as they can be

Today the profiles hold the semantic half well (`service_models`,
`required_adjacent_competencies`, `negative_exclusions`, `target_client_segment`)
and the firmographic half barely at all — no size range, no territory, no
vertical list. **The four gates that run first have nowhere to live.** That is
the immediate gap.

**Standing, with per-run narrowing.** You keep an Ideal Partner Profile the way
you keep an Ideal Employer Profile; a run narrows it (this technology, this
vertical) without rewriting it.

---

## 6. Vocabulary: match substance, not wording

Companies do not use your words. A firm says "advisory" where the profile says
"consultancy", "banking" where it says "fintech", "London" where it says "United
Kingdom". Literal matching makes a gate fire on wording rather than on facts, and
a plainly resolvable candidate reads as unknown.

One editable synonym vocabulary covers kinds of company, industries and
territories. A gate **reports the words the page used** and decides against the
profile — "in territory (London)", not "(United Kingdom)" — so a reader can see
why it fired.

---

## 7. What a scored row carries

The fields are the product; the verdict is the door they come through.

- **identity** — domain, name
- **verdict and why** — qualified / lead / eliminated, with the gate and reason
- **firmographics** — kind, size, location
- **semantics** — verticals served, expertise and services, **app partners /
  alliances they carry**, named clients, outcomes, stack, commercial terms
- **evidence per field** — the page and the quote it came from
- **the funnel trace** — which gates ran, their outcomes, and at which evidence
  grade

Fields are extracted **from pages already fetched** — the case studies, services
and partner pages the funnel had to read anyway. This is not a separate
enrichment stage; it is not throwing away what we are already touching.

---

## 8. What the number becomes

Gate outcomes decide admission; the semantic pass produces the fields and a
recommendation — *approach as reseller / co-sell / subcontract, because they
already implement X for Y and do not carry your category*.

A 0–100 score and a tier become **derived and secondary**, not the deliverable. A
score answers "how well does this match" when the reader's question is "what do
I do about this firm".

**Where it lands.** Scoring is the last node in the graph (`s-score`), so the
number is produced where every other stage is produced, and recorded where every
other stage is recorded: the run's table, and the running list —
`entity_state.score`, `.tier`, `.facts`, with one `entity_events` row per visit.
Two consequences worth stating:

- **The tier is capped by the evidence, not by the claim.** `enforce_tier` runs
  against the coverage of the pages actually gathered, so a model claiming
  `tier_1` on one case study records as the tier that evidence supports.
- **A score is a series, not just a column.** `entity_score_history` is a view
  over the events, so "61 last month, 87 now" is a query and
  `harness-fleet ledger --trend` prints it. A score that moved means the world
  changed or the lane did, and those are different things to act on.

---

## 9. Cost model

That is the whole reason this shape is faster:

- **Most candidates die at rung 0 for zero fetches.** A snippet cannot qualify,
  but it can eliminate, and most of the world is eliminable.
- **Survivors cost 1–3 pages**: home, then the pages the next unresolved gate
  needs.
- **Deep retrieval only after the gates pass** — the case studies, the partner
  page, the hiring board.
- **Shared third-party hosts are the scarce resource**, not CPU: a hiring board,
  a code host and a community search are the same host for every candidate, so
  they are paced and quota-limited. Asking them once per candidate is what made a
  run take forty minutes; asking them only for the survivors is what makes it
  minutes.

---

## 10. Per-lane shape

**partner** — population is system integrators, explicitly not software
companies. Not sourced from vendors' customer-story indexes: those name the
firms that *buy* a product. Read *their* case studies and *their* wording for
vertical, expertise and **their app partners**. The obvious names (global SIs,
everyone already known) are wasted rows.

**account** — population is my competitors' customers. Then stack, triggers and
initiatives.

**career** — population is employers. Role, seniority, location/remote,
compensation.

---

## 11. How the funnel reports itself

The first thing a run shows is not a list of companies, it is the funnel — one
line per node, named by the gate that did the work:

```
Funnel: result: 412 in -> 180 eliminated (180 at kind) [running on the kind
  question alone: no profile states a headcount, a territory or an industry]
  | surface: read 90, nothing on 12, 268 page(s) | stories: ...
```

Every number is a threshold a person can tune, and every number is counted off
the node's own table, so the count and the rows a person reads cannot disagree.
There are no silent zeros: a gate that could not be resolved is counted as
unknown with what would resolve it, and a run whose gates are running on almost
nothing says so out loud.

The tables are rows in the run's database, and the running list across runs is
what an operator reads afterwards:

```
harness-fleet ledger                       the running list, by score
harness-fleet ledger --trend               firms with two numbers and the movement
harness-fleet ledger --entity acme.co.uk   one firm's visits, in order
harness-fleet ledger --csv running.csv     a file, when a file is wanted
```

---

## 12. Open questions

1. **Does a numeric score survive at all**, or is verdict + fields + the
   recommendation the whole output? *(Still open. It now survives as a recorded
   number with a history, which is what makes the question answerable: a score
   that never moves is not carrying information.)*
2. **Who authors profiles** — hand-maintained documents, or drafted from a
   paragraph you correct?
3. **The "known firms" exclusion** — a list you maintain, or a rule (e.g. above N
   employees, or appearing in a global-SI list)?

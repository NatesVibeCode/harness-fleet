# Harness lanes — rung and source audit

Every rung of every ladder in the three lanes (account, partner, career), and
every source the ladders and backends can reach, checked for one thing: **does it
return something of value?** If it does, it is **VALUE**. If it runs and returns
nothing, it is a **TUNE** (fixable) or a **DROP** (not viable, with the reason).
If nothing can reach it, it is **UNUSED**. Where no run has ever produced evidence
against a live site, it is **UNMEASURED** — which is a gap to close, not a verdict
to skip.

The numbers live in [quality-report.md](quality-report.md), regenerated from the
run databases by `tools/quality_report.py`. This file is the judgment on top of them.

## What the evidence is, and the caveat that matters

| Database | What it holds |
|---|---|
| `/tmp/live-probe/fleet.db` | three real quota rounds of the **partner** lane against live search and live sites — the only live (non-fixture) run in the tree |
| `/tmp/lanes-ui/ui.db` | offline fixture runs for all three lanes, all on the `demo/fake` route — nobody's site was fetched |

**The caveat is the whole story.** The live probe ran `/tmp/live-probe/lanes/partner.json`,
a **two-rung, three-surface** ladder (`result` → `surface`, surfaces `home`/`about`/`services`)
that was hand-written to keep the probe cheap. The shipped partner ladder is
**three-rung, five-surface** (`result` → `surface` → `stories`). So the only live
numbers we have describe a ladder that is not the one that ships, and the shipped
third rung — `stories` — has **no live measurement at all**. Account and career have
**no live measurement at all**. Anything this report can claim is bounded by that.

The consequence is that several verdicts below are stated as "the mechanism is
correct / incorrect" from reading the code, and "unmeasured live" from the data.
That is the honest boundary, and it is called out where it applies.

---

## The ladders, rung by rung

### Account — `result` → `surface` → `stories`

Declared rungs: `result`, `surface`, `stories`. Live reach: **none** (fixture only).
Bar: `tier_3` (`stack_delivery`). Preset: `account-research`.

| Rung | Evidence | Gates | Surfaces | Verdict |
|---|---|---|---|---|
| `result` | snippet | size, location | — | **VALUE (mechanism) / UNMEASURED (live)** |
| `surface` | fetched | size, location | about, services, careers | **VALUE (mechanism) / UNMEASURED (live)** |
| `stories` | fetched | vertical | case_studies, blog, news | **UNMEASURED — and structurally the weakest rung in the lane** |

**Judgment.**

- `result` and `surface` do exactly what a cheap-then-fetch ladder should: the
  snippet rung eliminates on firmographics (size, location) at zero fetch cost, and
  the surface rung re-puts those same gates to the entity's own pages. The fixture
  run shows both `advanced` candidates. That is correct behaviour; nothing about it
  returns "nothing".
- **`stories` is the problem, and it is structural, not a bug.** It gates on
  `vertical` and reads `case_studies`/`blog`/`news`. But the account lane's funnel
  is `allows: "any"` and its `funnel.verticals` is unset — so the `vertical` gate
  **never runs** for this lane (see `lanes.py` `_validate_funnel`: a gate only runs
  when the profile states something to gate on). A rung whose only gate can never
  fire is a rung that fetches three page classes and settles **nothing**. In the
  fixture it is declared but unreached; even if reached it would be a pass-through.
- The account lane has no `answers` schema (`account-research` defines none, per
  `funnels.md`), so `vertical`, `stack`, and named clients — the things `stories`
  is fancied to capture — have **no column to land in** even if a run reached them.

**Verdict: `stories` on account is TUNE-OR-DROP, leaning DROP as written.** Either
give account a `vertical`/`stories` gate it actually uses (and a place to write the
answer), or delete the rung so the lane stops promising to read case studies it can
never score.

---

### Partner — `result` → `surface` → `stories`

Declared rungs: `result`, `surface`, `stories`. Live reach (probe): `result`, `surface` —
`stories` **never reached**. Bar: `tier_2` (`delivery_proof` + `stack_delivery`).
Preset: `partner-research`.

| Rung | Evidence | Gates | Surfaces | Verdict |
|---|---|---|---|---|
| `result` | snippet | kind, size, location | — | **VALUE** — eliminated 5 of 35 live, advanced 30 |
| `surface` | fetched | kind, size, location | home, about, services, partners, careers | **VALUE** — eliminated 12 of 30 live, read 68 records |
| `stories` | fetched | vertical | case_studies, partners, blog, news | **UNUSED live / EMPTY in fixture** |

**Judgment.**

- `result` and `surface` are the two rungs that demonstrably earn their place in the
  only live run we have: they eliminated 5 and 12 candidates respectively, for a
  cost of one search and one page-fetch stage each. That is the funnel doing its job.
- **`stories` is the recurring failure.** Three independent facts agree:
  1. In the live probe it is simply **not there** — the probe lane only declared two
     rungs, so no live statement exists for it.
  2. In the offline fixture it **is** compiled and reached (`r2-stories`, `g2-stories`
     each 2 rows) — but `g2-stories` verdict is **EMPTY**: 2 rows and none decided
     anything. The rung read `case_studies`/`partners`/`blog`/`news` and returned
     nothing that settled `vertical`.
  3. The `advances_to` code now has the correction clauses that were previously
     reported FIXED (a survivor is advanced when a rung reads surfaces the walk never
     touched, or asks a not-yet-passed gate) — those land in `rungs.py`. But the
     fixture shows the rung reaching the population and still producing zero decisions,
     which is a **content** problem (the pages carry no vertical signal), not a
     reachability problem.

**Verdict: `stories` is TUNE, with a specific cause.** The rung is reachable but
pays nothing because the surface paths it reads (`/case-studies`, `/our-work`,
`/success-stories`, …) mostly 404 or return marketing pages for mid-market SIs, and
the `vertical` gate has thin vocabulary to match against. The cheap fix is to (a) let
`stories` settle `vertical` off the search snippet and the surface rung's own text
rather than only off fetched story pages, and (b) accept that for most integrators the
vertical signal is *absent*, which is a legitimate "unknown", not a reason to keep the
rung fetching forever.

---

### Career — `result` → `posting`

Declared rungs: `result`, `posting`. Reach: both, in the fixture. Bar: `require_kinds
= ["delivery_hiring"]` (no tier). Preset: `career-research`.

| Rung | Evidence | Gates | Surfaces | Verdict |
|---|---|---|---|---|
| `result` | snippet | location | — | **VALUE** — the only lane with a nonzero score (2 of 3, mean 100) |
| `posting` | fetched | location | careers, about, ats | **VALUE (mechanism) / UNMEASURED (live)** |

**Judgment.**

- Career is the **only** lane with a proven nonzero score, and it is a fixture. The
  demo route fed it `acme.com` and it returned 100.0 / tier_1 / verified. That proves
  the plumbing (snippet → posting → score with a `delivery_hiring` kind via the `ats`
  channel) works end to end — but proves nothing about live sources.
- `g1-posting` shows **EMPTY** in the fixture (4 rows, none decided anything). This
  is the same shape as partner's `stories`: the rung is reached but its own gate adds
  no new elimination because `location` was already settled at `result`, and the
  `delivery_hiring` evidence bar is cleared at **score**, not at the `posting` gate.
  That is not necessarily a defect — `posting`'s real job is to *confirm the employer
  from the requisition page*, and it advanced all four — but it reads as a rung that
  never decides on its own.

**Verdict: VALUE, but the lane is unmeasured live.** Career's ladder is the simplest
and the only one with a passing score; its gap is entirely in measurement, not in
mechanism.

---

## The sources

**Backends (search):** `ddgs`, `hn` are declared on every lane. `ddgs` (DuckDuckGo)
is the live workhorse — the probe's 35 candidates all came through it. `hn` (Hacker
News / Algolia) is declared but produces **no rows in any recorded run**; it is
`UNUSED` in both databases. That is not necessarily non-viable — `hn` is a legitimate
community source — but as wired today it contributes nothing, and no lane's queries
are shaped to surface hiring or integrator discussion on HN.

**First-party surfaces (what `evidence=fetched` rungs read):** the verdicts come
from the live partner probe, the only real-site evidence:

| Surface | Records | Rows | Carried nothing | Dead path/HTTP | Verdict |
|---|---|---|---|---|---|
| `services` | 44 | 17 | 11 | 6 | **VALUE** — the highest-yield page class we have |
| `home` | 23 | 22 | 8 | 0 | **VALUE** — lands first, almost always carries something |
| `about` | 1 | 1 | 28 | 5 | **TUNE/weak** — effectively dead on this population (see below) |
| `case_studies` | 0 | 0 | 0 | 30 "not named" | **UNUSED** — only the `stories` rung names it, and that rung never ran live |
| `blog` | 0 | 0 | 0 | 28 "not named" | **UNUSED** |
| `careers` | 0 | 0 | 0 | — | **UNUSED live** (partner `surface` names it, but the probe lane omitted it) |
| `partners` | 0 | 0 | 0 | — | **UNUSED** |
| `news` | 0 | 0 | 0 | — | **UNUSED** |

**`about` is the concrete finding that carries over.** Across the live probe it
returned **1 record in 34 attempts**, 28 "carried nothing", 5 dead paths — while
`services` (44) and `home` (23) did the work. Three causes, all already diagnosed and
none yet fixed in code:

1. The fallback walk stops after two guessed paths (`surface_urls(...)[:2]`), so
   `about`'s third guess (`/company`) is never tried.
2. The sitemap is requested on the bare host while fetches go to `www.`, so host
   non-canonicalisation produces "no sitemap" on sites that have one.
3. The guessed `about` paths have never been re-probed against real integrator sites
   the way the plan file's `probed` note claims for its own 8-domain sweep.

**Verdict: `about` is TUNE, not drop** — it is where headcount and territory live,
which the size/location gates need. But it is close to dead as currently wired.

**ATS / channels:** `ghreennouse`/`ashby`/`lever` are read through JSON APIs and are
the only path to `delivery_hiring` for career. They are **UNMEASURED live** — no run
has exercised a real ATS board. The `career` lane's `posting` rung names `ats`, and
`require_kinds=["delivery_hiring"]` depends on it, so this is a live gap to close,
not a drop.

**Community / vendor_stories / code / registry:** all `UNUSED` in every recorded run.
These are the surfaces the tier-1 evidence bar (independent validation) requires, and
the partner lane deliberately dropped to `tier_2` so it would not need them. They are
retained plumbing with no live evidence of value.

---

## Decisions, in the order I would take them

1. **Drop or re-target `stories` on account.** A rung gating on a `vertical` question
   this lane never asks, reading pages it has no schema to store, is decoration. Either
   give account a real vertical/stack field and a gate, or delete the rung.
2. **Tune `stories` on partner.** It is reachable (the `advances_to` fix is in) but
   pays nothing. Settle `vertical` from snippet + surface text, and treat "no vertical
   signal" as a legitimate unknown rather than a reason to keep fetching.
3. **Fix `about`'s fallback.** Try all three paths, canonicalise the host before the
   sitemap request, and re-probe the path list against integrator sites. This is the
   cheapest highest-value source fix available.
4. **Fix the `kind` gate's soft markers (still open).** `SOFTWARE_TERMS` still
   contains "our platform" / "book a demo" / "our product", and `read_kind` still gives
   a single match absolute precedence — so a consultancy whose site says "our platform"
   once is eliminated as a product vendor. The live run already mis-eliminated
   `royalcyber.com`, `nebulaworks.com`, `upperedge.com` this way. (The *reverse* half —
   therapy practices, directories, media leak through — was partially fixed via
   `DIRECTORY_TERMS` and the `DELIVERY_TERMS` "reads as no delivery → fail" clause.)
5. **Write a reason onto every zero score (still open).** The live probe produced 13
   of 14 rows scoring 0.0 with an empty `because`; a zero a reader cannot audit is a
   bug report nobody filed.
6. **Measure career and account live.** Career has the only nonzero score and it is a
   fixture; account has never been run live. Both need the same live standard partner
   already got before any of their sources can be called VIABLE.
7. **Tooling nit:** `tools/quality_report.py --json` prints the JSON and then the full
   markdown (so `--json` is not machine-consumable), and `declared_surfaces()` counts
   `first_party`/`sitemap` which are not in `source_surfaces.json`. Both are small but
   they undermine "every number here is a count of rows some node wrote" when the
   surface inventory itself drifts.

---

## Scoreboard

| Lane | Rungs | Live reach | Nonzero score | Bottom line |
|---|---|---|---|---|
| account | result, surface, stories | none (fixture only) | 0 of 4 | mechanism OK, **unmeasured live**, `stories` is decoration |
| partner | result, surface, stories | result, surface | 1 of 14, mean 10.0 | two rungs earn their keep, `stories` pays nothing, `kind` gate still mis-eliminates |
| career | result, posting | none (fixture only) | 2 of 3, mean 100.0 | only passing lane, **unmeasured live** |

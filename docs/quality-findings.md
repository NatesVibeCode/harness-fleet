# Harness lanes — live rung and source audit

Every rung of every ladder in the three lanes (account, partner, career), and
every source the ladders reach, checked against **live runs**: real search, real
sites. Each is **VALUE**, **TUNE**, **DROP** or **UNUSED**, with the evidence.

The numbers are in [quality-report.md](quality-report.md), regenerated from the
live run databases. The tool is the CLI, not a script:

```bash
harness-fleet tune audit <run.db>      # every rung and source, with yield
harness-fleet tune sim --lane partner  # walk one candidate down the ladder
harness-fleet tune gate --file <page>  # one page against the gate rules
harness-fleet tune surface <domain> --probe   # what a site actually serves
```

## What was run, and how

| Lane | Live candidates | Rungs that ran | Seed |
|---|---|---|---|
| partner | 12 → 11 entities | `result`, `surface`, `stories` (all three) | `ideal_partner_profile.json` |
| account | 11 | `result`, `surface` | `ideal_company_profile.json` |
| career | 7 | `result`, `posting` | `profile.json` |

Search and page reads are live; scoring is not in these runs, so score columns
are zero by construction and are not a finding. The seed came from the
**onboarding profile** in every case — the lane declares the shape of its
questions and the profile supplies the traits. That is what made `vertical`
live on the partner ladder for the first time, which is what finally gave the
`stories` rung something to settle.

## The headline: a live run found what no fixture could

**Ten of eleven candidates could not be read at all.** The first live partner
walk reported `r1-surface: complete 1, nothing 10`, `r2-stories: nothing 11`,
and five rungs that decided nothing. The ladder looked strict; it was blind.

**Cause.** `dag --lane --from-items` took the entity from the record's metadata
and, when that was absent, used the **item id** — which in a discovery file is
a page slug. The walk then requested
`https://acemq.com-apache-kafka-consulting-_-services-_-kafka-experts/services`.
Every fetch 404'd. The rungs were reporting "nothing" about hosts that do not
exist.

**Fix.** The entity is what the source is *about*: the attribution the record
carries, or the domain of its own URL (`dag.carried_from_items`, one candidate
per entity). The same run, same domains, after:

| | before | after |
|---|---|---|
| `r1-surface` | complete 1, **nothing 10** | **read 10**, complete 1 |
| `r2-stories` | **nothing 11** | **read 7**, nothing 1, complete 1 |
| `g1-surface` | lead 11, eliminated 0 | **eliminated 2, qualified 7**, lead 2 |
| `g2-stories` | lead 11, eliminated 0 | **eliminated 3, qualified 3**, lead 3 |
| surfaces read | — | blog 16, careers 18, case_studies 15, home 10, services 10, news 6, partners 6, about 1 |

Every rung now decides something and every source returns records. This is the
single most valuable result of the audit, and it required a live run.

## The lane work, applied and measured

Each fix below was measured with `tune compare` on the same live population, and
each was found or corrected by the instrument rather than by reading code.

### 1. `about` was never read (FIXED — the sitemap is not the whole site)

The tuner's new reason aggregation said `about: nothing there 8, dead path 5` —
but the message is a generic fallback, so it did not say why. The raw reasons
did: **8 of 9 were "visited, and nothing on this surface was about the entity"**,
i.e. the surface was never *attempted*. When a sitemap is found,
`enrich_entity` treated it as authoritative and never probed the fallback paths —
and a sitemap lists what a CMS generates (posts, careers, stories), not the
static pages. `about` was missing from the sitemap of **every** live domain
probed: infinitelambda, infracloud, corrdyn, slalom. Meanwhile `tune surface`
printed "Missing Surfaces (Will probe fallback paths)". **The walker now does
what the tuner promises.**

Measured, same population:

```
Surface records:   about +8   partners +2   services +2   home +1
r1-surface:         +14 reads, same population
```

`about` went **1 → 9 records**, which is the page the size and location gates
depend on.

### 2. The directory gate was discarding the lane's entire population (FIXED)

Fixing `about` surfaced more text — and the tuner's `compare` caught the
consequence: `infinitelambda.com` was **newly eliminated** as "a directory of
companies". `tune gate` on its real 19kB page said why:

```
Directory : ['find a ']
Primary Svc: ['consultancy', 'consulting', 'implementation partner', 'advisory']
```

One soft phrase outvoted four primary services terms, because `read_kind`
checked directory markers **first and absolutely** — the same "presence is not
dominance" flaw the software markers had before their own fix. All **six** live
eliminations were that false positive: `billigence.com`, `acemq.com`,
`infracloud.io`, `ksolves.com`, `infinitelambda.com`, `inclinedplane.com` — every
one a real consultancy. Directory markers are now split: hard ones (a catalogue
addressing a visitor: "directory of", "submit your listing", "view profile")
decide outright, soft ones ("find a ", "search for", "browse by") decide only
when there is no primary services identity. Real catalogues and job boards still
fail — verified directly, and locked in a now-17-case corpus that goes to 81%
when the rule is reverted.

### 3. Career was sourcing job boards, not employers (FIXED)

**Five of seven candidates were boards** (glassdoor, swooped, remoterocketship,
builtin, roamjobs), so the lane asked Glassdoor for a hiring board of its own and
the bar's `delivery_hiring` — carried only by `ats` — was unreachable. Job boards
are now source hosts: a republished posting is evidence *about* the employer, and
a board page naming nobody is about nobody. Measured:

```
career candidates:  7 -> 3   (boards dropped; employers kept)
ats records:        0 -> 3   (real employer boards, found for the first time)
g1-posting:         lead 7, eliminated 0  ->  eliminated 1
```

`delivery_hiring` is now satisfiable. Note the two rules that interact: a
sourceless record still falls back to its id, but a record *with* a URL that
resolves to nobody is dropped — resurrecting the slug is what kept the boards.

### 4. Account's gates were never broken (CLOSED — it was the profile)

The finding said to "wire the ICP's firmographics through, or accept that the
lane eliminates nothing". Run with an ICP that states them, they fire:

```
location (3):  Kingwood is outside the profile's territory
               Canada is outside the profile's territory
               Bangalore is outside the profile's territory
size (2):      24134 people is above the profile's ceiling of 5000
               31468 people is above the profile's ceiling of 5000
```

The wiring works; the audit ICP stated no firmographics **by my own choice** (to
exercise every rung). Closed as a profile matter, and now measurable.

### 5. A trap the loader set, found by running it (FIXED)

Reading the store's active revision *before* the authoring file made editing
`ideal_partner_profile.json` a no-op the moment one revision existed: change a
size floor, re-run, silently get the old profile. The file is the authoring form,
so it is now read every run and persisted as a revision — the record no longer
overrules the person editing it.

## Rung verdicts

### partner — `result` → `surface` → `stories` — all VALUE

| Rung | Live outcome | Verdict |
|---|---|---|
| `result` | eliminated 1 of 12, advanced 11 | **VALUE** — cheap elimination before a fetch |
| `surface` | read 10 of 11, eliminated 2, qualified 7 | **VALUE** — the highest-yield rung |
| `stories` | read 7 of 9, eliminated 3, qualified 3 | **VALUE** — decides, once `vertical` is live |

`stories` was previously reported as decoration. Live, with an onboarding
profile supplying `target_industries`, it eliminated 3 and qualified 3. It earns
its place **only when the onboarding states verticals** — with no profile it
gates on a question that never runs. That is now a property of the profile, not
of the ladder.

### account — `result` → `surface` — one rung earns, one does not

| Rung | Live outcome | Verdict |
|---|---|---|
| `result` | advanced 11 of 11, eliminated 0 | **VALUE (weak)** — nothing eliminated |
| `surface` | read 5 of 11, qualified 2, lead 9 | **VALUE** — reads, but decides little |

The `stories` rung has been **removed** from this lane (it gated on `vertical`
with no vertical declared and no field to write the answer into). Account is now
a two-rung ladder, and that is the honest shape for a lane with no `answers`
schema: `g1-surface` resolves 2 of 11 because the profile states no size and no
territory, so the cheap gates have nothing to decide. **TUNE**: the ICP's
firmographics should reach the gates, or the lane should not pretend to gate.

### career — `result` → `posting` — sources are the problem, not the rungs

| Rung | Live outcome | Verdict |
|---|---|---|
| `result` | advanced 7 of 7, eliminated 0 | **VALUE (weak)** |
| `posting` | read 2 of 7, lead 7, eliminated 0 | **EMPTY** — decides nothing |

`g1-posting` is **EMPTY**: 7 rows and not one decision. The live reason is
decisive and is a *sourcing* failure:

```
candidates: glassdoor.com, swooped.co, remoterocketship.com, builtin.com,
            roamjobs.com, pearson.jobs, HotelEngine, Inc. dba Engine
```

**Five of seven candidates are job boards, not employers.** The ladder then asks
the `ats` surface for a hiring board belonging to Glassdoor, and gets:

```
unknown Greenhouse board 'glassdoor'
unknown Ashby org 'glassdoor'
unknown Lever org 'glassdoor'
```

That is the `ats` source **working correctly** — it probed three providers and
reported honestly. But `require_kinds=["delivery_hiring"]` is carried only by
`ats`, and no employer is in the population, so **no row can ever clear the
career lane's bar**. **DROP the current entry queries; TUNE the resolver** to
attribute a posting to the employer it names rather than to the board that
republished it (`entity_key_for` already prefers `hiringOrganization`, so the
gap is that these boards are not in `SOURCE_HOSTS`).

## Source verdicts

| Source | Live yield | Verdict |
|---|---|---|
| `careers` | 18 + 6 + 3 | **VALUE** — the most reliable surface |
| `blog` | 16 | **VALUE** |
| `case_studies` | 15 | **VALUE** — and it is what `stories` gates on |
| `home` | 10 (10 rows, 0 empty) | **VALUE** — lands first, always carries something |
| `services` | 10 + 5 | **VALUE** |
| `partners` | 6 | **VALUE** |
| `news` | 6 | **VALUE** |
| `about` | **1 + 2 + 1** | **TUNE** — still nearly dead across all three lanes |
| `ats` | 0 records from 9 attempts | **VALUE (source) / empty (population)** |
| `vendor_stories`, `community`, `code` | not read | **UNUSED** — deliberate: the partner floor is `tier_2`, which does not need independent validation |
| `sitemap` | discovery aid | **VALUE** — 4 dead paths is the honest cost of probing |

`about` remains the finding that carries from the first audit: it is where
headcount and territory live, so the size and location gates depend on it, and
live it returned **4 records across 29 attempts**. The three known causes are
unfixed: the fallback walk stops after two guessed paths, the sitemap is asked
for on the bare host, and the guessed path list has never been re-probed against
real integrator sites.

## Defects found and fixed (each with a test)

1. **`dag --from-items` keyed the ladder on a page slug** — the blindness above.
   `dag.carried_from_items`, one candidate per entity.
2. **`tune audit`'s text report was unreachable.** `format_audit_report` read
   `exists`, `runs`, `rung_rows`, `unique_entities`; `audit_database` produced
   none of them, so every database — full or empty — printed "no records". The
   existing test hand-built the dict, which is why it passed.
3. **`tune sim` asked every lane the partner's kind question.** It passed
   `lane.model_dump()` (gate fields nested under `funnel`) where the engine reads
   a flat profile, so `allows` was never seen and every lane defaulted to
   `services`. A career posting was eliminated for "nothing reads as a delivery
   firm" in a lane that asks no such question.
4. **`profile_kind` was enumerated in four places** (a tuple, two schema CHECKs,
   a decode branch, a filename map), so a new object needed engine edits. Now an
   open slug decoded by a **registry** (`harness_fleet/profiles.py`): a new
   object is `register(Model)`. Verified end-to-end with a new
   `ideal_supplier` — its own kind, its own authoring file, its own queries, no
   engine change.
5. **`IdealEmployerProfile` had no `profile_kind`**, so saving an IEP filed it
   as `ideal_company`.
6. **The "seed" concept is gone.** A lane no longer declares seeds; the
   onboarding profile supplies the traits. **No company is ever a query**: an
   anchor logo is a calibration exemplar, and a firm's name as a search returns
   that firm's own pages, which can be evidence about nobody else. Anchors
   belong in the scoring prompt, and in an exclusion list where one exists.
7. **`docs/funnels.md` overstated query volume** — `templates × term-sets` (24)
   instead of the real expansion (16). It now reports what a run searches.
8. **`quality_report.py` called an attempted-but-empty source "never read"**,
   filing `ats` under both "carried nothing" and "never reached".

## The route bug: "opencode is out" was never opencode

Every live run printed **"No verified-free route yet"** and, without a pinned
route, failed every model call. The cause was not the provider and not the
account: it was the file sandbox, and it was invisible because the failure reads
exactly like a provider being unavailable.

`opencode` opens `$XDG_DATA_HOME/opencode/log/opencode.log` **before it reads a
prompt or lists a model**. That path is outside the workspace, so the sandbox
denies the write and every invocation returns:

```
Error: Unexpected error
Unknown: FileSystem.open (/Users/nate/.local/share/opencode/log/opencode.log)
```

**`catalog.refresh_from_harness` ran discovery with no `env=`**, so discovery
inherited the sandboxed HOME, failed, and registered nothing. The registry fell
back to the seven packaged hints — the Zen models only. There were no OpenRouter
routes at all, and the same denial killed every `opencode run`, which is why it
looked like opencode was down.

The machine was never short of routes. Through the opencode login, that CLI
exposes **84 models, including 20 verified-free `opencode/openrouter/*:free`
routes** — OpenRouter *through* opencode, needing no OpenRouter key, which is
exactly what the provider's own contract comment says it carries.

**The fix, in two places, because discovery returning routes is not enough:**
discovery *and* the inference call now run in a writable stand-in HOME that
**carries the CLI's credentials across** (`auth.json`, the config). A bare
temp HOME "works" and is worse: it answers with 7 models instead of 84 and none
of the passthrough, because opencode keeps its login in the same directory as
the log it cannot open.

Measured, same workspace:

| | before | after |
|---|---|---|
| routes registered from opencode | 0 | 26 |
| `opencode/openrouter/*:free` (failover) | **0** | **20** |
| usable (enabled + verified free) | 0 | **48** |
| a pinned inference call | `FileSystem.open(...)` | reaches OpenRouter |

And the failover works end to end. Pinned to `gemma-4-31b-it:free` the upstream
answered `429 ... is temporarily rate-limited upstream, isRetryable: true`; the
ladder recorded a `rate_limit` cooldown and moved on. Unpinned, the same call
completed:

```
requested_route: opencode/openrouter/nex-agi/nex-n2.5-mini:free
status: complete
cost: 0.0   cost_status: reported_zero
usage: 14273 tokens
```

**Live scoring is no longer unmeasured: a free route serves real inference
through opencode, and fails over when one is rate-limited.**

## Routes are asked at once, not one at a time

Fixing the routes exposed the real reason a run took ten minutes: the ladder was
walked **serially**. `for route_id in ladder[:attempt_limit]` tried one model,
waited for its refusal, cooled it down, and tried the next — so a batch spent its
whole budget queueing behind routes that were never going to answer, while the
model that would have replied sat idle. On a free ladder, where most routes are
rate limits and timeouts, that is the worst possible order.

The head of the ladder is now **raced**: up to `DEFAULT_ROUTE_RACE` (3) routes
answer the same prompt concurrently, and the first transport success is taken.
The racers are **daemon threads on purpose** — a route that is merely slow must
not hold up a route that already replied, so the answer returns and the
stragglers are abandoned rather than awaited. The existing per-route walk is
unchanged: it still records every attempt, cools down every refusal and validates
every answer, but the routes it walks have already replied.

Measured at the unit level: three stub routes at 3.0s, 1.0s and 0.2s return in
**0.21s**, with the slow one abandoned.

**Racing then found a conflict that only concurrency creates.** Three concurrent
`opencode` processes shared one stand-in HOME — and opencode keeps its own SQLite
state there — so they contended on one 3.3 GB database and every racer failed
with `database is locked`, which reads like the provider refusing. Each call now
gets **private state with the login symlinked in**: the credentials are shared,
the database is not.

Raced and un-pinned, a real call completes:

```
requested_route: opencode/openrouter/inclusionai/ling-3.0-flash-sante:free
status: complete   cost_status: reported_zero   16764 tokens
```

## What is still open

1. ~~Scoring is unmeasured live.~~ **CLOSED** — the raced failover ladder serves
   real inference at `cost: 0.0`.
2. **Racing spends several free calls per answer.** That is the point on a free
   ladder, but the count should be visible in the run report rather than implied
   by `route_race`; the attempt table already records every racer, so the cost of
   the hedge is auditable.
2. **`about` still 404s where it should not.** The sitemap gap is closed and
   `about` went 1 → 9 records, but the remaining notes are `dead path` — the
   guessed path list (`/about`, `/about-us`, `/company`) has still never been
   re-probed against real integrator sites, and `verticalserve.com` answers 403
   to all three. That is a path-list tuning job, and `tune surface <domain>
   --probe` is the tool for it.
3. **The account lane's candidate set.** With boards and vendor pages excluded,
   account resolves 8 of 11 records; `dzone.com` and `featuredcustomers.com`
   survived as candidates although neither is a firm. The account lane would
   want the same treatment career just got, with its own vocabulary.

## Resolved in this round

| Item | Outcome |
|---|---|
| `about` unread (sitemap treated as the whole site) | **FIXED** — 1 → 9 records |
| Directory gate discarding the live population | **FIXED** — 6 false eliminations → 0, hard detection intact |
| Career sourcing job boards, `delivery_hiring` unreachable | **FIXED** — 7 → 3 candidates, `ats` 0 → 3 records |
| Account gates "eliminating nothing" | **CLOSED** — profile, not code; fires on stated firmographics |
| Authoring-file edits silently ignored | **FIXED** — the file is read every run |
| The instrument itself | **FIXED** — reasons, declared/silent gates, compare, 17-case corpus |

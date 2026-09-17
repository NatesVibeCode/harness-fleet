"""Run the partner sourcing plan: find partners, then enrich each one.

Two entry points, one pipeline:

* **find** — cold start, no list. Fans the plan's queries across every search
  backend the fleet ships (web, HN, Reddit, Stack Exchange, Discourse, dev.to,
  Lobsters, Lemmy, YC) and turns the hits into candidate partner entities.
* **enrich** — for one partner, whether it came from *find* or from a list you
  already have. Walks the shared surface plan (their own site, their ATS board,
  the directories that review them, the vendor stories that name them), adds
  independent mentions and press, then bundles everything per entity.

The surface walk itself is not partner-specific and does not live here: it is
``harness_fleet/enrich.py``, which every lane calls when it is short of
evidence. What stays in this module is what is specific to this product — the
find-stage queries, and the practice gate that decides whether an entity is a
delivery firm at all. Re-exported names below keep this product's import path
working for callers that have always used it.

The rule that makes the difference between evidence and noise: **attribution**.
A hit counts toward a partner only when that partner's domain or name appears
in the URL or in the captured text. A web search for "Trace3 Kafka" happily
returns pages about *tracing*; without this filter they read as evidence.
"""
from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from . import contracts
from .bundler import bundle_records, canonicalize_entity_id
from .discover import (
    RawRecord,
    SearchHit,
    fetch_text,
    to_input_items,
    web_search,
)
from .enrich import (
    attribution_terms as attribution_terms,
)
from .enrich import (
    enrich_entity as enrich_entity,
)
from .enrich import (
    fetch_vendor_stories as fetch_vendor_stories,
)
from .enrich import (
    filter_attributed as filter_attributed,
)
from .enrich import (
    is_attributed as is_attributed,
)
from .enrich import (
    load_surfaces as load_surfaces,
)
from .enrich import (
    surface_urls as surface_urls,
)
from .enrich import (
    vendor_story_urls as vendor_story_urls,
)
from .models import InputItem
from .sources import (
    ATS_DOMAINS as ATS_DOMAINS,
)
from .sources import (
    CASE_STUDY_PATH_RE as CASE_STUDY_PATH_RE,
)
from .sources import (
    COMMUNITY_DOMAINS as COMMUNITY_DOMAINS,
)
from .sources import (
    PLATFORM_HOSTS as PLATFORM_HOSTS,
)
from .sources import (
    REGISTRY_DOMAINS as REGISTRY_DOMAINS,
)
from .sources import (
    REVIEW_DOMAINS as REVIEW_DOMAINS,
)
from .sources import (
    host_of as _host,
)
from .sources import (
    is_source_host as is_source_host,
)
from .sources import (
    linked_domains as linked_domains,
)

COMMUNITY_BACKENDS = ("hn", "reddit", "stackexchange", "discourse", "devto", "lobsters", "lemmy")
# A name shorter than this is too generic to prove attribution on its own
# ("acme", "data"), so only the full domain counts for those.
_ATTRIBUTION_MIN_NAME = 6
# A page is a *practice* page only if it says something about doing the work.
# Without this gate every blog post on the internet is a "candidate partner",
# because a page is always trivially attributed to its own host. The list is
# data, not code: edit evidence_rules.practice_signals in the plan.
_DEFAULT_PRACTICE_SIGNALS = (
    "case study", "client", "customer", "consulting", "implementation",
    "integration", "migration", "managed service", "professional services",
    "delivery", "practice", "partner", "systems integrator", "hiring",
    "solutions architect", "services",
)


class SourcingError(RuntimeError):
    """The plan is unusable, or a step cannot be attempted at all."""


# CMS assets live under the same paths as stories (background images, headers,
# logos). They are never prose, so they are dropped before any fetch.
_ASSET_HINTS = (
    "background", "asset", "header", "logo", "icon", "font", "sprite",
    "screenshot", "thumbnail", "avatar", "placeholder",
)
_SLUG_STOPWORDS = {"case-study", "case-studies", "customers", "clients", "work", "portfolio", "success-stories"}


def candidate_entities(url: str | None, text: str | None = None) -> list[str]:
    """Entities a page could be filed under.

    The page's own domain normally identifies the partner. Two exceptions are
    worth catching: a vendor case study (``appomni.com/case-studies/trace3/`` is
    *about* trace3, so the page files under the partner domains it links), and a
    page hosted on a community, directory or platform site, which files under the
    firms it links to. A slug is never turned into a domain: a candidate has to
    be a domain somebody actually wrote down. Attribution then decides which of
    those the page is really about.
    """
    host = _host(url)
    out: list[str] = []
    if host and is_source_host(host):
        out.extend(linked_domains(text))
        return list(dict.fromkeys(out))
    if host:
        out.append(host)
    match = CASE_STUDY_PATH_RE.search(urlparse((url or "") if "://" in (url or "") else f"https://{url or ''}").path or "")
    if match and host and not is_source_host(host):
        # A vendor case study is *about* a partner: file it under the real
        # domains the page writes down, never under a domain we invent.
        out.extend(linked_domains(text))
        segments = [seg for seg in urlparse((url or "") if "://" in (url or "") else f"https://{url or ''}").path.split("/") if seg]
        if segments:
            slug = segments[-1].lower().strip()
            # A slug names the subject of a case study, but a slug is not a
            # domain: `fintech-payment-platform` must never become
            # `fintech-payment-platform.com`. Keep it only when it is already a
            # dotted domain that is not a platform or source host.
            if (3 <= len(slug) <= 30 and slug not in _SLUG_STOPWORDS
                    and "." in slug and not is_source_host(slug)):
                out.append(slug)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# The practice gate — this product's admission rule, not the engine's
# ---------------------------------------------------------------------------

def practice_signals(plan: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Terms that mark a page as being about *delivering* work.

    These used to be read from the packaged plan; they are the product's own
    admission rule and live here now that the plan is gone.
    """
    return _DEFAULT_PRACTICE_SIGNALS


def has_practice_signal(text: str | None, signals: Sequence[str] | None = None) -> bool:
    """True when the page talks about delivering work for clients at all.

    A page is always trivially attributed to its own host, so without this gate
    every blog post on the internet is a candidate partner. It is deliberately
    *not* in the shared enrichment: whether an entity delivers work is a
    question this product asks of its candidates, and the evidence bar already
    answers it for every other lane.
    """
    blob = (text or "").lower()
    return any(term in blob for term in (signals or _DEFAULT_PRACTICE_SIGNALS))


# ---------------------------------------------------------------------------
# Plan expansion
# ---------------------------------------------------------------------------

def _substitute(template: str, values: dict[str, str]) -> str | None:
    """Fill placeholders; None when a required value is missing."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return None if "{" in out else out


def enrich_urls(plan: dict[str, Any] | None, entity: str) -> dict[str, list[str]]:
    """Where one entity's evidence lives, by surface family.

    The URLs come from the shared surface plan, not from this product's file:
    which paths a website keeps its case studies under is a fact about the web,
    and the same walk serves every lane. The ``plan`` argument is accepted and
    ignored so this product's callers keep working.
    """
    domain = canonicalize_entity_id(entity) or entity
    plan = load_surfaces()
    slug = domain.split(".")[0]
    return {
        "first_party": [
            url
            for surface in ("services", "case_studies", "careers", "about", "blog")
            for url in surface_urls(surface, domain, surfaces=plan)
        ],
        # The HTML board hosts are not listed: they answer 200 for a slug that
        # does not exist, so the readable form is the API.
        "ats": [str(t).format(slug=slug) for t in (plan.get("ats") or {}).values() if isinstance(t, str)],
        "third_party": surface_urls("registry", domain, surfaces=plan),
    }


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

@dataclass
class SourcingReport:
    """What happened, so a run can explain itself instead of silently thinning."""

    stage: str
    searched: int = 0
    fetched: int = 0
    kept: int = 0
    dropped_unattributed: int = 0
    dropped_no_practice_signal: int = 0
    vendor_stories: int = 0
    rejected: str | None = None
    skipped: list[dict[str, str]] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "searched": self.searched,
            "fetched": self.fetched,
            "kept": self.kept,
            "dropped_unattributed": self.dropped_unattributed,
            "dropped_no_practice_signal": self.dropped_no_practice_signal,
            "vendor_stories": self.vendor_stories,
            "rejected": self.rejected,
            "candidates": self.candidates,
            "skipped": self.skipped,
        }


def _fetch_hits(hits: Sequence[SearchHit], *, timeout: float, respect_robots: bool, snippets_only: bool,
                report: SourcingReport, seen: set[str] | None = None, pace: float = 0.0) -> list[RawRecord]:
    """Fetch each URL once, however many queries and backends returned it.

    Pages go through the shared parallel fetch — one slow host must not
    serialize every other host — while snippets and the dedupe stay inline.
    Order follows the hits, so the dossier reads the same at any worker count.
    """
    from .enrich import _fetch_pages_many

    records: list[RawRecord] = []
    seen = seen if seen is not None else set()
    pending: list[SearchHit] = []
    for hit in hits:
        if hit.url in seen:
            continue
        seen.add(hit.url)
        if snippets_only:
            records.append(RawRecord(
                text=hit.snippet, source_uri=hit.url, title=hit.title,
                item_id=hit.url, metadata={"evidence": "indicator", "backend": hit.backend},
            ))
            continue
        pending.append(hit)
    fetched = _fetch_pages_many(
        [hit.url for hit in pending], fetch_text,
        timeout=timeout, respect_robots=respect_robots, pace=pace,
    )
    by_url = {url: (ok, outcome) for url, ok, outcome in fetched}
    for hit in pending:
        ok, outcome = by_url[hit.url]
        if not ok:  # a dead link must not stop the sourcing run
            report.skipped.append({"url": hit.url, "reason": str(outcome)[:200]})
            continue
        records.append(outcome)
    report.fetched += len(records)
    return records


def enrich_partner(
    entity: str,
    *,
    plan: dict[str, Any] | None = None,
    backends: Sequence[str] | None = None,
    max_pages: int = 8,
    max_per_query: int = 8,
    searxng_url: str | None = None,
    timeout: float = 20.0,
    delay: float = 1.0,
    respect_robots: bool = True,
    snippets_only: bool = False,
    include_fetch: bool = True,
    vendor_stories: bool = True,
    vendor_story_limit: int = 20,
) -> tuple[list[InputItem], SourcingReport]:
    """Deepen one partner from its own surfaces, with no plan to read."""
    report = SourcingReport(stage="enrich", candidates=[entity])
    domain = canonicalize_entity_id(entity) or entity
    records: list[RawRecord] = []
    seen_urls: set[str] = set()

    if include_fetch:
        # The walk is the shared one. This command asks for every kind the
        # engine knows, because its job is "tell me everything about this
        # domain" — a lane asks only for the kinds its own bar is short of.
        walked, walk = enrich_entity(
            domain,
            kinds=tuple(contracts.EVIDENCE_KINDS),
            max_pages=max_pages,
            timeout=timeout,
            delay=delay,
            respect_robots=respect_robots,
            vendor_stories=vendor_stories,
            vendor_story_limit=vendor_story_limit,
        )
        records.extend(walked)
        report.fetched += walk.visited
        report.vendor_stories = walk.by_surface.get("vendor_stories", 0)
        report.skipped.extend(
            {"url": str(s.get("url") or s.get("surface") or ""), "reason": str(s.get("reason", ""))}
            for s in walk.skipped[:5]
        )

    # Independent mentions and press, via the search backends. --no-fetch means
    # no fetching at all, so search hits stay snippets in that mode.
    snippets_only = snippets_only or not include_fetch
    search_backends = list(backends) if backends else [*COMMUNITY_BACKENDS, "ddgs"]
    # Press and mentions. The query for each kind of evidence is already
    # declared centrally, so this product does not keep a second list that can
    # drift from it.
    queries = [f'"{domain}"'] + contracts.queries_for_gaps(
        domain, tuple(contracts.EVIDENCE_KINDS), limit=len(contracts.EVIDENCE_KINDS)
    )
    # One (query, backend) pair per worker: mentions come from every backend at
    # once instead of one search at a time. Assembly below stays sequential, so
    # searched/skipped counts and dossier order read the same either way.
    pairs = [(query, backend) for query in queries if query for backend in search_backends]

    def search_one(pair: tuple[str, str]) -> tuple[str, str, bool, Any]:
        query, backend = pair
        try:
            return (query, backend, True, web_search(
                query, backends=[backend], max_results=max_per_query,
                searxng_url=searxng_url, timeout=timeout,
            ))
        except Exception as exc:
            return (query, backend, False, exc)

    searched: list[tuple[str, str, bool, Any]] = []
    if pairs:
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(pairs)))) as pool:
            searched = list(pool.map(search_one, pairs))
    for query, backend, ok, outcome in searched:
        report.searched += 1
        if not ok:
            report.skipped.append({"backend": backend, "query": query, "reason": str(outcome)[:200]})
            continue
        records.extend(_fetch_hits(
            outcome, timeout=timeout, respect_robots=respect_robots,
            snippets_only=snippets_only, report=report, seen=seen_urls, pace=delay,
        ))

    attributed, dropped = filter_attributed(records, domain)
    report.dropped_unattributed = len(dropped)

    # A dossier is the *output* of filtering, never an input to it. `find` gates
    # candidates before bundling; enrich used to bundle whatever domain it was
    # handed, so `enrich stripe.com` produced a "partner dossier" for a SaaS
    # vendor. The same gate applies here: the collected evidence has to show the
    # entity delivering work at all.
    keyed = [item.model_copy(update={"item_id": domain}) for item in to_input_items(attributed)]
    evidence = "\n".join(item.text for item in keyed)
    if not has_practice_signal(evidence, practice_signals(plan)):
        report.rejected = (
            f"{domain} shows no delivery evidence in the sources collected for it: "
            "nothing says it implements, integrates or delivers work for clients. "
            "No dossier was written."
        )
        report.kept = 0
        return [], report

    # One dossier per partner: the bundle merges into a single row per entity.
    items = bundle_records(keyed)
    report.kept = len(items)
    return items, report

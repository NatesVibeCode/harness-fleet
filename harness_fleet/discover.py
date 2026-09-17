"""Mechanical web discovery: broad search, polite fetch, text parse, ATS intake.

This module is the mechanical counterpart to the agent-guided discovery
playbook: instead of hand-running ``site:`` queries, ``discover`` searches the
broad web, fetches hits, and parses them into :class:`InputItem` records whose
``text`` is stored verbatim so downstream quote grounding keeps working.

Search backends: ``ddgs`` metasearch, self-hosted SearXNG, HN Algolia, and the
YC company directory (all keyless). Intake: arbitrary URLs, sitemaps,
same-domain crawls, Greenhouse/Ashby/Lever JSON APIs, YC profiles.

Hard dependencies: stdlib + ``httpx`` (already required). Broad web search
(``ddgs``, MIT) and high-fidelity article extraction (``trafilatura``,
Apache-2.0, then ``readability-lxml``, Apache-2.0) light up when the optional
``discover`` extra is installed::

    pip install <distribution>[discover]

JS-heavy pages render via the optional ``js`` extra (Playwright, experimental)::

    pip install <distribution>[js] && playwright install chromium

Every entry point degrades to a clear install hint when an optional backend is
missing. Keyless structured sources (Greenhouse/Ashby JSON APIs, HN Algolia,
YC directory) work with the base install.

Evidence grades in ``metadata["evidence"]``: ``fetched`` (full page text,
grounding-grade), ``profile`` (directory/ATS structured text, indicator),
``indicator`` (search snippet, triage only — never backs tier-1 claims).
"""
from __future__ import annotations

import atexit
import csv
import ipaddress
import json
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from . import branding
from .channels import channel_hits, load_channels
from .models import InputItem
from .sources import canonicalize_entity_id, host_of, is_noise_host, is_source_host

USER_AGENT = f"{branding.CLI_NAME}-discover (+{branding.HOMEPAGE})"
DISCOVER_EXTRA = f"pip install {branding.DIST_NAME}[discover]"

HN_API = "https://hn.algolia.com/api/v1/search"
YC_API = "https://api.ycombinator.com/v0.1/companies"
SE_API = "https://api.stackexchange.com/2.3"
DEVTO_API = "https://dev.to/api"
LEMMY_DEFAULT = "https://programming.dev"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{org}"
LEVER_API = "https://api.lever.co/v0/postings/{org}?mode=json"

MAX_BYTES = 2_000_000  # per-fetch response cap
SNIPPET_CHARS = 2000  # snippet fallback / search-hit text cap


class DiscoverError(ValueError):
    pass


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    backend: str = ""


@dataclass
class RawRecord:
    """Unvalidated discovered record; converted to InputItem via to_input_items."""

    text: str
    source_uri: str
    title: str | None = None
    item_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# IDs and URLs
# ---------------------------------------------------------------------------

_ID_BAD = re.compile(r"[^A-Za-z0-9_.-]+")
_WS = re.compile(r"\s+")


def slugify_id(value: str, max_len: int = 128) -> str:
    """Map an arbitrary URL/org string to a valid InputItem id."""
    import hashlib

    slug = _ID_BAD.sub("_", value.strip()).strip("_.")
    slug = re.sub(r"_+", "_", slug) or "item"
    if len(slug) > max_len:
        digest = hashlib.sha256(slug.encode()).hexdigest()[:12]
        slug = f"{slug[: max_len - 13]}_{digest}"
    return slug


def record_id(source_uri: str, title: str | None = None) -> str:
    """Stable, readable id: domain plus title slug (falls back to domain)."""
    base = domain_of(source_uri) or source_uri
    if title and title.strip():
        words = _WS.sub(" ", title.strip()).split(" ")[:8]
        return slugify_id(f"{base}-{'-'.join(words)}")
    return slugify_id(base)


def _stable_suffix(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:8]


def domain_of(url: str) -> str:
    try:
        candidate = str(url).strip()
        parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
        host = parsed.hostname
        if not host:
            return ""
        host = host.rstrip(".").lower()
        if host.startswith("www."):
            host = host[4:]
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        return host
    except Exception:
        return ""


# Marketing/tracking query params carry no page identity; dropping them keeps
# ?utm_* variants of one page from defeating cross-backend dedupe.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "mc_cid", "mc_eid",
    "_hsenc", "_hsmi", "mkt_tok", "vero_conv", "vero_id", "snoobi_campaign",
    "pk_campaign", "pk_kwd", "piwik_campaign", "piwik_kwd", "yclid", "dclid",
})


def canonical_url(url: str) -> str:
    try:
        parts = urlparse(url.strip())
        path = parts.path.rstrip("/")
        query = "&".join(
            part for part in parts.query.split("&")
            if part and part.split("=", 1)[0].lower() not in _TRACKING_PARAMS
        )
        return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, parts.params, query, ""))
    except Exception:
        return url.strip()


def _safe_json(resp: httpx.Response, url_or_desc: str) -> Any:
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise DiscoverError(f"invalid JSON response from {url_or_desc}: {exc}") from exc


# ---------------------------------------------------------------------------
# Broad web search backends
# ---------------------------------------------------------------------------

def search_ddgs(query: str, max_results: int = 10, timeout: float = 20.0) -> list[SearchHit]:
    """Broad metasearch via ddgs (no API key). Requires the discover extra."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise DiscoverError(f"ddgs is not installed ({DISCOVER_EXTRA})") from exc
    hits: list[SearchHit] = []
    try:
        try:
            session = DDGS(timeout=timeout)
        except TypeError:
            # Older ddgs releases take no constructor timeout; they fall back to
            # their own client default, which is the best available behaviour.
            session = DDGS()
        with session as ddgs:
            for row in ddgs.text(query, max_results=max_results) or []:
                url = (row.get("href") or row.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                hits.append(SearchHit(
                    url=url,
                    title=(row.get("title") or "")[:500],
                    snippet=(row.get("body") or "")[:SNIPPET_CHARS],
                    backend="ddgs",
                ))
    except DiscoverError:
        raise
    except Exception as exc:
        if _is_empty_result(exc):
            # "No results found" is an answer, not a failure. Treating it as an
            # error made an empty query look like a dead source, retried it
            # three times and reported the wrong reason for a zero.
            return []
        raise DiscoverError(f"ddgs search failed for {query!r}: {exc}") from exc
    return hits


def _is_empty_result(exc: BaseException) -> bool:
    """Whether a backend is saying "nothing matched" rather than "I broke"."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "no results found" in text or "no results" == str(exc).strip().lower()


def search_searxng(
    query: str,
    base_url: str,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    max_pages: int = 5,
) -> list[SearchHit]:
    """Broad search via a self-hosted SearXNG instance (``/search?format=json``).

    Paginates (``pageno``) until ``max_results`` hits or an empty page.
    """
    base = base_url.rstrip("/")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        hits: list[SearchHit] = []
        for page in range(1, max_pages + 1):
            params = {"q": query, "format": "json", "language": "en", "pageno": page}
            resp = client.get(f"{base}/search", params=params)  # type: ignore[arg-type]
            # Explicit status before JSON parsing: a 503 HTML page must not
            # surface as a misleading "invalid JSON" error.
            if resp.status_code != 200:
                raise DiscoverError(f"SearXNG returned HTTP {resp.status_code} for {query!r}")
            payload = _safe_json(resp, f"SearXNG search for {query!r}")
            page_rows = payload.get("results") or []
            if not page_rows:
                break
            for row in page_rows:
                url = (row.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                hits.append(SearchHit(
                    url=url,
                    title=(row.get("title") or "")[:500],
                    snippet=(row.get("content") or "")[:SNIPPET_CHARS],
                    backend="searxng",
                ))
                if len(hits) >= max_results:
                    return hits
        return hits
    except DiscoverError:
        raise
    except Exception as exc:
        raise DiscoverError(f"SearXNG search failed for {query!r}: {exc}") from exc
    finally:
        if close:
            client.close()


def search_hn(
    query: str,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Search Hacker News (Algolia API, no key): stories, hiring threads, comments."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        resp = client.get(HN_API, params={"query": query, "hitsPerPage": max_results})
        payload = _safe_json(resp, f"HN search for {query!r}")
        hits: list[SearchHit] = []
        for row in (payload.get("hits") or [])[:max_results]:
            url = (row.get("url") or "").strip() or f"https://news.ycombinator.com/item?id={row.get('objectID')}"
            snippet = (row.get("story_text") or row.get("comment_text") or row.get("title") or "")
            hits.append(SearchHit(
                url=url,
                title=(row.get("title") or "")[:500],
                snippet=_fallback_strip(snippet)[:SNIPPET_CHARS],
                backend="hn",
            ))
        return hits
    except DiscoverError:
        raise
    except Exception as exc:
        raise DiscoverError(f"HN search failed for {query!r}: {exc}") from exc
    finally:
        if close:
            client.close()


# ---------------------------------------------------------------------------
# YC company directory (paginated public JSON API, no key)
# ---------------------------------------------------------------------------

def _yc_get_page(
    page: int,
    client: httpx.Client,
    timeout: float = 20.0,
) -> tuple[list[dict[str, Any]], int]:
    try:
        resp = client.get(YC_API, params={"page": page}, timeout=timeout)
    except Exception as exc:
        raise DiscoverError(f"YC directory fetch failed (page {page}): {exc}") from exc
    payload = _safe_json(resp, f"YC directory page {page}")
    return payload.get("companies") or [], int(payload.get("totalPages") or 1)


def _yc_matches(company: dict[str, Any], words: list[str], batch: str | None, tags: Sequence[str]) -> bool:
    if company.get("status") not in (None, "Active"):
        return False
    if batch and str(company.get("batch") or "").lower() != batch.lower():
        return False
    if tags:
        have = {str(t).lower() for t in (company.get("tags") or []) + (company.get("industries") or [])}
        if not any(t.lower() in have for t in tags):
            return False
    if words:
        haystack = " ".join([
            str(company.get("name") or ""),
            str(company.get("oneLiner") or ""),
            str(company.get("longDescription") or ""),
            " ".join(str(t) for t in (company.get("tags") or [])),
            " ".join(str(t) for t in (company.get("industries") or [])),
        ]).lower()
        if not all(w in haystack for w in words):
            return False
    return True


def _yc_profile_text(company: dict[str, Any]) -> str:
    lines = [
        f"{company.get('name', '')} ({company.get('batch', '')})",
        str(company.get("oneLiner") or ""),
        str(company.get("longDescription") or ""),
        f"Team size: {company.get('teamSize', 'unknown')}",
        f"Industries: {', '.join(company.get('industries') or [])}",
        f"Tags: {', '.join(company.get('tags') or [])}",
        f"Website: {company.get('website') or ''}",
    ]
    return "\n".join(line for line in lines if line.strip())


def search_yc(
    query: str,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    max_pages: int = 10,
) -> list[SearchHit]:
    """Search the YC directory client-side (bulk paginated API, no key).

    Hits point at company websites with the one-liner as snippet: triage
    indicators, not evidence. Use ``fetch --yc`` for full profile records.
    """
    words = [w.lower() for w in query.split() if w.strip()]
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        hits: list[SearchHit] = []
        page, total = 1, 1
        while len(hits) < max_results and page <= min(total, max_pages):
            companies, total = _yc_get_page(page, client, timeout)
            if not companies:
                break
            for company in companies:
                if not _yc_matches(company, words, None, ()):
                    continue
                url = (company.get("website") or company.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                hits.append(SearchHit(
                    url=url,
                    title=f"{company.get('name', '')} ({company.get('batch', '')})".strip()[:500],
                    snippet=str(company.get("oneLiner") or "")[:SNIPPET_CHARS],
                    backend="yc",
                ))
                if len(hits) >= max_results:
                    return hits
            page += 1
        return hits
    finally:
        if close:
            client.close()


def fetch_yc_companies(
    query: str | None = None,
    batch: str | None = None,
    tags: Sequence[str] = (),
    max_companies: int | None = None,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    max_pages: int = 20,
    delay: float = 0.2,
) -> list[RawRecord]:
    """Dump YC company profiles as indicator-grade records (directory text, no key).

    The directory is newest-first and large (~250 pages); ``max_pages`` bounds
    the scan, so narrow queries for older companies may need a bigger budget.
    """
    words = [w.lower() for w in (query or "").split() if w.strip()]
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        records: list[RawRecord] = []
        page, total = 1, 1

        def _stamp(records: list[RawRecord]) -> list[RawRecord]:
            # Coverage is load-bearing: the directory is newest-first, so a
            # capped scan may never reach older companies. Every record says
            # how much of the directory was actually seen.
            for record in records:
                record.metadata["yc_pages_scanned"] = page
                record.metadata["yc_total_pages"] = total
            return records

        while page <= min(total, max_pages):
            companies, total = _yc_get_page(page, client, timeout)
            if not companies:
                break
            for company in companies:
                if not _yc_matches(company, words, batch, tags):
                    continue
                records.append(RawRecord(
                    text=_yc_profile_text(company),
                    source_uri=company.get("url") or company.get("website") or "",
                    title=str(company.get("name") or ""),
                    item_id=slugify_id(f"yc-{company.get('slug') or company.get('id')}"),
                    metadata={
                        "source": "ycombinator",
                        "evidence": "profile",
                        "website": company.get("website") or "",
                        "batch": company.get("batch") or "",
                        "tags": list(company.get("tags") or []),
                    },
                ))
                if max_companies is not None and len(records) >= max_companies:
                    return _stamp(records)
            page += 1
            if delay > 0 and page <= min(total, max_pages):
                time.sleep(delay)
        return _stamp(records)
    finally:
        if close:
            client.close()


def _validate_backends(
    backends: Sequence[str],
    searxng_url: str | None,
    discourse_url: str | None = None,
) -> list[str]:
    channels = _workspace_channels()
    unknown = [b for b in backends if b not in BACKENDS and b not in channels]
    if unknown:
        hint = ""
        try:
            available = load_channels(Path.cwd())
            if available:
                hint = f"; workspace channels: {sorted(available)}"
        except Exception:
            pass
        raise DiscoverError(
            f"unknown search backend(s): {unknown} (choose from {sorted(BACKENDS)}{hint})"
        )
    if "searxng" in backends and not searxng_url:
        raise DiscoverError("searxng backend requires --searxng-url (self-hosted instance)")
    if "discourse" in backends and not discourse_url:
        raise DiscoverError("discourse backend requires --discourse-url (instance to search)")
    return list(backends)


def _workspace_channels() -> dict[str, Any]:
    """Channels this workspace defines, loaded once per call site."""
    from .channels import load_channels

    try:
        return load_channels(Path.cwd())
    except Exception:
        # A broken channel file must not take down the built-in backends; the
        # CLI reports it when the channel itself is asked for.
        return {}



#: Per-host spacing, so many hosts can be fetched at once while no single host is
#: hit faster than the delay asks. One global sleep made breadth expensive and
#: punished every host for the slowest one.
_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_host_guard = threading.Lock()



_source_last: dict[str, float] = {}
_source_locks: dict[str, threading.Lock] = {}
_source_guard = threading.Lock()


def _source_ready(source: str, delay: float) -> bool:
    """Claim this source's turn. One request per source at a time, spaced.

    Different sources proceed in parallel; a source is never hit twice at once,
    which is the difference between breadth and getting rate-limited.
    """
    if delay <= 0 or not source:
        return True
    with _source_guard:
        lock = _source_locks.setdefault(source, threading.Lock())
    with lock:
        gap = delay - (time.monotonic() - _source_last.get(source, 0.0))
        if gap > 0:
            time.sleep(gap)
        _source_last[source] = time.monotonic()
    return True


def _space_host(host: str, delay: float) -> None:
    """Wait until this host may be fetched again; other hosts proceed."""
    if delay <= 0 or not host:
        return
    with _host_guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        gap = delay - (time.monotonic() - _host_last.get(host, 0.0))
        if gap > 0:
            time.sleep(gap)
        _host_last[host] = time.monotonic()


#: A search surface is a network call to somebody else's service: rate limits,
#: TLS hiccups and empty-result errors are the normal weather, not a verdict on
#: the query. Retrying the same query a couple of times before recording it as a
#: failed source is the difference between "this query found nothing" and "this
#: run found nothing", which is the difference a person actually sees.
SEARCH_ATTEMPTS = 3
SEARCH_RETRY_WAIT_SEC = 2.0
#: Some free metasearch answers *zero* results for a query that has them, then
#: answers normally seconds later. A zero is therefore worth re-asking — but
#: only where a zero is known to be unreliable. A self-hosted instance or a
#: first-party API that answers empty is answering; re-asking it three times
#: just makes every run slower. The retries are bounded either way, so one query
#: that genuinely matches nothing costs a couple of requests, not an answer.
SEARCH_EMPTY_ATTEMPTS = 3
#: Backends whose empty answer is not yet an answer.
SEARCH_EMPTY_RETRY_BACKENDS = ("ddgs",)


def empty_attempts_for(backend: str, requested: int = SEARCH_EMPTY_ATTEMPTS) -> int:
    """How many times an empty answer from this backend is worth re-asking."""
    return max(1, requested) if backend in SEARCH_EMPTY_RETRY_BACKENDS else 1


def _run_backend(
    backend: str,
    query: str,
    max_results: int,
    searxng_url: str | None,
    client: httpx.Client,
    reddit_subreddits: Sequence[str] = (),
    se_tagged: Sequence[str] = (),
    se_site: str = "stackoverflow",
    discourse_url: str | None = None,
    lemmy_instance: str = LEMMY_DEFAULT,
    timeout: float = 20.0,
) -> list[SearchHit]:
    channel = _workspace_channels().get(backend)
    if channel is not None:
        return channel_hits(channel, query, max_results=max_results, timeout=timeout, client=client)
    if backend == "ddgs":
        # The other backends inherit the timeout from the shared httpx client;
        # ddgs builds its own session, so it must be told explicitly.
        return search_ddgs(query, max_results=max_results, timeout=timeout)
    if backend == "searxng":
        return search_searxng(query, base_url=searxng_url or "", max_results=max_results, client=client)
    if backend == "yc":
        return search_yc(query, max_results=max_results, client=client)
    if backend == "reddit":
        return search_reddit(query, subreddits=reddit_subreddits, max_results=max_results, client=client)
    if backend == "stackexchange":
        return search_stackexchange(query, tagged=se_tagged, site=se_site,
                                     max_results=max_results, client=client)
    if backend == "discourse":
        return search_discourse(query, base_url=discourse_url or "", max_results=max_results, client=client)
    if backend == "lobsters":
        return search_lobsters(query, max_results=max_results, client=client)
    if backend == "lemmy":
        return search_lemmy(query, instance=lemmy_instance, max_results=max_results, client=client)
    if backend == "devto":
        return search_devto(query, max_results=max_results, client=client)
    if backend == "hn":
        return search_hn(query, max_results=max_results, client=client)
    raise DiscoverError(f"search backend not wired: {backend!r}")


def run_backend_retrying(
    backend: str,
    query: str,
    *,
    attempts: int = SEARCH_ATTEMPTS,
    empty_attempts: int = SEARCH_EMPTY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> tuple[list[SearchHit], int]:
    """Ask one backend one query, retrying transient failures with backoff.

    Returns the hits and how many attempts it took. The last failure is raised
    with its own type intact, so the caller still records precisely why a query
    produced nothing — a retry never turns a dead source into a live one, it
    just stops treating weather as a verdict. An empty answer is re-asked too,
    because the same surface that answers nothing often answers fully moments
    later, and a run whose every query is empty reports no result at all.
    """
    tries = max(1, attempts)
    empty_tries = max(1, min(empty_attempts_for(backend, empty_attempts), tries))
    used = 0
    last: Exception | None = None
    for attempt in range(tries):
        used = attempt + 1
        try:
            hits = _run_backend(backend, query, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised below with its type intact
            last = exc
            if attempt + 1 < tries:
                sleep(SEARCH_RETRY_WAIT_SEC * (attempt + 1))
                continue
            raise
        if hits or used >= empty_tries:
            return hits, used
        sleep(SEARCH_RETRY_WAIT_SEC * used)
    assert last is not None  # unreachable: the loop either returns or raises
    raise last


def web_search(
    query: str,
    backends: Sequence[str] = ("ddgs", "hn"),
    max_results: int = 10,
    searxng_url: str | None = None,
    timeout: float = 20.0,
    reddit_subreddits: Sequence[str] = (),
    se_tagged: Sequence[str] = (),
    se_site: str = "stackoverflow",
    discourse_url: str | None = None,
    lemmy_instance: str = LEMMY_DEFAULT,
) -> list[SearchHit]:
    """Run one query across backends; dedupe by canonical URL, keep order."""
    backends = _validate_backends(backends, searxng_url, discourse_url)
    seen: set[str] = set()
    merged: list[SearchHit] = []
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for backend in backends:
            hits = _run_backend(backend, query, max_results, searxng_url, client,
                                reddit_subreddits, se_tagged, se_site,
                                discourse_url, lemmy_instance, timeout=timeout)
            for hit in hits:
                key = canonical_url(hit.url)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hit)
    return merged


# ---------------------------------------------------------------------------
# Polite fetch + parse
# ---------------------------------------------------------------------------

_robots_cache: dict[str, tuple[list[str], list[str]]] = {}


def _parse_robots(txt: str) -> tuple[list[str], list[str]]:
    """Parse robots.txt rules for our UA group.

    A group is consecutive ``User-agent`` lines plus their rules; it applies
    when any agent line is ``*`` (or names us). A new group starts at a
    ``User-agent`` line following a rule line. Returns (allows, disallows).
    """
    allows: list[str] = []
    disallows: list[str] = []
    applicable = False
    saw_rule = False
    for raw in txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if saw_rule:
                applicable, saw_rule = False, False
            applicable = applicable or value in ("*", f"{branding.CLI_NAME}-discover")
        elif key in ("allow", "disallow"):
            saw_rule = True
            if applicable and value:
                (allows if key == "allow" else disallows).append(value)
    return allows, disallows


def robots_allowed(url: str, client: httpx.Client, timeout: float = 10.0) -> bool:
    """Minimal robots.txt check with Allow/Disallow longest-match. Fail-open."""
    try:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in _robots_cache:
            try:
                resp = client.get(f"{origin}/robots.txt", timeout=timeout)
                _robots_cache[origin] = _parse_robots(resp.text) if resp.status_code == 200 else ([], [])
            except Exception:
                _robots_cache[origin] = ([], [])
        allows, disallows = _robots_cache[origin]
        path = parts.path or "/"
        longest_allow = max((len(a) for a in allows if path.startswith(a)), default=-1)
        longest_deny = max((len(d) for d in disallows if path.startswith(d)), default=-1)
        return longest_deny < 0 or longest_allow >= longest_deny
    except Exception:
        return True


_CHROME_HINT = re.compile(
    r"nav|menu|footer|header|sidebar|cookie|banner|breadcrumb|social|promo|advert"
    r"|cta|chat|popup|modal|overlay|subscribe|newsletter",
    re.IGNORECASE,
)
_SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search"}


_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
               "link", "meta", "param", "source", "track", "wbr"}


class _FallbackExtractor(HTMLParser):
    """Stdlib boilerplate stripper with browser-like auto-closing (unclosed <p> etc.)."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._stack: list[str] = []
        self._skipping = False
        self._skip_level = 0
        self.title = ""

    def _trigger_skip(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside", "form", "head"):
            if tag == "header" and "article" in self._stack:
                return False  # article <header> holds the real title/lede
            return True
        # Content containers outrank chrome heuristics: a "promo" class inside
        # <article>/<main> is page copy, not site chrome.
        if "article" in self._stack or "main" in self._stack:
            return False
        role = dict(attrs).get("role", "")
        if role in _SKIP_ROLES:
            return True
        if tag == "div":
            blob = " ".join([tag] + [k for k, _ in attrs] + [v or "" for _, v in attrs])
            return bool(_CHROME_HINT.search(blob))
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            return
        self._stack.append(tag)
        if not self._skipping and self._trigger_skip(tag, attrs):
            self._skipping = True
            self._skip_level = len(self._stack)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return  # <img/> style self-closes must not disturb the stack
        while self._stack and self._stack[-1] != tag:
            self._stack.pop()  # implicit close, like browsers
        if self._stack:
            self._stack.pop()
        if self._skipping and len(self._stack) < self._skip_level:
            self._skipping = False

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return "\n\n".join(self._chunks)


def _remove_hidden_blocks(html: str) -> str:
    """Drop script/style/noscript/template blocks: never page copy.

    The tag-strip rescue path below cannot tell markup from text, so hidden
    blocks must go before any regex strips tags — otherwise raw JSON-LD and
    JS leak into "verbatim source text" and can be cited as evidence.
    """
    return re.sub(
        r"<(script|style|noscript|template)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _fallback_strip(html: str) -> str:
    import html as _html

    cleaned = _remove_hidden_blocks(html)
    parser = _FallbackExtractor()
    try:
        parser.feed(cleaned)
        text = parser.get_text()
        return _html.unescape(text) if text else _html.unescape(re.sub(r"<[^>]+>", " ", cleaned))
    except Exception:
        return _html.unescape(re.sub(r"<[^>]+>", " ", cleaned))


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    import html as _html

    return _WS.sub(" ", _html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))).strip()[:500]


def _meta_description(html: str) -> str:
    """The page's own one-sentence self-description, `og:description` first.

    A consultancy's meta description usually states what kind of firm it is in
    one clean sentence — the kind gate's best evidence on the page — and the
    body extractors never see it: it lives in `<head>`, which every content
    extractor skips.
    """
    import html as _html

    patterns = (
        r'<meta\b[^>]*\bproperty="og:description"[^>]*\bcontent="([^"]*)"',
        r'<meta\b[^>]*\bcontent="([^"]*)"[^>]*\bproperty="og:description"',
        r'<meta\b[^>]*\bname="description"[^>]*\bcontent="([^"]*)"',
        r'<meta\b[^>]*\bcontent="([^"]*)"[^>]*\bname="description"',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            text = _WS.sub(" ", _html.unescape(match.group(1))).strip()
            if text:
                return text[:500]
    return ""


#: Alt texts that name nothing: kept out of the evidence so a logo grid does
#: not read as a client list. A real name ("acme-bank-logo") keeps its full
#: string — only a bare generic word is dropped.
_GENERIC_ALTS = frozenset({
    "logo", "icon", "image", "photo", "picture", "banner", "avatar",
    "thumbnail", "illustration", "spacer", "pixel", "arrow", "button",
    "background", "header", "footer",
})


def _keep_alt(alt: str) -> bool:
    """Whether an image alt names something worth quoting."""
    import html as _html

    text = _html.unescape(alt or "").strip()
    if len(text) < 3:
        return False
    return re.sub(r"[\W_]+", "", text).lower() not in _GENERIC_ALTS


def _expose_alts(html: str) -> str:
    """Rewrite images as the words they carry, so every extractor sees them.

    Partner pages show named clients as logo images: the name lives only in
    `alt`, which every content extractor drops. Rewriting the tag to its words
    keeps the verbatim string quotable in whatever extractor wins.
    """
    def _swap(match: re.Match[str]) -> str:
        alt = match.group(1) if match.group(1) is not None else match.group(2)
        return f" {alt} " if _keep_alt(alt or "") else " "

    return re.sub(
        r"<img\b[^>]*?\balt=(?:\"([^\"]*)\"|'([^']*)')[^>]*>",
        _swap, html, flags=re.IGNORECASE,
    )


_DENSITY_MIN_DOC_WORDS = 200  # a document below this is genuinely thin (JS shell, stub)
_DENSITY_MIN_CAPTURE_RATIO = 0.2  # an extractor must capture this share of a real body


def _word_count(text: str) -> int:
    """Whitespace word count; density compares like with like, not tokens."""
    return len(text.split())


def _document_word_count(html: str) -> int:
    """Word count of the whole cleaned document, tags stripped, entities decoded."""
    import html as _html

    stripped = re.sub(r"<[^>]+>", " ", _remove_hidden_blocks(html))
    return _word_count(_html.unescape(stripped))


def _captures_document(candidate: str, document_words: int) -> bool:
    """True when *candidate* plausibly represents the document it came from.

    Extractors sometimes return one widget subtree of a long page — archived
    table-era markup is the classic case — and accepting that would cite a menu
    instead of the page. Candidates from a genuinely thin document (a JS shell,
    a stub) still pass: there is nothing bigger to capture there.
    """
    if not candidate.strip():
        return False
    if document_words < _DENSITY_MIN_DOC_WORDS:
        return True
    return _word_count(candidate) >= _DENSITY_MIN_CAPTURE_RATIO * document_words


def extract_text(html: str) -> str:
    """HTML -> verbatim plain text. Prefers readability, then trafilatura, then stdlib.

    Hidden script/style blocks are removed first: their code is never page copy,
    and trafilatura would otherwise surface JSON-LD bodies as text. Readability
    goes first because it returns markup, so the chrome-aware fallback stripper
    below still sees nav/menu roles and classes; trafilatura's plain-text output
    would bypass those heuristics and leak site chrome into citable evidence.

    Every extractor's output is density-gated against the whole document:
    archived table-era pages can leave readability holding a small widget
    subtree of a page that carries thousands of words of prose, and that widget
    must not win. Rejected output falls through to the next extractor and
    finally to the chrome-aware full-document strip.
    """
    if not html.strip():
        return ""
    cleaned = _expose_alts(_remove_hidden_blocks(html))
    document_words = _document_word_count(cleaned)
    best_rejected = ""
    try:
        from readability import Document

        summary = Document(cleaned).summary()
        if summary and summary.strip():
            text = _fallback_strip(summary).strip()
            if text and _captures_document(text, document_words):
                return text
            best_rejected = max(best_rejected, text, key=len)
    except ImportError:
        pass
    except Exception:
        pass
    try:
        import trafilatura

        out = trafilatura.extract(cleaned, include_comments=False, include_tables=True)
        if out and out.strip():
            text = out.strip()
            if _captures_document(text, document_words):
                return text
            best_rejected = max(best_rejected, text, key=len)
    except ImportError:
        pass
    except Exception:
        pass
    fallback = _fallback_strip(cleaned).strip()
    # The full-document strip is the last resort. An extractor's widget is still
    # better than an empty record when that strip finds nothing.
    result = fallback or best_rejected
    # The page's self-description leads: it is verbatim page content the body
    # extractors never see (it lives in <head>), and it is usually the cleanest
    # statement of what kind of firm this is. Skipped when the body already says
    # it, so the text never states the same sentence twice.
    description = _meta_description(cleaned)
    if description and " ".join(description.split()) not in " ".join(result.split()):
        result = f"{description}\n\n{result}" if result else description
    return result

def require_playwright() -> None:
    """Fail fast with an install hint when the ``js`` extra is missing."""
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        raise DiscoverError(
            f"playwright is not installed (pip install {branding.DIST_NAME}[js] "
            "&& playwright install chromium)"
        ) from exc


#: Playwright's sync API is bound to the thread that started it, and discovery
#: fetches from a ThreadPoolExecutor, so the driver and browser are kept per
#: worker thread and reused for every page that thread renders. Launching
#: Chromium once per page was the whole cost of rendering: a run that renders a
#: hundred low-yield pages paid a hundred cold starts.
_js_local = threading.local()


def _js_browser():
    """This thread's shared ``(playwright, browser)``, launched on first use."""
    state = getattr(_js_local, "state", None)
    if state is not None:
        return state
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DiscoverError(
            f"playwright is not installed (pip install {branding.DIST_NAME}[js] "
            "&& playwright install chromium)"
        ) from exc
    driver = None
    try:
        driver = sync_playwright().start()
        browser = driver.chromium.launch()
    except Exception:
        # A failed start must not leave a half-built driver cached: the next
        # call on this thread retries rather than reusing the wreckage.
        if driver is not None:
            try:
                driver.stop()
            except Exception:
                pass
        raise
    state = (driver, browser)
    _js_local.state = state
    return state


def close_js_browser() -> None:
    """Close this thread's browser and Playwright driver, if it has one.

    Registered with :mod:`atexit`; safe to call directly (and idempotent).
    """
    state = getattr(_js_local, "state", None)
    if state is None:
        return
    driver, browser = state
    try:
        browser.close()
    except Exception:
        pass
    try:
        driver.stop()
    except Exception:
        pass
    try:
        del _js_local.state
    except AttributeError:
        pass


atexit.register(close_js_browser)


def _render_js_page(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Render a JS-heavy page, returning ``(final_url, html)``.

    ``final_url`` is the URL the browser settled on, which differs from the
    requested URL whenever the page redirects. Callers that resolve relative
    links must use it as the base.

    The browser is this thread's shared instance (see :func:`_js_browser`);
    each call still gets its own page.
    """
    try:
        _, browser = _js_browser()
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto(url, timeout=int(timeout * 1000))
            # networkidle resolves as soon as the network has been idle for
            # ~500ms; 8000 is only its cap, so this is already verdict-driven
            # and needs no fixed sleep or polling loop of our own.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            return page.url, page.content()
        finally:
            page.close()
    except DiscoverError:
        raise
    except Exception as exc:
        raise DiscoverError(f"JS render failed for {url}: {exc}") from exc


def _render_js(url: str, timeout: float = 30.0) -> str:
    """Render a JS-heavy page via Playwright (optional ``js`` extra). Experimental."""
    return _render_js_page(url, timeout=timeout)[1]


#: An HTML page this thin is a shell: either it really is a stub, or the body is
#: client-rendered and arrived after the HTTP GET. Below this many words the
#: record is worth one browser render before it is trusted (see ``fetch_text``).
AUTO_RENDER_MIN_WORDS = 80


_BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal", "instance-data"})
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _host_is_public(host: str) -> bool:
    """False for loopback, private, link-local, or cloud-metadata hosts."""
    host = host.strip().strip("[]").lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a DNS name; see _guard_url for the rebinding caveat
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _guard_url(url: str) -> None:
    """Refuse to fetch non-http(s) or private/loopback/metadata addresses.

    Discovery fetches URLs chosen by third-party search results, so this is the
    boundary that keeps a hostile hit from reading cloud metadata or a local
    service and persisting it as citable evidence. DNS names are not resolved
    here, so a name that resolves to a private address is out of scope.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise DiscoverError(f"refusing non-http(s) URL: {url}")
    if not parts.hostname or not _host_is_public(parts.hostname):
        raise DiscoverError(f"refusing private or local address: {url}")


def _http_get(
    url: str,
    client: httpx.Client,
    timeout: float,
    respect_robots: bool,
) -> tuple[str, str, bytes]:
    """Single polite GET, following redirects manually with a per-hop host guard.

    Returns (final_url, raw_content_type, capped_bytes).
    """
    current = url
    for _ in range(6):
        _guard_url(current)
        if respect_robots and not robots_allowed(current, client):
            raise DiscoverError(f"blocked by robots.txt: {current}")
        try:
            resp = client.get(current, timeout=timeout, follow_redirects=False)
        except Exception as exc:
            raise DiscoverError(f"fetch failed for {current}: {exc}") from exc
        if resp.is_redirect:
            location = resp.headers.get("location")
            if not location:
                raise DiscoverError(f"redirect without a location for {current}")
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            raise DiscoverError(f"HTTP {resp.status_code} for {current}")
        raw_header = resp.headers.get("content-type", "") or ""
        return current, raw_header, resp.content[:MAX_BYTES]
    raise DiscoverError(f"too many redirects for {url}")


def _json_ld_nodes(payload: Any) -> list[dict[str, Any]]:
    """Every JSON-LD node in a payload, flattening ``@graph`` wrappers."""
    if isinstance(payload, list):
        nodes: list[dict[str, Any]] = []
        for entry in payload:
            nodes.extend(_json_ld_nodes(entry))
        return nodes
    if not isinstance(payload, dict):
        return []
    if "@graph" in payload:
        return _json_ld_nodes(payload["@graph"])
    return [payload]


def _json_ld_employer(html: str) -> tuple[str, str]:
    """The employer a posting page names, as (name, domain-or-empty).

    A requisition syndicated onto a board carries its employer in the page's own
    structured data — ``hiringOrganization`` on a JobPosting — and that name is
    the attribution the page itself makes. Reading it is the difference between
    filing a posting under the board that republished it and filing it under the
    company doing the hiring, which is the entity a person asked about.

    Returns ``("", "")`` when the page names no employer, so an unattributed
    page keeps whatever attribution the URL gives it.
    """
    if "jobposting" not in html.lower():
        return "", ""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        try:
            payload = json.loads(block)
        except ValueError:
            continue
        for node in _json_ld_nodes(payload):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else list(types or [])
            if "jobposting" not in {str(t).lower() for t in types}:
                continue
            org = node.get("hiringOrganization")
            if isinstance(org, list):
                org = org[0] if org else None
            if isinstance(org, str):
                name = _WS.sub(" ", org).strip()
                if name:
                    return name, ""
                continue
            if not isinstance(org, dict):
                continue
            name = _WS.sub(" ", str(org.get("name") or "")).strip()
            url = str(org.get("url") or org.get("sameAs") or "").strip()
            domain = ""
            if url:
                candidate = canonicalize_entity_id(url)
                if candidate and not is_source_host(candidate):
                    domain = candidate
            if name or domain:
                return name, domain
    return "", ""


def _json_ld_description(html: str) -> str:
    """Pull a description/articleBody out of JSON-LD script blocks.

    Only consults schema.org types that carry article-like content
    (Article, BlogPosting, NewsArticle, JobPosting, Product, WebPage);
    returns the longest candidate or "".
    """
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    )
    wanted = {"article", "blogposting", "newsarticle", "jobposting", "product", "webpage", "techarticle"}
    best = ""
    for block in blocks:
        try:
            payload = json.loads(block)
        except ValueError:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        if isinstance(payload, dict) and "@graph" in payload:
            graph = payload["@graph"]
            nodes = graph if isinstance(graph, list) else [graph]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else list(types or [])
            if types and not {t.lower() for t in types if isinstance(t, str)} & wanted:
                continue
            for key in ("articleBody", "description", "text"):
                value = node.get(key)
                if isinstance(value, str):
                    value = _WS.sub(" ", value).strip()
                    if len(value) > len(best):
                        best = value
    return best


# ---------------------------------------------------------------------------
# Wayback Machine (archived captures, keyless CDX API)
# ---------------------------------------------------------------------------

WAYBACK_CDX_API = "http://web.archive.org/cdx/search/cdx"
WAYBACK_MIN_CAPTURE_BYTES = 5_000  # smaller captures are stubs, not pages
_WAYBACK_URL = re.compile(
    r"^https?://web\.archive\.org/web/(?P<timestamp>\d{4,14})(?:[a-z_]{0,3})?/(?P<original>.+)$",
    re.IGNORECASE,
)
_CDX_FIELDS = ("timestamp", "original", "statuscode", "length")


def wayback_snapshot_url(original_url: str, timestamp: str) -> str:
    """Raw-capture URL for one archived page (``id_``: Wayback chrome not injected)."""
    return f"https://web.archive.org/web/{str(timestamp).strip()}id_/{str(original_url).strip()}"


def _cdx_value(row: Sequence[Any], index: dict[str, int], *names: str) -> str:
    for name in names:
        position = index.get(name)
        if position is not None and position < len(row):
            value = str(row[position] or "").strip()
            if value:
                return value
    return ""


def _parse_wayback_cdx(payload: Any) -> list[dict[str, str]]:
    """Defensive CDX ``output=json`` -> flat rows. Malformed input is ``[]``."""
    if isinstance(payload, dict):
        payload = payload.get("rows")
    if not isinstance(payload, list):
        return []
    raw_rows = [row for row in payload if isinstance(row, (list, tuple)) and row]
    if not raw_rows:
        return []
    header = [str(cell).strip().lower() for cell in raw_rows[0]]
    if any(name in header for name in ("timestamp", "statuscode", "original")):
        raw_rows = raw_rows[1:]
    else:
        header = list(_CDX_FIELDS)
    index = {name: position for position, name in enumerate(header)}
    rows: list[dict[str, str]] = []
    for row in raw_rows:
        timestamp = _cdx_value(row, index, "timestamp")
        original = _cdx_value(row, index, "original")
        if not timestamp or not original:
            continue
        rows.append({
            "timestamp": timestamp,
            "original": original,
            "status": _cdx_value(row, index, "statuscode", "status"),
            "length": _cdx_value(row, index, "length"),
        })
    return rows


def wayback_cdx(
    url_pattern: str,
    *,
    limit: int = 50,
    collapse: str = "timestamp:6",
    status: str = "200",
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    """List archived captures of *url_pattern* via the Wayback CDX API (keyless).

    An empty, malformed or failed response is ``[]``: a missing archive must
    never crash a discovery run.
    """
    params: dict[str, Any] = {
        "url": url_pattern,
        "output": "json",
        "fl": ",".join(_CDX_FIELDS),
        "collapse": collapse,
        "limit": limit,
    }
    if status:
        params["filter"] = f"status:{status}"
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        resp = client.get(WAYBACK_CDX_API, params=params, timeout=timeout)
        if resp.status_code != 200:
            return []
        try:
            payload = resp.json()
        except Exception:
            return []
        return _parse_wayback_cdx(payload)
    except Exception:
        return []
    finally:
        if close:
            client.close()


def _cdx_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def pick_wayback_snapshots(
    rows: Sequence[dict[str, Any]] | None,
    *,
    limit: int = 5,
    since: str | None = None,
    until: str | None = None,
    min_length: int = WAYBACK_MIN_CAPTURE_BYTES,
) -> list[dict[str, Any]]:
    """Best captures from CDX rows: status-200 only, tiny stubs dropped, newest first."""
    start, end = _cdx_digits(since), _cdx_digits(until)
    picked: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        timestamp = str(row.get("timestamp") or "").strip()
        if len(_cdx_digits(timestamp)) < 4:
            continue
        status = str(row.get("status") or row.get("statuscode") or "").strip()
        if status and status != "200":
            continue
        try:
            length = int(str(row.get("length") or "0").strip() or 0)
        except ValueError:
            length = 0
        if 0 < length < min_length:
            continue
        if start and timestamp < start:
            continue
        if end and timestamp[: len(end)] > end:
            continue
        picked.append(dict(row))
    picked.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    return picked[:limit]


def wayback_snapshots(
    url_pattern: str,
    *,
    limit: int = 5,
    since: str | None = None,
    until: str | None = None,
    min_length: int = WAYBACK_MIN_CAPTURE_BYTES,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Newest usable captures for *url_pattern*."""
    rows = wayback_cdx(url_pattern, limit=max(limit * 4, limit), timeout=timeout, client=client)
    return pick_wayback_snapshots(rows, limit=limit, since=since, until=until, min_length=min_length)


def _wayback_capture(url: str) -> tuple[str, str] | None:
    """``(original_url, snapshot_timestamp)`` when *url* is a Wayback capture URL."""
    match = _WAYBACK_URL.match(str(url).strip())
    if not match:
        return None
    return match.group("original"), match.group("timestamp")


def _snapshot_iso(timestamp: str) -> str:
    """CDX timestamp -> ISO-8601 UTC capture time (partial stamps padded)."""
    digits = (re.sub(r"\D", "", timestamp) + "0101000000")[:14]
    try:
        parsed = datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return parsed.isoformat()


def _archive_metadata(url: str) -> dict[str, Any]:
    """Metadata markers for an archived capture (``{}`` for a live URL)."""
    capture = _wayback_capture(url)
    if not capture:
        return {}
    original, timestamp = capture
    metadata: dict[str, Any] = {
        "archived": True,
        "archive": "wayback",
        "snapshot_timestamp": timestamp,
        "original_url": original,
    }
    captured_at = _snapshot_iso(timestamp)
    if captured_at:
        metadata["captured_at"] = captured_at
    return metadata


def _record_from_response(final_url: str, raw_header: str, raw: bytes, source_url: str) -> RawRecord:
    """Parse one fetched response into a verbatim record. Raises DiscoverError."""
    archive = _archive_metadata(final_url) or _archive_metadata(source_url)
    content_type = raw_header.split(";")[0].strip().lower()
    if content_type == "application/pdf" or final_url.lower().endswith(".pdf"):
        text = _extract_pdf_bytes(raw)
        title = domain_of(final_url)
        return RawRecord(text=text, source_uri=final_url, title=title,
                         item_id=record_id(final_url, title),
                         metadata={"evidence": "fetched", "format": "pdf", **archive})
    if content_type == "text/plain" or final_url.endswith(".txt"):
        text = _decode_body(raw, raw_header).strip()
        if not text:
            raise DiscoverError(f"no extractable text for {source_url}")
        return RawRecord(text=text, source_uri=final_url, title=domain_of(final_url),
                         item_id=record_id(final_url), metadata={"evidence": "fetched", **archive})
    if content_type not in ("text/html", "application/xhtml+xml", ""):
        raise DiscoverError(f"unsupported content-type {content_type or 'unknown'} for {source_url}")
    html = _decode_body(raw, raw_header)
    text = extract_text(html)
    structured = ""
    if len(text) < 200:
        # Thin pages (JS shells, boilerplate-stripped bodies) often still
        # carry their content in JSON-LD; prefer it over failing the fetch.
        structured = _json_ld_description(html)
        if len(structured) > len(text):
            text = structured
    if not text:
        raise DiscoverError(f"no extractable text for {source_url}")
    title = _extract_title(html) or domain_of(final_url)
    metadata: dict[str, Any] = {"evidence": "fetched", **archive}
    # The response's own content type is what tells a caller whether re-fetching
    # the page through a browser could yield more (an HTML shell might hydrate;
    # a PDF or plain-text body cannot). It is decided here and only here, so
    # callers read it rather than re-deriving it from the header.
    metadata["content_type"] = content_type or "text/html"
    # A partner page linking snowflake.com/partners is alliance evidence
    # without another fetch: the link targets ride on the record. The page's
    # own host is excluded — site navigation is not an alliance.
    metadata["outbound_hosts"] = _outbound_hosts(html, final_url)
    description = _meta_description(html)
    if description:
        metadata["meta_description"] = description
    employer, employer_domain = _json_ld_employer(html)
    if employer_domain or employer:
        # The posting names its own employer. Attribution prefers the domain the
        # page gives (a stable id); a name with no domain is still the page's own
        # statement and beats filing the posting under the board that carried it.
        metadata["hiring_domain"] = employer_domain
        metadata["hiring_organization"] = employer
        metadata["hiring_attribution"] = "json-ld hiringOrganization"
    if structured and text == structured:
        metadata["format"] = "json-ld"
    return RawRecord(text=text, source_uri=final_url, title=title,
                     item_id=record_id(final_url, title),
                     metadata=metadata)
def _outbound_hosts(html: str, base_url: str) -> list[str]:
    """External hosts this page links to, sorted and deduped."""
    from urllib.parse import urlparse

    own = (urlparse(base_url).netloc or "").lower()
    hosts: list[str] = []
    for link in extract_links(html, base_url):
        host = (urlparse(link).netloc or "").lower()
        if not host or host == own or host in hosts:
            continue
        hosts.append(host)
    return sorted(hosts)


def _decode_body(raw: bytes, content_type: str) -> str:
    """Decode honoring an explicit charset (xml/html default utf-8, text latin-1 per RFC)."""
    match = re.search(r"charset=([\w-]+)", content_type)
    for encoding in ([match.group(1)] if match else []) + ["utf-8", "windows-1252"]:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_pdf_bytes(raw: bytes) -> str:
    """Best-effort PDF -> text via pypdf (discover extra), then pdfminer if present."""
    import logging
    from io import BytesIO

    from .input_data import looks_like_text

    # pypdf's font parser logs a "fontTools is required" warning per glyph set
    # on some PDFs, which buries the run's own output. The text still extracts;
    # only the chatter is dropped.
    for noisy in ("pypdf", "pdfminer", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n\n".join(pages).strip()
        # Parser output can still be binary garbage on scanned/odd PDFs;
        # reject it so the next backend (or a clear error) takes over.
        if text and looks_like_text(text):
            return text
    except ImportError:
        pass
    except Exception:
        pass
    try:
        from pdfminer.high_level import (
            extract_text as _pdfminer_extract,
        )

        text = (_pdfminer_extract(BytesIO(raw)) or "").strip()
        if text and looks_like_text(text):
            return text
    except ImportError:
        pass
    except Exception:
        pass
    raise DiscoverError(
        "cannot parse PDF content (install pypdf via "
        f"{DISCOVER_EXTRA}, or download the file for local --input)"
    )


def fetch_text(
    url: str,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
    max_bytes: int = MAX_BYTES,
    respect_robots: bool = True,
    render_js: bool = False,
    auto_render: bool = True,
) -> RawRecord:
    """GET a URL and parse it to a verbatim-text record. Raises DiscoverError.

    With ``auto_render`` on, an HTML response whose text is thinner than
    :data:`AUTO_RENDER_MIN_WORDS` is re-fetched once through the browser; the
    longer of the two texts wins and the record says it was rendered. The
    render is best-effort: no playwright, or any other render failure, leaves
    the HTTP record exactly as it was.
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        if respect_robots and not robots_allowed(url, client):
            raise DiscoverError(f"blocked by robots.txt: {url}")
        if render_js:
            html = _render_js(url, timeout=timeout)[:max_bytes]
            text = extract_text(html)
            if not text:
                raise DiscoverError(f"no extractable text for {url} (even rendered)")
            title = _extract_title(html) or domain_of(url)
            return RawRecord(text=text, source_uri=url, title=title,
                             item_id=record_id(url, title),
                             metadata={"evidence": "fetched", "rendered": "js", **_archive_metadata(url)})
        final_url, raw_header, raw = _http_get(url, client, timeout, respect_robots=False)
        record = _record_from_response(final_url, raw_header, raw[:max_bytes], url)
        if not auto_render or record.metadata.get("content_type") != "text/html":
            return record
        if len(record.text.split()) >= AUTO_RENDER_MIN_WORDS:
            return record
        # One render per URL at most: this is the only call site that renders a
        # low-yield page, so "at most once" is the shape of the code, not a
        # counter that could drift.
        try:
            rendered_html = _render_js(final_url, timeout=timeout)[:max_bytes]
        except DiscoverError:
            return record
        rendered_text = extract_text(rendered_html)
        if not rendered_text:
            return record
        # The browser's DOM is where the page's real links, description and
        # JSON-LD live, so the rendered text is parsed back into a record and
        # the shell's own framing (redirect target, archive, content type) is
        # layered over it. Re-parsing costs no network call.
        rendered = _record_from_response(record.source_uri, raw_header, rendered_html.encode(), url)
        if len(rendered.text) <= len(record.text):
            return record
        metadata = {**record.metadata, **rendered.metadata}
        metadata["rendered"] = "js-auto"
        metadata["render_reason"] = "low_yield"
        rendered.metadata = metadata
        return rendered
    finally:
        if close:
            client.close()


# ---------------------------------------------------------------------------
# Community sources: Reddit (Arctic Shift archive + RSS) + Hacker News threads
# ---------------------------------------------------------------------------

ARCTIC_POSTS = "https://arctic-shift.photon-reddit.com/api/posts/search"
# Arctic Shift answered HTTP 400 for every query (checked 2026-09) and reddit.com's
# own JSON answers 403 without OAuth, so the working keyless path is the Pushshift
# mirror below: full submission and comment records, dated, with permalinks.
PULLPUSH_SUBMISSIONS = "https://api.pullpush.io/reddit/search/submission/"
PULLPUSH_COMMENTS = "https://api.pullpush.io/reddit/search/comment/"
ARCTIC_COMMENTS = "https://arctic-shift.photon-reddit.com/api/comments/search"
HN_ITEM_API = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"

REDDIT_EMPTY = {"", "[removed]", "[deleted]"}
REDDIT_UA = f"{branding.CLI_NAME}-discover (+{branding.HOMEPAGE}; community research)"


def _get_with_backoff(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
    attempts: int = 4,
) -> httpx.Response:
    """GET with Retry-After/exponential backoff on 429 + Arctic Shift throttle 422s."""
    last_error = ""
    for attempt in range(attempts):
        try:
            resp = client.get(url, params=params, timeout=timeout)
        except Exception as exc:
            last_error = f"request failed for {url}: {exc}"
            resp = None
        if resp is not None:
            if resp.status_code == 200:
                return resp
            body = resp.text[:200].lower()
            retryable = resp.status_code == 429 or (
                resp.status_code == 422 and ("slow down" in body or "timeout" in body)
            )
            if not retryable:
                raise DiscoverError(f"HTTP {resp.status_code} for {url}")
            last_error = f"HTTP {resp.status_code} for {url} (throttled)"
            wait = resp.headers.get("retry-after")
            delay = float(wait) if wait and wait.isdigit() else min(2.0 ** attempt, 30.0)
        else:
            delay = min(2.0 ** attempt, 30.0)
        if attempt < attempts - 1:
            time.sleep(delay)
    raise DiscoverError(f"{last_error} after {attempts} attempts")


def _reddit_post_text(post: dict[str, Any]) -> str:
    title = str(post.get("title") or "").strip()
    selftext = str(post.get("selftext") or "").strip()
    if selftext in REDDIT_EMPTY:
        return title
    return f"{title}\n\n{selftext}".strip()


def _reddit_record(post: dict[str, Any], max_chars: int | None = None) -> RawRecord | None:
    text = _reddit_post_text(post)
    if not text:
        return None
    permalink = str(post.get("permalink") or "")
    url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
    subreddit = str(post.get("subreddit") or "")
    return RawRecord(
        text=text[:max_chars] if max_chars else text,
        source_uri=url,
        title=f"r/{subreddit}: {post.get('title') or ''}".strip()[:500],
        item_id=slugify_id(f"reddit-{post.get('id')}"),
        metadata={
            "source": "reddit",
            "evidence": "profile",
            "author": str(post.get("author") or ""),
            "subreddit": subreddit,
            "score": post.get("score"),
            "num_comments": post.get("num_comments"),
        },
    )


def search_reddit(
    query: str,
    subreddits: Sequence[str] = (),
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Full-text subreddit search via the Arctic Shift archive (keyless).

    Hits are triage indicators (title + selftext excerpt). Use ``fetch`` paths
    or Arctic post records for complete text. Respects throttling via backoff.
    """
    words = query.strip()
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": REDDIT_UA})
        close = True
    try:
        subs = [s for s in subreddits] or [""]
        per_sub = max(1, (max_results + len(subs) - 1) // len(subs))
        hits: list[SearchHit] = []
        for sub in subs:
            params: dict[str, Any] = {"query": words, "limit": min(per_sub, 100)}
            if sub:
                params["subreddit"] = sub
            try:
                resp = _get_with_backoff(client, ARCTIC_POSTS, params=params, timeout=timeout)
                posts = _safe_json(resp, ARCTIC_POSTS).get("data") or []
            except DiscoverError:
                continue
            for post in posts:
                if post.get("over_18") in (True, "True", "true"):
                    continue
                permalink = str(post.get("permalink") or "")
                url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
                if not url.startswith(("http://", "https://")):
                    continue
                snippet = _reddit_post_text(post)[:SNIPPET_CHARS]
                if not snippet:
                    continue
                hits.append(SearchHit(
                    url=url,
                    title=f"r/{post.get('subreddit')}: {post.get('title') or ''}".strip()[:500],
                    snippet=snippet,
                    backend="reddit",
                ))
                if len(hits) >= max_results:
                    return hits
        if hits:
            return hits
        # The archive is currently answering HTTP 400 for every query, so fall
        # through to the mirror rather than reporting a silent zero.
        hits = search_reddit_pullpush(
            words, subreddits=subreddits, max_results=max_results, timeout=timeout, client=client,
        )
        if hits:
            return hits
        return search_reddit_pullpush(
            words, subreddits=subreddits, max_results=max_results, timeout=timeout,
            client=client, kind="comment",
        )
    finally:
        if close:
            client.close()


def search_reddit_pullpush(
    query: str,
    subreddits: Sequence[str] = (),
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    kind: str = "submission",
) -> list[SearchHit]:
    """Full-text Reddit search through the PullPush mirror (keyless).

    Submissions give title + body; comments give the body alone, which is often
    where the actual recommendation lives ("we used X for our Kafka rollout").
    A failed or malformed response is an empty list, never an exception: one
    dead mirror must not end a discovery run. The mirror throttles shared
    quota (observed HTTP 429 after a handful of queries), so an empty result
    here can mean "try later" rather than "nothing exists".
    """
    endpoint = PULLPUSH_COMMENTS if kind == "comment" else PULLPUSH_SUBMISSIONS
    words = query.strip()
    if not words:
        return []
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": REDDIT_UA})
        close = True
    try:
        subs = [s for s in subreddits] or [""]
        per_sub = max(1, (max_results + len(subs) - 1) // len(subs))
        hits: list[SearchHit] = []
        for sub in subs:
            params: dict[str, Any] = {"q": words, "size": min(per_sub, 100), "sort": "desc"}
            if sub:
                params["subreddit"] = sub
            try:
                resp = _get_with_backoff(client, endpoint, params=params, timeout=timeout)
                rows = _safe_json(resp, endpoint).get("data") or []
            except DiscoverError:
                continue
            for row in rows:
                if row.get("over_18") in (True, "True", "true"):
                    continue
                permalink = str(row.get("permalink") or "")
                url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else str(
                    row.get("full_link") or permalink or ""
                )
                if not url.startswith(("http://", "https://")):
                    continue
                if kind == "comment":
                    snippet = str(row.get("body") or "").strip()
                    title = f"r/{row.get('subreddit')}: comment"
                else:
                    snippet = _reddit_post_text(row)
                    title = f"r/{row.get('subreddit')}: {row.get('title') or ''}".strip()
                if not snippet:
                    continue
                hits.append(SearchHit(
                    url=url, title=title[:500], snippet=snippet[:SNIPPET_CHARS],
                    backend="reddit-pullpush",
                ))
                if len(hits) >= max_results:
                    return hits
        return hits
    finally:
        if close:
            client.close()


def _feed_text(value: str | None) -> str:
    """Feed item prose as plain text: CDATA/entities decoded, markup stripped."""
    import html as _html

    raw = _html.unescape(str(value or ""))
    if "<" in raw and ">" in raw:
        raw = _fallback_strip(raw)
    return re.sub(r"\s+", " ", raw).strip()


def _feed_published(entry: Any, ns: dict[str, str]) -> str:
    """Item date as ISO-8601, from RSS pubDate or Atom published/updated."""
    for tag in ("pubDate", "published", "updated", "date"):
        node = entry.find(tag) if entry.find(tag) is not None else entry.find(f"{{{ns.get('atom', '')}}}{tag}")
        if node is None or not (node.text or "").strip():
            continue
        text = node.text.strip()
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
    return ""


# ---------------------------------------------------------------------------
# GitHub: engineering reality in a firm's own artifacts
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
GITHUB_README_CHARS = 20000  # one README is prose enough; bound it per repo


def _github_headers() -> dict[str, str]:
    """GitHub request headers; a token is optional and never required.

    Unauthenticated access allows ~60 requests/hour per IP. The connector works
    without a token and reports a throttle rather than pretending the org is
    empty; if ``GITHUB_TOKEN`` happens to be set it is used, but nothing here
    asks for one or reads a credential store.
    """
    import os

    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_get(client: httpx.Client, url: str, *, params: dict[str, Any] | None = None,
                timeout: float = 20.0) -> Any:
    """One GitHub API call, throttles surfaced as DiscoverError rather than silence."""
    resp = client.get(url, params=params, timeout=timeout, headers=_github_headers())
    if resp.status_code in (403, 429):
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining == "0" or resp.status_code == 429:
            raise DiscoverError(
                "GitHub API rate limit reached (unauthenticated allows ~60 requests/hour); "
                "set GITHUB_TOKEN or retry later"
            )
    if resp.status_code == 404:
        raise DiscoverError(f"GitHub 404 for {url}")
    if resp.status_code != 200:
        raise DiscoverError(f"HTTP {resp.status_code} for {url}")
    try:
        return resp.json()
    except ValueError as exc:
        raise DiscoverError(f"invalid JSON from {url}: {exc}") from exc


def _github_repo_record(repo: dict[str, Any], html_url: str) -> RawRecord:
    """One repo as prose: what it is, what it is written in, how alive it is."""
    pushed = str(repo.get("pushed_at") or repo.get("updated_at") or "")
    parts = [
        str(repo.get("full_name") or ""),
        str(repo.get("description") or "").strip(),
        f"Language: {repo.get('language') or 'unknown'}",
        f"Stars: {repo.get('stargazers_count') or 0}",
        f"Open issues: {repo.get('open_issues_count') or 0}",
        f"Last pushed: {pushed}",
        "Archived" if repo.get("archived") else "Active",
    ]
    metadata: dict[str, Any] = {
        "evidence": "fetched", "source": "github", "kind": "repository",
        "repo": repo.get("full_name"), "archived": bool(repo.get("archived")),
    }
    if pushed:
        metadata["captured_at"] = pushed
    return RawRecord(
        text="\n".join(part for part in parts if part),
        source_uri=str(repo.get("html_url") or html_url),
        title=str(repo.get("full_name") or "")[:500] or None,
        item_id=str(repo.get("html_url") or html_url),
        metadata=metadata,
    )


def fetch_github_org(
    org: str,
    *,
    max_repos: int = 10,
    include_readmes: bool = True,
    include_releases: bool = True,
    include_issues: bool = True,
    releases_per_repo: int = 3,
    issues_per_repo: int = 10,
    readme_chars: int = GITHUB_README_CHARS,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> tuple[list[RawRecord], list[dict[str, str]]]:
    """An org's public artifacts as records: repos, READMEs, releases, issue titles.

    This is engineering reality rather than a claim about it: what the firm
    builds, what it maintains, what it writes in a README, how it responds to
    issues. Forks are skipped (they are other people's work), archived repos are
    kept and marked, and everything carries the date it was last pushed so
    recency decay measures the artifact rather than the fetch.
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    records: list[RawRecord] = []
    skipped: list[dict[str, str]] = []
    try:
        repos: list[dict[str, Any]] = []
        for endpoint in (f"{GITHUB_API}/orgs/{org}/repos", f"{GITHUB_API}/users/{org}/repos"):
            try:
                payload = _github_get(client, endpoint, params={
                    "sort": "pushed", "direction": "desc", "per_page": max(1, min(max_repos * 2, 100)),
                    "type": "public",
                }, timeout=timeout)
            except DiscoverError as exc:
                skipped.append({"source": f"github:{org}", "reason": str(exc)[:200]})
                continue
            if isinstance(payload, list):
                repos = payload
                break
        for repo in repos:
            if not isinstance(repo, dict) or repo.get("fork"):
                continue
            if len([r for r in records if r.metadata.get("kind") == "repository"]) >= max_repos:
                break
            html_url = str(repo.get("html_url") or "")
            full_name = str(repo.get("full_name") or "")
            records.append(_github_repo_record(repo, html_url))

            if include_readmes and full_name:
                try:
                    payload = _github_get(client, f"{GITHUB_API}/repos/{full_name}/readme", timeout=timeout)
                    raw = str(payload.get("content") or "").replace("\n", "")
                    if raw:
                        import base64

                        try:
                            text = base64.b64decode(raw).decode("utf-8", errors="replace")
                        except Exception:
                            text = ""
                        if text.strip():
                            metadata: dict[str, Any] = {
                                "evidence": "fetched", "source": "github", "kind": "readme",
                                "repo": full_name,
                            }
                            pushed = str(repo.get("pushed_at") or "")
                            if pushed:
                                metadata["captured_at"] = pushed
                            records.append(RawRecord(
                                text=text[:readme_chars],
                                source_uri=f"{html_url}#readme", title=f"{full_name} README"[:500],
                                item_id=f"{html_url}#readme", metadata=metadata,
                            ))
                except DiscoverError as exc:
                    skipped.append({"source": f"github:{full_name}/readme", "reason": str(exc)[:200]})

            if include_releases and full_name and releases_per_repo > 0:
                try:
                    releases = _github_get(client, f"{GITHUB_API}/repos/{full_name}/releases", params={
                        "per_page": releases_per_repo,
                    }, timeout=timeout)
                except DiscoverError as exc:
                    skipped.append({"source": f"github:{full_name}/releases", "reason": str(exc)[:200]})
                    releases = []
                for release in releases if isinstance(releases, list) else []:
                    body = str(release.get("body") or "").strip()
                    published = str(release.get("published_at") or release.get("created_at") or "")
                    name = str(release.get("name") or release.get("tag_name") or "").strip()
                    if not (body or name):
                        continue
                    release_meta: dict[str, Any] = {
                        "evidence": "fetched", "source": "github", "kind": "release",
                        "repo": full_name,
                    }
                    if published:
                        release_meta["captured_at"] = published
                    records.append(RawRecord(
                        text=f"{full_name} {name}\n\n{body}".strip(),
                        source_uri=str(release.get("html_url") or html_url),
                        title=f"{full_name} release {name}"[:500] or None,
                        item_id=str(release.get("html_url") or f"{html_url}#release-{name}"),
                        metadata=release_meta,
                    ))

            if include_issues and full_name and issues_per_repo > 0:
                try:
                    issues = _github_get(client, f"{GITHUB_API}/repos/{full_name}/issues", params={
                        "state": "all", "per_page": issues_per_repo, "sort": "updated",
                    }, timeout=timeout)
                except DiscoverError as exc:
                    skipped.append({"source": f"github:{full_name}/issues", "reason": str(exc)[:200]})
                    issues = []
                for issue in issues if isinstance(issues, list) else []:
                    if not isinstance(issue, dict) or "pull_request" in issue:
                        continue
                    title = str(issue.get("title") or "").strip()
                    body = str(issue.get("body") or "").strip()
                    if not title:
                        continue
                    updated = str(issue.get("updated_at") or "")
                    issue_meta: dict[str, Any] = {
                        "evidence": "fetched", "source": "github", "kind": "issue",
                        "repo": full_name, "state": issue.get("state"),
                    }
                    if updated:
                        issue_meta["captured_at"] = updated
                    records.append(RawRecord(
                        text=f"{full_name}: {title}\n\n{body[:1000]}".strip(),
                        source_uri=str(issue.get("html_url") or html_url),
                        title=f"{full_name}: {title}"[:500] or None,
                        item_id=str(issue.get("html_url") or f"{html_url}#issue-{issue.get('number')}"),
                        metadata=issue_meta,
                    ))
        return records, skipped
    finally:
        if close:
            client.close()


# ---------------------------------------------------------------------------
# Package registries and documentation surfaces
# ---------------------------------------------------------------------------

PACKAGE_REGISTRIES = ("pypi", "npm", "crates", "rubygems", "maven")
# Where documentation, changelogs and release notes usually live when a firm
# publishes them. Probed, not guessed at fetch time: a candidate that answers
# 404 simply is not part of the surface.
DOC_PATH_CANDIDATES = ("/docs", "/documentation", "/changelog", "/releases", "/api-docs", "/blog")


def _registry_urls(name: str) -> dict[str, str]:
    """Registry API URLs for one package name, per ecosystem."""
    return {
        "pypi": f"https://pypi.org/pypi/{name}/json",
        "npm": f"https://registry.npmjs.org/{name}",
        "crates": f"https://crates.io/api/v1/crates/{name}",
        "rubygems": f"https://rubygems.org/api/v1/gems/{name}.json",
        "maven": f"https://search.maven.org/solrsearch/select?q=a:%22{name}%22&rows=10&wt=json",
    }


def _registry_records(registry: str, name: str, payload: Any, url: str) -> list[RawRecord]:
    """One registry's payload as prose records: what the package says it is."""
    records: list[RawRecord] = []
    text_parts: list[str] = []
    captured = ""
    # A registry entry with no description is scaffolding, not evidence: an
    # empty payload must not become a record just because it parsed.
    has_prose = False

    if registry == "pypi" and isinstance(payload, dict):
        info = payload.get("info") or {}
        has_prose = bool(str(info.get("summary") or "").strip() or str(info.get("description") or "").strip())
        text_parts = [
            f"{info.get('name') or name} {info.get('version') or ''}".strip(),
            str(info.get("summary") or ""),
            str(info.get("description") or "")[:20000],
            f"Requires: {info.get('requires_python') or 'unspecified'}",
            f"Home: {info.get('home_page') or info.get('project_url') or ''}",
        ]
        releases = payload.get("releases") or {}
        if isinstance(releases, dict) and releases:
            latest = sorted(releases)[-1]
            files = releases.get(latest) or []
            if files and isinstance(files[0], dict):
                captured = str(files[0].get("upload_time_iso_8601") or files[0].get("upload_time") or "")
    elif registry == "npm" and isinstance(payload, dict):
        has_prose = bool(str(payload.get("description") or "").strip() or str(payload.get("readme") or "").strip())
        latest = (payload.get("dist-tags") or {}).get("latest") or ""
        text_parts = [
            f"{payload.get('name') or name} {latest}".strip(),
            str(payload.get("description") or ""),
            str(payload.get("readme") or "")[:20000],
        ]
        times = payload.get("time") or {}
        captured = str(times.get(latest) or times.get("modified") or "")
    elif registry == "crates" and isinstance(payload, dict):
        crate = payload.get("crate") or {}
        has_prose = bool(str(crate.get("description") or "").strip())
        text_parts = [
            f"{crate.get('name') or name} {crate.get('newest_version') or ''}".strip(),
            str(crate.get("description") or ""),
            f"Downloads: {crate.get('downloads') or 0}",
            f"Repository: {crate.get('repository') or ''}",
        ]
        captured = str(crate.get("updated_at") or crate.get("created_at") or "")
    elif registry == "rubygems" and isinstance(payload, dict):
        has_prose = bool(str(payload.get("info") or "").strip())
        text_parts = [
            f"{payload.get('name') or name} {payload.get('version') or ''}".strip(),
            str(payload.get("info") or ""),
            f"Downloads: {payload.get('downloads') or 0}",
            f"Home: {payload.get('homepage_uri') or ''}",
        ]
        captured = str(payload.get("version_created_at") or "")
    elif registry == "maven" and isinstance(payload, dict):
        docs = ((payload.get("response") or {}).get("docs")) or []
        has_prose = bool(docs)
        text_parts = [f"Maven Central artifacts matching {name}: {len(docs)}"]
        for artifact in docs[:10]:
            if not isinstance(artifact, dict):
                continue
            text_parts.append(
                f"{artifact.get('g')}:{artifact.get('a')} {artifact.get('latestVersion') or ''} "
                f"({artifact.get('timestamp') or ''})"
            )
            if not captured and artifact.get("timestamp"):
                captured = datetime.fromtimestamp(
                    int(artifact["timestamp"]) / 1000, tz=timezone.utc
                ).isoformat()

    if not has_prose:
        return []
    text = "\n".join(part for part in text_parts if part and part.strip())
    if not text.strip():
        return []
    metadata: dict[str, Any] = {
        "evidence": "fetched", "source": f"registry:{registry}", "kind": "package", "package": name,
    }
    if captured:
        try:
            parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            metadata["captured_at"] = parsed.astimezone(timezone.utc).isoformat()
            metadata["published"] = metadata["captured_at"]
        except ValueError:
            pass
    records.append(RawRecord(
        text=text,
        source_uri=url,
        title=f"{name} ({registry})"[:500],
        item_id=url,
        metadata=metadata,
    ))
    return records


def fetch_package(
    name: str,
    *,
    registry: str = "auto",
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> tuple[list[RawRecord], list[dict[str, str]]]:
    """A published package as prose: description, README, version and date.

    What a firm publishes and how often is engineering output as behaviour
    rather than as a claim. Every registry here is a public JSON API (no key).
    ``registry="auto"`` tries the ecosystems in order and returns the first that
    knows the name; the misses are reported so an empty result is distinguishable
    from "we never asked".
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    skipped: list[dict[str, str]] = []
    try:
        candidates = [registry] if registry != "auto" else list(PACKAGE_REGISTRIES)
        for candidate in candidates:
            url = _registry_urls(name).get(candidate)
            if not url:
                skipped.append({"source": f"registry:{candidate}", "reason": f"unknown registry {candidate!r}"})
                continue
            try:
                resp = _get_with_backoff(client, url, timeout=timeout, attempts=2)
            except DiscoverError as exc:
                skipped.append({"source": f"registry:{candidate}:{name}", "reason": str(exc)[:200]})
                continue
            try:
                payload = _safe_json(resp, url)
            except DiscoverError as exc:
                skipped.append({"source": f"registry:{candidate}:{name}", "reason": str(exc)[:200]})
                continue
            records = _registry_records(candidate, name, payload, url)
            if records:
                return records, skipped
        return [], skipped
    finally:
        if close:
            client.close()


def discover_doc_urls(
    domain: str,
    *,
    paths: Sequence[str] = DOC_PATH_CANDIDATES,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Documentation and changelog URLs that actually answer for a domain.

    A firm's docs and release notes explain what it builds and how it changes;
    the paths are conventional, so they are probed rather than guessed at read
    time. A 404 is simply not part of the surface, not a failure.
    """
    host = domain.strip().rstrip("/")
    if "://" not in host:
        host = f"https://{host}"
    parsed = urlparse(host)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [f"{origin}{path}" for path in paths]
    # A docs host is not always a `docs.` subdomain: docs.python.org is the
    # documentation host itself, and prefixing it again asks DNS for
    # docs.docs.python.org, which does not exist.
    bare = parsed.netloc.split(":")[0]
    if bare and not bare.startswith("docs."):
        candidates.append(f"https://docs.{bare}")
        if not bare.startswith("www."):
            candidates.append(f"https://docs.{parsed.netloc}")
    found: list[str] = []
    skipped: list[dict[str, str]] = []
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        for url in dict.fromkeys(candidates):
            try:
                resp = client.get(url, timeout=timeout)
            except Exception as exc:
                skipped.append({"source": f"docs:{url}", "reason": str(exc)[:200]})
                continue
            if resp.status_code == 200 and "text/html" in (resp.headers.get("content-type") or "text/html"):
                found.append(url)
    finally:
        if close:
            client.close()
    return found, skipped


def fetch_feed(
    url: str,
    *,
    max_results: int = 20,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    include_transcripts: bool = True,
    follow_links: bool = True,
    max_chars: int | None = None,
    respect_robots: bool = True,
) -> list[RawRecord]:
    """RSS or Atom feed -> records carrying the item prose.

    Podcast show notes and newsletter archives are where practitioners explain
    method and judgement in their own words, and both are reachable keylessly
    through feeds. Items keep their publication date so recency decay measures
    the claim rather than the fetch. A feed usually carries only an excerpt
    (measured: 35-133 words), so by default the item link is followed for the
    full article (measured: ~3,500 words for a newsletter post) and a
    ``podcast:transcript`` link is followed as its own record when advertised.
    """
    import xml.etree.ElementTree as ET

    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        resp = _get_with_backoff(client, url, timeout=timeout)
        body = resp.text
    except DiscoverError:
        return []
    finally:
        if close:
            client.close()

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "podcast": "http://podcastindex.org/namespace/1.0",
    }
    entries = list(root.iter("item")) or list(root.iter(f"{{{ns['atom']}}}entry"))
    records: list[RawRecord] = []
    for entry in entries[: max(0, max_results)]:
        title_node = entry.find("title")
        if title_node is None:
            title_node = entry.find(f"{{{ns['atom']}}}title")
        title = _feed_text(title_node.text if title_node is not None else "")

        link = ""
        link_node = entry.find("link")
        if link_node is not None:
            link = (link_node.text or link_node.get("href") or "").strip()
        if not link:
            for candidate in entry.findall(f"{{{ns['atom']}}}link"):
                if (candidate.get("rel") or "alternate") == "alternate":
                    link = (candidate.get("href") or "").strip()
                    break

        body_text = ""
        for tag in ("encoded", "content", "description", "summary"):
            node = entry.find(tag)
            if node is None:
                node = entry.find(f"{{{ns['content']}}}{tag}") or entry.find(f"{{{ns['atom']}}}{tag}")
            if node is not None and (node.text or "").strip():
                body_text = _feed_text(node.text)
                break
        text = f"{title}\n\n{body_text}".strip() if body_text else title
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        if not text.strip():
            continue
        published = _feed_published(entry, ns)
        metadata: dict[str, Any] = {"evidence": "fetched", "source": "feed", "feed": url}
        if published:
            metadata["published"] = published
            metadata["captured_at"] = published
        emitted = False

        if follow_links and link and link != url:
            # The feed carries an excerpt; the article carries the argument.
            try:
                page = fetch_text(link, timeout=timeout, respect_robots=respect_robots)
            except Exception:
                page = None
            if page is not None and len((page.text or "").split()) > len(text.split()):
                item_meta: dict[str, Any] = {
                    "evidence": "fetched", "source": "feed-item", "feed": url,
                }
                if published:
                    item_meta["published"] = published
                    item_meta["captured_at"] = published
                records.append(RawRecord(
                    text=page.text,
                    source_uri=link,
                    title=title[:500] or None,
                    item_id=link,
                    metadata=item_meta,
                ))
                emitted = True

        if not emitted:
            # One record per item: the followed page when it carried more prose,
            # otherwise what the feed itself provided.
            records.append(RawRecord(
                text=text,
                source_uri=link or url,
                title=title[:500] or None,
                item_id=link or url,
                metadata=metadata,
            ))

        if not include_transcripts:
            continue
        for transcript in entry.findall(f"{{{ns['podcast']}}}transcript"):
            transcript_url = (transcript.get("url") or "").strip()
            if not transcript_url:
                continue
            try:
                page = fetch_text(transcript_url, timeout=timeout, respect_robots=respect_robots)
            except Exception:
                continue
            transcript_meta: dict[str, Any] = {
                "evidence": "fetched", "source": "feed-transcript",
                "feed": url, "transcript_of": link or title,
            }
            if published:
                transcript_meta["published"] = published
                transcript_meta["captured_at"] = published
            records.append(RawRecord(
                text=page.text,
                source_uri=transcript_url,
                title=f"{title} (transcript)"[:500] or None,
                item_id=transcript_url,
                metadata=transcript_meta,
            ))
    return records


def fetch_reddit_posts(
    query: str,
    subreddits: Sequence[str] = (),
    max_posts: int | None = None,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[RawRecord]:
    """Complete Arctic post records (title + full selftext) for a query."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": REDDIT_UA})
        close = True
    try:
        subs = [s for s in subreddits] or [""]
        records: list[RawRecord] = []
        for sub in subs:
            params: dict[str, Any] = {"query": query.strip(), "limit": 100}
            if sub:
                params["subreddit"] = sub
            resp = _get_with_backoff(client, ARCTIC_POSTS, params=params, timeout=timeout)
            for post in _safe_json(resp, ARCTIC_POSTS).get("data") or []:
                if post.get("over_18") in (True, "True", "true"):
                    continue
                record = _reddit_record(post)
                if record is not None:
                    records.append(record)
                if max_posts is not None and len(records) >= max_posts:
                    return records
        return records
    finally:
        if close:
            client.close()


def fetch_reddit_rss(
    subreddit: str,
    sort: str = "new",
    max_results: int = 25,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[RawRecord]:
    """Fresh subreddit posts via public RSS (selftext HTML included, keyless).

    RSS has no search; it complements Arctic Shift (archive search) with
    freshness. Reddit rate-limits RSS aggressively: backoff is built in.
    """
    import html as _html
    import xml.etree.ElementTree as ET

    if sort not in ("new", "hot", "top", "rising"):
        raise DiscoverError(f"unknown subreddit sort: {sort}")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": REDDIT_UA})
        close = True
    try:
        url = f"https://www.reddit.com/r/{subreddit}/{sort}/.rss"
        resp = _get_with_backoff(client, url, timeout=timeout)
        try:
            root = ET.fromstring(resp.content)
        except Exception as exc:
            raise DiscoverError(f"cannot parse RSS for r/{subreddit}: {exc}") from exc
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        records: list[RawRecord] = []
        for entry in root.findall("atom:entry", ns)[:max_results]:
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            raw_html = entry.findtext("atom:content", default="", namespaces=ns) or ""
            body = _fallback_strip(_html.unescape(raw_html)).strip()
            text = f"{title}\n\n{body}".strip() if body else title
            if not text:
                continue
            link = ""
            link_el = entry.find("atom:link", ns)
            if link_el is not None:
                link = link_el.get("href") or ""
            author = (entry.findtext("atom:author/atom:name", default="", namespaces=ns) or "").strip()
            post_id = ""
            entry_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
            match = re.search(r"/comments/([a-z0-9]+)/", entry_id) or re.fullmatch(r"t3_([a-z0-9]+)", entry_id)
            if match:
                post_id = match.group(1)
            records.append(RawRecord(
                text=text,
                source_uri=link,
                title=f"r/{subreddit}: {title}".strip()[:500],
                item_id=slugify_id(f"reddit-{post_id or link}"),
                metadata={"source": "reddit-rss", "evidence": "profile",
                          "author": author, "subreddit": subreddit},
            ))
        return records
    finally:
        if close:
            client.close()


def fetch_reddit_thread(
    post_id_or_url: str,
    max_comments: int = 50,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> RawRecord:
    """Full comment thread for a Reddit post via Arctic Shift (keyless)."""
    match = re.search(r"/comments/([A-Za-z0-9]+)(?:[/?#]|$)", post_id_or_url) \
        or re.search(r"redd\.it/([A-Za-z0-9]+)", post_id_or_url, re.IGNORECASE)
    post_id = match.group(1) if match else post_id_or_url.strip()
    if not re.fullmatch(r"[A-Za-z0-9]+", post_id):
        raise DiscoverError(f"not a Reddit post id or comments URL: {post_id_or_url}")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": REDDIT_UA})
        close = True
    try:
        resp = _get_with_backoff(
            client, ARCTIC_COMMENTS,
            params={"link_id": f"t3_{post_id}", "limit": min(max(max_comments * 2, 25), 500)},
            timeout=timeout,
        )
        comments = _safe_json(resp, ARCTIC_COMMENTS).get("data") or []
        kept = [c for c in comments if str(c.get("body") or "").strip() not in REDDIT_EMPTY]
        kept.sort(key=lambda c: int(c.get("score") or 0), reverse=True)
        kept = kept[:max_comments]
        if not kept:
            raise DiscoverError(f"no retrievable comments for Reddit post {post_id}")
        subreddit = str(kept[0].get("subreddit") or "")
        lines = []
        for c in kept:
            author = str(c.get("author") or "unknown")
            body = str(c.get("body") or "").strip()
            score = c.get("score", 0)
            lines.append(f"[{author} (+{score})]: {body}")
        return RawRecord(
            text="\n\n".join(lines),
            source_uri=f"https://www.reddit.com/comments/{post_id}/",
            title=f"Reddit thread r/{subreddit} ({len(kept)} comments)".strip()[:500],
            item_id=slugify_id(f"reddit-thread-{post_id}"),
            metadata={"source": "reddit", "evidence": "profile",
                      "subreddit": subreddit, "post_id": post_id},
        )
    finally:
        if close:
            client.close()

# ---------------------------------------------------------------------------
# Hacker News full threads (official Firebase API, keyless)
# ---------------------------------------------------------------------------

def _hn_clean(text: str) -> str:
    import html as _html

    return _fallback_strip(_html.unescape(text or "")).strip()


def _hn_item(item_id: str, client: httpx.Client, timeout: float) -> dict[str, Any] | None:
    try:
        resp = client.get(HN_ITEM_API.format(item_id=item_id), timeout=timeout)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def fetch_hn_thread(
    item_id_or_url: str,
    max_comments: int = 50,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> RawRecord:
    """Full HN story + top comments via Firebase (keyless). Skips dead/deleted."""
    match = re.search(r"[?&]id=(\d+)", item_id_or_url)
    item_id = match.group(1) if match else item_id_or_url.strip()
    if not item_id.isdigit():
        raise DiscoverError(f"not an HN item id or URL: {item_id_or_url}")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        story = _hn_item(item_id, client, timeout)
        if not story or story.get("type") not in ("story", "poll", "job", "comment", None):
            raise DiscoverError(f"HN item {item_id} not found")
        parts: list[str] = []
        title = str(story.get("title") or f"HN {item_id}")
        parts.append(title)
        selftext = _hn_clean(str(story.get("text") or ""))
        if selftext:
            parts.append(selftext)
        story_url = str(story.get("url") or "")
        if story_url:
            parts.append(story_url)
        queue: list = list(story.get("kids") or [])
        comments: list[str] = []
        while queue and len(comments) < max_comments:
            kid = _hn_item(str(queue.pop(0)), client, timeout)
            if not kid or kid.get("deleted") or kid.get("dead"):
                continue
            body = _hn_clean(str(kid.get("text") or ""))
            if body:
                author = str(kid.get("by") or "unknown")
                comments.append(f"[{author}]: {body}")
            queue.extend(kid.get("kids") or [])
        if comments:
            parts.append(f"{len(comments)} comments:")
            parts.extend(comments)
        text = "\n\n".join(parts).strip()
        if not text:
            raise DiscoverError(f"no retrievable text for HN item {item_id}")
        return RawRecord(
            text=text,
            source_uri=f"https://news.ycombinator.com/item?id={item_id}",
            title=title[:500],
            item_id=slugify_id(f"hn-{item_id}"),
            metadata={"source": "hackernews", "evidence": "profile",
                      "score": story.get("score"), "descendants": story.get("descendants")},
        )
    finally:
        if close:
            client.close()


def _hn_item_id_from_url(url: str) -> str | None:
    if "news.ycombinator.com" not in urlparse(url).netloc.lower():
        return None
    match = re.search(r"[?&]id=(\d+)", url)
    return match.group(1) if match else None


def _reddit_post_id_from_url(url: str) -> str | None:
    netloc = urlparse(url).netloc.lower()
    if netloc.endswith("redd.it"):
        path = urlparse(url).path.strip("/").split("/")[0]
        return path if re.fullmatch(r"[A-Za-z0-9]+", path or "") else None
    if "reddit.com" not in netloc:
        return None
    match = re.search(r"/comments/([A-Za-z0-9]+)(?:[/?#]|$)", url)
    return match.group(1) if match else None


def fetch_smart_url(
    url: str,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
    respect_robots: bool = True,
    render_js: bool = False,
) -> RawRecord:
    """Fetch one URL via the best mechanical path.

    HN item URLs resolve through Firebase (full thread, no scraping);
    Reddit comment URLs resolve through Arctic Shift (login walls defeat HTML
    fetch); everything else uses polite HTML/PDF fetch.
    """
    hn_id = _hn_item_id_from_url(url)
    if hn_id:
        return fetch_hn_thread(hn_id, client=client, timeout=timeout)
    reddit_id = _reddit_post_id_from_url(url)
    if reddit_id:
        return fetch_reddit_thread(reddit_id, client=client, timeout=timeout)
    return fetch_text(url, client=client, timeout=timeout,
                      respect_robots=respect_robots, render_js=render_js)


# ---------------------------------------------------------------------------
# Q&A + forums: Stack Exchange, Discourse, Lobsters, Lemmy, Dev.to (all keyless)
# ---------------------------------------------------------------------------

LAST_SE_QUOTA: dict[str, Any] = {}


def _se_get(
    path: str,
    params: dict[str, Any],
    timeout: float,
    client: httpx.Client,
) -> dict[str, Any]:
    try:
        resp = client.get(f"{SE_API}{path}", params=params, timeout=timeout)
    except Exception as exc:
        raise DiscoverError(f"Stack Exchange request failed: {exc}") from exc
    if resp.status_code != 200:
        raise DiscoverError(f"Stack Exchange HTTP {resp.status_code} ({resp.text[:150]})")
    payload = _safe_json(resp, f"Stack Exchange {path}")
    if "error_id" in payload:
        raise DiscoverError(f"Stack Exchange error {payload.get('error_id')}: {payload.get('error_message')}")
    LAST_SE_QUOTA.update(quota_remaining=payload.get("quota_remaining"), quota_max=payload.get("quota_max"))
    if payload.get("backoff"):
        time.sleep(min(int(payload["backoff"]), 30))
    return payload


def search_stackexchange(
    query: str,
    tagged: Sequence[str] = (),
    site: str = "stackoverflow",
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Question search via api.stackexchange (keyless 300 req/day). Indicator hits."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        params: dict[str, Any] = {"q": query.strip(), "site": site, "pagesize": min(max_results, 100),
                                  "order": "desc", "sort": "relevance"}
        if tagged:
            params["tagged"] = ";".join(tagged)
        payload = _se_get("/search/advanced", params, timeout, client)
        hits: list[SearchHit] = []
        for q in payload.get("items") or []:
            link = str(q.get("link") or "")
            if not link.startswith(("http://", "https://")):
                continue
            title = _hn_clean(str(q.get("title") or ""))
            hits.append(SearchHit(url=link, title=title[:500], snippet=title[:SNIPPET_CHARS],
                                  backend="stackexchange"))
        return hits[:max_results]
    finally:
        if close:
            client.close()


def fetch_stackexchange_questions(
    query: str,
    tagged: Sequence[str] = (),
    site: str = "stackoverflow",
    max_questions: int | None = None,
    include_answers: bool = False,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[RawRecord]:
    """Full question bodies (+ optional top answer) via api.stackexchange, keyless.

    ``include_answers`` costs one extra API call per question against the
    300 req/day anonymous quota; off by default.
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        params: dict[str, Any] = {"q": query.strip(), "site": site, "filter": "withbody",
                                  "pagesize": min(max_questions or 10, 100),
                                  "order": "desc", "sort": "relevance"}
        if tagged:
            params["tagged"] = ";".join(tagged)
        payload = _se_get("/search/advanced", params, timeout, client)
        records: list[RawRecord] = []
        for q in payload.get("items") or []:
            title = _hn_clean(str(q.get("title") or ""))
            body = extract_text(str(q.get("body") or "")).strip()
            text = f"{title}\n\n{body}".strip() if body else title
            if include_answers:
                answer = _se_top_answer(int(q.get("question_id", 0)), site, timeout, client)
                if answer:
                    text += f"\n\nTop answer (score {answer[1]}):\n{answer[0]}"
            if not text:
                continue
            records.append(RawRecord(
                text=text,
                source_uri=str(q.get("link") or ""),
                title=title[:500],
                item_id=slugify_id(f"se-{site}-{q.get('question_id')}"),
                metadata={"source": "stackexchange", "evidence": "profile", "site": site,
                          "score": q.get("score"), "answer_count": q.get("answer_count"),
                          "tags": list(q.get("tags") or []),
                          "quota_remaining": LAST_SE_QUOTA.get("quota_remaining")},
            ))
            if max_questions is not None and len(records) >= max_questions:
                break
        return records
    finally:
        if close:
            client.close()


#: Stack Exchange sites whose questions the API is asked for directly. Their
#: HTML now answers 403 to non-browser clients, so fetching the page loses the
#: body while the API still serves it.
_SE_HOST_SITES = {
    "stackoverflow.com": "stackoverflow",
    "serverfault.com": "serverfault",
    "superuser.com": "superuser",
    "askubuntu.com": "askubuntu",
    "mathoverflow.net": "mathoverflow",
}
_SE_QUESTION_ID_RE = re.compile(r"/questions/(\d+)")


def se_site_for_url(url: str) -> str | None:
    """The API ``site`` parameter for a question URL, or None if it is not SE."""
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if host in _SE_HOST_SITES:
        return _SE_HOST_SITES[host]
    if host.endswith(".stackexchange.com"):
        return host[: -len(".stackexchange.com")]
    return None


def fetch_stackexchange_question(
    url: str,
    *,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    include_answers: bool = False,
) -> RawRecord:
    """One question's body via the API, for a hit whose page will not serve us."""
    match = _SE_QUESTION_ID_RE.search(url or "")
    site = se_site_for_url(url or "")
    if not match or not site:
        raise DiscoverError(f"not a Stack Exchange question URL: {url}")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        payload = _se_get(
            f"/questions/{match.group(1)}",
            {"site": site, "filter": "withbody"},
            timeout,
            client,
        )
        items = payload.get("items") or []
        if not items:
            raise DiscoverError(f"Stack Exchange returned no question for {url}")
        question = items[0]
        title = _hn_clean(str(question.get("title") or ""))
        body = extract_text(str(question.get("body") or "")).strip()
        text = f"{title}\n\n{body}".strip() if body else title
        if include_answers:
            answer = _se_top_answer(int(question.get("question_id", 0)), site, timeout, client)
            if answer:
                text += f"\n\nTop answer (score {answer[1]}):\n{answer[0]}"
        if not text:
            raise DiscoverError(f"Stack Exchange question {url} carried no text")
        return RawRecord(
            text=text,
            source_uri=str(question.get("link") or url),
            title=title[:500] or None,
            item_id=slugify_id(f"se-{site}-{question.get('question_id')}"),
            metadata={"evidence": "fetched", "source": "stackexchange", "site": site},
        )
    finally:
        if close:
            client.close()


def _se_top_answer(question_id: int, site: str, timeout: float, client: httpx.Client) -> tuple[str, Any] | None:
    if not question_id:
        return None
    try:
        payload = _se_get(f"/questions/{question_id}/answers",
                          {"site": site, "filter": "withbody", "pagesize": 1,
                           "order": "desc", "sort": "votes"}, timeout, client)
    except DiscoverError:
        return None
    answers = payload.get("items") or []
    if not answers:
        return None
    body = extract_text(str(answers[0].get("body") or "")).strip()
    return (body, answers[0].get("score")) if body else None


def _discourse_base(base_url: str) -> str:
    base = base_url if "://" in base_url else f"https://{base_url}"
    return base.rstrip("/")


def search_discourse(
    query: str,
    base_url: str,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Search any Discourse instance (/search.json, keyless). Indicator hits."""
    base = _discourse_base(base_url)
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(f"{base}/search.json", params={"q": query.strip()}, timeout=timeout)
        except Exception as exc:
            raise DiscoverError(f"Discourse search failed for {base}: {exc}") from exc
        payload = _safe_json(resp, f"Discourse search for {base}")
        titles = {t.get("id"): _hn_clean(str(t.get("fancy_title") or t.get("title") or ""))
                  for t in payload.get("topics") or []}
        hits: list[SearchHit] = []
        seen_topics: set = set()
        for post in payload.get("posts") or []:
            topic_id = post.get("topic_id")
            if topic_id in seen_topics:
                continue
            seen_topics.add(topic_id)
            title = titles.get(topic_id, "")
            snippet = _fallback_strip(_hn_clean(str(post.get("blurb") or "")))[:SNIPPET_CHARS]
            hits.append(SearchHit(url=f"{base}/t/{topic_id}", title=title[:500],
                                  snippet=snippet or title[:SNIPPET_CHARS], backend="discourse"))
            if len(hits) >= max_results:
                break
        return hits
    finally:
        if close:
            client.close()


def fetch_discourse_topic(
    base_url: str,
    topic_id: str | int,
    max_posts: int = 5,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> RawRecord:
    """Full posts of one Discourse topic (keyless). Skips deleted/small-action posts."""
    import html as _html

    base = _discourse_base(base_url)
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(f"{base}/t/{topic_id}.json", timeout=timeout)
        except Exception as exc:
            raise DiscoverError(f"Discourse topic fetch failed ({base}/t/{topic_id}): {exc}") from exc
        if resp.status_code == 404:
            raise DiscoverError(f"Discourse topic not found: {base}/t/{topic_id}")
        if resp.status_code != 200:
            raise DiscoverError(f"Discourse topic HTTP {resp.status_code}: {base}/t/{topic_id}")
        topic = _safe_json(resp, f"Discourse topic {base}/t/{topic_id}")
        title = _html.unescape(str(topic.get("title") or f"Topic {topic_id}"))
        lines = [title]
        count = 0
        for post in (topic.get("post_stream") or {}).get("posts") or []:
            if count >= max_posts:
                break
            if post.get("post_type") != 1 or post.get("deleted_at"):
                continue
            body = extract_text(str(post.get("cooked") or "")).strip()
            if not body:
                continue
            lines.append(f"[{post.get('username', 'unknown')}]: {body}")
            count += 1
        if count == 0:
            raise DiscoverError(f"no retrievable posts in Discourse topic {base}/t/{topic_id}")
        return RawRecord(
            text="\n\n".join(lines),
            source_uri=f"{base}/t/{topic_id}",
            title=title[:500],
            item_id=slugify_id(f"discourse-{urlparse(base).netloc}-{topic_id}"),
            metadata={"source": "discourse", "evidence": "profile",
                      "instance": base, "topic_id": str(topic_id)},
        )
    finally:
        if close:
            client.close()


def fetch_discourse_search(
    base_url: str,
    query: str | None,
    max_topics: int | None = None,
    max_posts_each: int = 5,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> tuple[list[RawRecord], list[dict[str, str]]]:
    """Search (or latest topics without a query) then fetch full topics, keyless."""
    base = _discourse_base(base_url)
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        topic_ids: list = []
        if query and query.strip():
            try:
                resp = client.get(f"{base}/search.json", params={"q": query.strip()}, timeout=timeout)
            except Exception as exc:
                raise DiscoverError(f"Discourse search failed for {base}: {exc}") from exc
            if resp.status_code != 200:
                raise DiscoverError(f"Discourse search HTTP {resp.status_code} for {base}")
            for post in _safe_json(resp, f"Discourse search {base}").get("posts") or []:
                topic_id = post.get("topic_id")
                if topic_id and topic_id not in topic_ids:
                    topic_ids.append(topic_id)
        else:
            try:
                resp = client.get(f"{base}/latest.json", timeout=timeout)
            except Exception as exc:
                raise DiscoverError(f"Discourse latest failed for {base}: {exc}") from exc
            if resp.status_code != 200:
                raise DiscoverError(f"Discourse latest HTTP {resp.status_code} for {base}")
            for topic in (_safe_json(resp, f"Discourse latest {base}").get("topic_list") or {}).get("topics") or []:
                if topic.get("id") and topic["id"] not in topic_ids:
                    topic_ids.append(topic["id"])
        records: list[RawRecord] = []
        skipped: list[dict[str, str]] = []
        for topic_id in topic_ids if max_topics is None else topic_ids[:max_topics]:
            try:
                records.append(fetch_discourse_topic(base, topic_id, max_posts=max_posts_each,
                                                     timeout=timeout, client=client))
            except DiscoverError as exc:
                skipped.append({"source": f"discourse:{base}/t/{topic_id}", "reason": str(exc)})
        return records, skipped
    finally:
        if close:
            client.close()


def fetch_lobsters(
    tag: str | None = None,
    max_results: int = 25,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[RawRecord]:
    """Lobsters newest (or tag) listing with plain-text descriptions, keyless."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        url = f"https://lobste.rs/t/{tag}.json" if tag else "https://lobste.rs/newest.json"
        try:
            resp = _get_with_backoff(client, url, timeout=timeout)
        except DiscoverError as exc:
            if "HTTP 404" in str(exc):
                raise DiscoverError(f"unknown Lobsters tag: {tag}") from exc
            raise
        records: list[RawRecord] = []
        for story in _safe_json(resp, url) or []:
            title = str(story.get("title") or "").strip()
            description = str(story.get("description_plain") or "").strip()
            text = f"{title}\n\n{description}".strip() if description else title
            if not text:
                continue
            records.append(RawRecord(
                text=text,
                source_uri=str(story.get("comments_url") or story.get("short_id_url") or ""),
                title=title[:500],
                item_id=slugify_id(f"lobsters-{story.get('short_id')}"),
                metadata={"source": "lobsters", "evidence": "profile",
                          "link": str(story.get("url") or ""),
                          "score": story.get("score"), "comment_count": story.get("comment_count"),
                          "tags": list(story.get("tags") or [])},
            ))
            if len(records) >= max_results:
                break
        return records
    finally:
        if close:
            client.close()


def search_lobsters(
    query: str,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Client-side filter over the Lobsters newest listing (no search API)."""
    words = [w.lower() for w in query.split() if w.strip()]
    own_records = fetch_lobsters(tag=None, max_results=50, timeout=timeout, client=client)
    hits: list[SearchHit] = []
    for rec in own_records:
        if not rec.source_uri.startswith(("http://", "https://")):
            continue
        haystack = f"{rec.title} {rec.text} {' '.join(rec.metadata.get('tags') or [])}".lower()
        if words and not all(w in haystack for w in words):
            continue
        hits.append(SearchHit(url=rec.source_uri, title=rec.title or "",
                              snippet=rec.text[:SNIPPET_CHARS], backend="lobsters"))
        if len(hits) >= max_results:
            break
    return hits


def _lemmy_records(payload: dict[str, Any]) -> list[RawRecord]:
    records: list[RawRecord] = []
    for wrapper in payload.get("posts") or []:
        post = wrapper.get("post") or {}
        if post.get("removed") or post.get("deleted") or post.get("nsfw"):
            continue
        name = str(post.get("name") or "").strip()
        body = str(post.get("body") or "").strip()
        text = f"{name}\n\n{body}".strip() if body else name
        if not text:
            continue
        records.append(RawRecord(
            text=text,
            source_uri=str(post.get("ap_id") or ""),
            title=name[:500],
            item_id=slugify_id(f"lemmy-{post.get('id')}"),
            metadata={"source": "lemmy", "evidence": "profile",
                      "published": str(post.get("published") or "")},
        ))
    for wrapper in payload.get("comments") or []:
        comment = wrapper.get("comment") or {}
        if comment.get("removed") or comment.get("deleted"):
            continue
        body = str(comment.get("content") or "").strip()
        if not body:
            continue
        records.append(RawRecord(
            text=body,
            source_uri=str(comment.get("ap_id") or ""),
            title=body.split("\n", 1)[0][:500],
            item_id=slugify_id(f"lemmy-c-{comment.get('id')}"),
            metadata={"source": "lemmy", "evidence": "profile",
                      "published": str(comment.get("published") or "")},
        ))
    return records


def search_lemmy(
    query: str,
    instance: str = LEMMY_DEFAULT,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Lemmy instance search (posts + comments, keyless). Indicator hits."""
    base = instance.rstrip("/")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(f"{base}/api/v3/search",
                              params={"q": query.strip(), "type_": "All",
                                      "listing_type": "All", "limit": min(max_results, 50)},
                              timeout=timeout)
        except Exception as exc:
            raise DiscoverError(f"Lemmy search failed for {base}: {exc}") from exc
        if resp.status_code != 200:
            raise DiscoverError(f"Lemmy search HTTP {resp.status_code} for {base}")
        hits = [SearchHit(url=r.source_uri, title=r.title or "", snippet=r.text[:SNIPPET_CHARS],
                          backend="lemmy")
                for r in _lemmy_records(_safe_json(resp, f"Lemmy search {base}")) if r.source_uri.startswith(("http://", "https://"))]
        return hits[:max_results]
    finally:
        if close:
            client.close()


def fetch_lemmy(
    query: str,
    instance: str = LEMMY_DEFAULT,
    max_results: int = 25,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[RawRecord]:
    """Full Lemmy post/comment bodies for a query (keyless)."""
    base = instance.rstrip("/")
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(f"{base}/api/v3/search",
                              params={"q": query.strip(), "type_": "All",
                                      "listing_type": "All", "limit": min(max_results, 50)},
                              timeout=timeout)
        except Exception as exc:
            raise DiscoverError(f"Lemmy search failed for {base}: {exc}") from exc
        if resp.status_code != 200:
            raise DiscoverError(f"Lemmy search HTTP {resp.status_code} for {base}")
        return _lemmy_records(_safe_json(resp, f"Lemmy search {base}"))[:max_results]
    finally:
        if close:
            client.close()


def fetch_devto_tag(
    tag: str,
    max_articles: int | None = None,
    full_body: bool = True,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[RawRecord]:
    """Dev.to tag listing (+ full markdown bodies, keyless)."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = _get_with_backoff(client, f"{DEVTO_API}/articles",
                                     params={"tag": tag, "per_page": min(max_articles or 10, 1000)},
                                     timeout=timeout)
        except DiscoverError as exc:
            raise DiscoverError(f"Dev.to listing failed for tag {tag!r}: {exc}") from exc
        records: list[RawRecord] = []
        for article in _safe_json(resp, "Dev.to") or []:
            title = str(article.get("title") or "").strip()
            if full_body:
                try:
                    detail = _get_with_backoff(client, f"{DEVTO_API}/articles/{article.get('id')}", timeout=timeout)
                    body = str(_safe_json(detail, "Dev.to article").get("body_markdown") or "").strip()
                except DiscoverError:
                    body = ""
                text = f"{title}\n\n{body}".strip() if body else title
                evidence = "profile" if body else "indicator"
            else:
                text = f"{title}\n\n{article.get('description') or ''}".strip()
                evidence = "indicator"
            if not text:
                continue
            records.append(RawRecord(
                text=text,
                source_uri=str(article.get("url") or ""),
                title=title[:500],
                item_id=slugify_id(f"devto-{article.get('id')}"),
                metadata={"source": "dev.to", "evidence": evidence,
                          "tags": list(article.get("tag_list") or []),
                          "reactions": (article.get("public_reactions_count") or 0)},
            ))
            if max_articles is not None and len(records) >= max_articles:
                break
        return records
    finally:
        if close:
            client.close()


def search_devto(
    query: str,
    max_results: int = 10,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> list[SearchHit]:
    """Client-side filter over latest Dev.to articles (no search API)."""
    words = [w.lower() for w in query.split() if w.strip()]
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = _get_with_backoff(client, f"{DEVTO_API}/articles", params={"per_page": 30}, timeout=timeout)
        except DiscoverError as exc:
            raise DiscoverError(f"Dev.to listing failed: {exc}") from exc
        hits: list[SearchHit] = []
        for article in _safe_json(resp, "Dev.to") or []:
            title = str(article.get("title") or "")
            description = str(article.get("description") or "")
            haystack = f"{title} {description} {' '.join(article.get('tag_list') or [])}".lower()
            if words and not all(w in haystack for w in words):
                continue
            url = str(article.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            hits.append(SearchHit(url=url, title=title[:500],
                                  snippet=f"{title}\n\n{description}".strip()[:SNIPPET_CHARS],
                                  backend="devto"))
            if len(hits) >= max_results:
                break
        return hits
    finally:
        if close:
            client.close()


#: Backends with no keyword search API. They filter a recent listing, so a
#: specific query can legitimately match nothing; saying so beats reporting
#: "0 hits", which reads as a broken connector.
LISTING_FILTER_BACKENDS: dict[str, str] = {
    "devto": "the latest Dev.to articles",
    "lobsters": "the newest Lobsters stories",
    "yc": "the YC company directory",
}


BACKENDS: dict[str, Callable[..., list[SearchHit]]] = {
    "ddgs": search_ddgs,
    "searxng": search_searxng,
    "hn": search_hn,
    "yc": search_yc,
    "reddit": search_reddit,
    "stackexchange": search_stackexchange,
    "discourse": search_discourse,
    "lobsters": search_lobsters,
    "lemmy": search_lemmy,
    "devto": search_devto,
}


# ---------------------------------------------------------------------------
# Structured ATS intake (keyless JSON APIs)
# ---------------------------------------------------------------------------

def normalize_term(term: str) -> str:
    """Lowercase + whitespace-collapsed form for deterministic term matching."""
    return _WS.sub(" ", term.strip().lower())


def stack_signal(
    text: str,
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> dict[str, Any]:
    """Mechanical stack membership signal: exact + normalized substring match.

    Returns ``{"required_hits": [...], "excluded_hits": [...], "verdict": ...}``
    where verdict is ``"excluded"`` (an excluded term appears), ``"missing"``
    (required terms given but none appears), ``"matched"`` (a required term
    appears and nothing excluded), or ``"unconstrained"`` (no terms given).
    The LLM explains and ranks; it never decides set membership from memory.
    """
    haystack = normalize_term(text or "")
    required_hits = [t for t in required_stack if t and normalize_term(t) in haystack]
    excluded_hits = [t for t in excluded_stack if t and normalize_term(t) in haystack]
    if excluded_hits:
        verdict = "excluded"
    elif required_stack and not required_hits:
        verdict = "missing"
    elif required_hits:
        verdict = "matched"
    else:
        verdict = "unconstrained"
    return {"required_hits": required_hits, "excluded_hits": excluded_hits, "verdict": verdict}


def _title_matches(
    title: str,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> bool:
    """Deterministic ATS title gate: must match an include term (if any) and no exclude term."""
    lowered = title.lower()
    if exclude and any(term.lower() in lowered for term in exclude if term):
        return False
    if include and not any(term.lower() in lowered for term in include if term):
        return False
    return True


def _ats_records(
    jobs: Sequence[dict[str, Any]],
    *,
    source: str,
    get_text: Callable[[dict[str, Any]], str],
    get_url: Callable[[dict[str, Any]], str],
    get_title: Callable[[dict[str, Any]], str],
    get_job_id: Callable[[dict[str, Any]], str],
    org: str,
    max_jobs: int | None = None,
    title_include: Sequence[str] = (),
    title_exclude: Sequence[str] = (),
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> list[RawRecord]:
    records: list[RawRecord] = []
    for job in jobs if max_jobs is None else list(jobs)[:max_jobs]:
        title = ""
        try:
            title = get_title(job)
        except Exception:
            title = ""
        if not _title_matches(title or "", title_include, title_exclude):
            continue
        try:
            text = get_text(job)
        except Exception:
            continue
        if not text or not text.strip():
            continue
        metadata: dict[str, Any] = {"ats": source, "org": org, "evidence": "profile"}
        if required_stack or excluded_stack:
            metadata["stack_signal"] = stack_signal(text, required_stack, excluded_stack)
            if metadata["stack_signal"]["verdict"] == "excluded":
                metadata["stack_veto"] = True
        records.append(RawRecord(
            text=text.strip(),
            source_uri=get_url(job),
            title=title,
            item_id=slugify_id(f"{org}-{get_job_id(job)}"),
            metadata=metadata,
        ))
    return records


def fetch_greenhouse_board(
    board: str,
    max_jobs: int | None = None,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    title_include: Sequence[str] = (),
    title_exclude: Sequence[str] = (),
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> list[RawRecord]:
    """Fetch postings for a Greenhouse board token (public JSON API, no key).

    Deterministic title/stack gates run before the LLM ever sees a posting:
    ``title_include``/``title_exclude`` filter job titles, ``required_stack``
    /``excluded_stack`` annotate each record with a mechanical
    ``stack_signal`` (excluded hits set ``stack_veto``).
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(GREENHOUSE_API.format(board=board))
        except Exception as exc:
            raise DiscoverError(f"Greenhouse fetch failed for board {board!r}: {exc}") from exc
        if resp.status_code == 404:
            raise DiscoverError(f"unknown Greenhouse board {board!r}")
        if resp.status_code != 200:
            raise DiscoverError(f"Greenhouse returned HTTP {resp.status_code} for board {board!r}")
        jobs = _safe_json(resp, f"Greenhouse board {board}").get("jobs") or []
        return _ats_records(
            jobs, source="greenhouse", org=board, max_jobs=max_jobs,
            get_text=lambda j: extract_text(j.get("content") or ""),
            get_url=lambda j: j.get("absolute_url") or "",
            get_title=lambda j: str(j.get("title") or ""),
            get_job_id=lambda j: str(j.get("id") or ""),
            title_include=title_include, title_exclude=title_exclude,
            required_stack=required_stack, excluded_stack=excluded_stack,
        )
    finally:
        if close:
            client.close()


def fetch_ashby_org(
    org: str,
    max_jobs: int | None = None,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    title_include: Sequence[str] = (),
    title_exclude: Sequence[str] = (),
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> list[RawRecord]:
    """Fetch postings for an Ashby org (public posting API, no key).

    Accepts the same deterministic title/stack gates as
    :func:`fetch_greenhouse_board`.
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(ASHBY_API.format(org=org))
        except Exception as exc:
            raise DiscoverError(f"Ashby fetch failed for org {org!r}: {exc}") from exc
        if resp.status_code == 404:
            raise DiscoverError(f"unknown Ashby org {org!r}")
        if resp.status_code != 200:
            raise DiscoverError(f"Ashby returned HTTP {resp.status_code} for org {org!r}")
        jobs = _safe_json(resp, f"Ashby org {org}").get("jobs") or []
        return _ats_records(
            [j for j in jobs if j.get("isListed", True)], source="ashby", org=org, max_jobs=max_jobs,
            get_text=lambda j: (j.get("descriptionPlain") or "") or extract_text(j.get("descriptionHtml") or ""),
            get_url=lambda j: j.get("jobUrl") or "",
            get_title=lambda j: str(j.get("title") or ""),
            get_job_id=lambda j: str(j.get("id") or "")[:8],
            title_include=title_include, title_exclude=title_exclude,
            required_stack=required_stack, excluded_stack=excluded_stack,
        )
    finally:
        if close:
            client.close()


def fetch_lever_org(
    org: str,
    max_jobs: int | None = None,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
    title_include: Sequence[str] = (),
    title_exclude: Sequence[str] = (),
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> list[RawRecord]:
    """Fetch postings for a Lever org. Many companies migrated ATS; 404 is common.

    Accepts the same deterministic title/stack gates as
    :func:`fetch_greenhouse_board`.
    """
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(LEVER_API.format(org=org))
        except Exception as exc:
            raise DiscoverError(f"Lever fetch failed for org {org!r}: {exc}") from exc
        if resp.status_code == 404:
            raise DiscoverError(f"unknown Lever org {org!r} (many companies migrated off Lever)")
        if resp.status_code != 200:
            raise DiscoverError(f"Lever returned HTTP {resp.status_code} for org {org!r}")
        jobs = _safe_json(resp, f"Lever org {org}")
        if isinstance(jobs, dict):
            jobs = jobs.get("postings") or jobs.get("data") or []
        return _ats_records(
            jobs, source="lever", org=org, max_jobs=max_jobs,
            get_text=lambda j: extract_text(j.get("text") or j.get("description") or ""),
            get_url=lambda j: j.get("hostedUrl") or j.get("applyUrl") or "",
            get_title=lambda j: str(j.get("text") or j.get("title") or "")[:200],
            get_job_id=lambda j: str(j.get("id") or "")[:8],
            title_include=title_include, title_exclude=title_exclude,
            required_stack=required_stack, excluded_stack=excluded_stack,
        )
    finally:
        if close:
            client.close()


# ---------------------------------------------------------------------------
# Sitemaps + same-domain site crawl (no key; honors robots.txt)
# ---------------------------------------------------------------------------

def _sitemap_entries(payload: bytes) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Split a sitemap document into (child_sitemaps, [(page_url, lastmod)]).

    Namespace-blind like the rest of intake; ``lastmod`` is the raw string
    (or None) so recrawl freshness decisions stay downstream.
    """
    import xml.etree.ElementTree as ET

    if payload[:2] == b"\x1f\x8b":
        import gzip

        try:
            payload = gzip.decompress(payload)
        except Exception as exc:
            raise DiscoverError(f"cannot decompress gzipped sitemap: {exc}") from exc
    try:
        root = ET.fromstring(payload)
    except Exception as exc:
        raise DiscoverError(f"cannot parse sitemap XML: {exc}") from exc

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _text(node: Any) -> str:
        return (node.text or "").strip()

    sitemaps: list[str] = []
    pages: list[tuple[str, str | None]] = []
    if local(root.tag) == "sitemapindex":
        for child in root:
            if local(child.tag) != "sitemap":
                continue
            for node in child:
                if local(node.tag) == "loc" and _text(node):
                    sitemaps.append(_text(node))
    else:
        for url in root.iter():
            if local(url.tag) != "url":
                continue
            loc, lastmod = "", None
            for node in url:
                kind = local(node.tag)
                if kind == "loc" and _text(node):
                    loc = _text(node)
                elif kind == "lastmod" and _text(node):
                    lastmod = _text(node)
            if loc:
                pages.append((loc, lastmod))
    return sitemaps, pages


def _sitemap_locs(payload: bytes) -> tuple[list[str], list[str]]:
    """Split a sitemap document into (child_sitemaps, page_urls), namespace-blind."""
    sitemaps, entries = _sitemap_entries(payload)
    return sitemaps, [url for url, _ in entries]


def fetch_sitemap_urls(
    sitemap_url: str,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
    max_urls: int | None = None,
    _depth: int = 0,
) -> list[str]:
    """Fetch a sitemap (or sitemapindex, recursively) and return page URLs."""
    return [
        url for url, _ in fetch_sitemap_entries(
            sitemap_url, client=client, timeout=timeout, max_urls=max_urls, _depth=_depth,
        )
    ]


def fetch_sitemap_entries(
    sitemap_url: str,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
    max_urls: int | None = None,
    _depth: int = 0,
) -> list[tuple[str, str | None]]:
    """Fetch a sitemap (or sitemapindex, recursively) with lastmod dates.

    Returns ``[(page_url, lastmod)]`` in document order, deduped. ``lastmod``
    is the raw sitemap string or None when absent — freshness policy stays
    with the caller. Prefer this over :func:`fetch_sitemap_urls` when
    recrawl decisions need dates.
    """
    if _depth > 3:
        return []
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        try:
            resp = client.get(sitemap_url, timeout=timeout)
        except Exception as exc:
            raise DiscoverError(f"sitemap fetch failed for {sitemap_url}: {exc}") from exc
        if resp.status_code != 200:
            raise DiscoverError(f"sitemap HTTP {resp.status_code} for {sitemap_url}")
        content = resp.content
        if content[:2] == b"\x1f\x8b":
            import gzip
            try:
                content = gzip.decompress(content)
            except Exception as exc:
                raise DiscoverError(f"cannot decompress gzipped sitemap: {exc}") from exc
        child_maps, entries = _sitemap_entries(content[:MAX_BYTES])
        collected = list(entries)
        for child in child_maps[:10]:
            if max_urls is not None and len(collected) >= max_urls:
                break
            # Hand each child only the budget still outstanding, so a large
            # sitemapindex cannot download every nested sitemap before the
            # final truncation makes the cap look effective.
            remaining = None if max_urls is None else max(1, max_urls - len(collected))
            try:
                collected.extend(fetch_sitemap_entries(
                    child, client=client, timeout=timeout, max_urls=remaining, _depth=_depth + 1,
                ))
            except DiscoverError:
                continue  # stale child sitemaps are common; keep the good ones
        deduped: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for url, lastmod in collected:
            if url not in seen:
                seen.add(url)
                deduped.append((url, lastmod))
        return deduped[:max_urls] if max_urls is not None else deduped
    finally:
        if close:
            client.close()


def discover_sitemap_url(
    site_url: str,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> str:
    """Locate a site's sitemap via common paths + robots.txt Sitemap: lines."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        parts = urlparse(site_url if "://" in site_url else f"https://{site_url}")
        scheme = parts.scheme or "https"
        host = parts.netloc or parts.path
        origins = [f"{scheme}://{host}"]
        alt_host = host[4:] if host.startswith("www.") else f"www.{host}"
        origins.append(f"{scheme}://{alt_host}")

        candidates: list[str] = []
        for origin in origins:
            candidates.extend([f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"])
            try:
                resp = client.get(f"{origin}/robots.txt", timeout=timeout)
                if resp.status_code == 200:
                    for line in resp.text.splitlines():
                        clean = line.split("#", 1)[0].strip()
                        if clean.lower().startswith("sitemap:"):
                            candidates.append(clean.split(":", 1)[1].strip())
            except Exception:
                pass
        candidates = list(dict.fromkeys(candidates))
        for candidate in candidates:
            try:
                resp = client.get(candidate, timeout=timeout)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            head = resp.content.lstrip()[:5]
            gzipped = resp.content[:2] == b"\x1f\x8b"
            if gzipped or head.startswith((b"<?xml", b"<urls", b"<sit")):
                return candidate
        raise DiscoverError(f"no sitemap found for {site_url} (tried {len(candidates)} locations)")
    finally:
        if close:
            client.close()


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.strip():
                self.links.append(href.strip())


def extract_links(html: str, base_url: str) -> list[str]:
    """Same-scheme http(s) links from a page, resolved + defragmented + deduped."""
    from urllib.parse import urldefrag, urljoin

    parser = _LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    out, seen = [], set()
    for href in parser.links:
        if href.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def crawl_site(
    start_url: str,
    max_pages: int = 20,
    max_depth: int = 2,
    same_origin: bool = True,
    timeout: float = 20.0,
    delay: float = 1.0,
    respect_robots: bool = True,
    render_js: bool = False,
    client: httpx.Client | None = None,
) -> tuple[list[RawRecord], list[dict[str, str]]]:
    """BFS crawl from one URL: same-origin + depth-capped, polite, robots-aware."""
    close = False
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        close = True
    try:
        origin = urlparse(start_url).netloc.lower()
        queue: list[tuple[str, int]] = [(start_url, 0)]
        visited: set[str] = {canonical_url(start_url)}
        records: list[RawRecord] = []
        skipped: list[dict[str, str]] = []
        last_fetch: dict[str, float] = {}
        while queue and len(records) + len(skipped) < max_pages:
            url, depth = queue.pop(0)
            # Per-origin politeness: pace each origin independently so one
            # slow origin neither hammers its server nor throttles the crawl.
            if delay > 0:
                page_origin = urlparse(url).netloc.lower()
                wait = delay - (time.time() - last_fetch.get(page_origin, 0.0))
                if wait > 0:
                    time.sleep(wait)
                last_fetch[page_origin] = time.time()
            try:
                if render_js:
                    if respect_robots and not robots_allowed(url, client):
                        raise DiscoverError(f"blocked by robots.txt: {url}")
                    final_url, rendered_html = _render_js_page(url, timeout=timeout)
                    rendered_html = rendered_html[:MAX_BYTES]
                    text = extract_text(rendered_html)
                    if not text:
                        raise DiscoverError(f"no extractable text for {url} (even rendered)")
                    title = _extract_title(rendered_html) or domain_of(url)
                    record = RawRecord(text=text, source_uri=url, title=title,
                                       item_id=record_id(url, title),
                                       metadata={"evidence": "fetched", "rendered": "js"})
                    html = rendered_html if depth < max_depth else ""
                else:
                    final_url, raw_header, raw = _http_get(url, client, timeout, respect_robots)
                    record = _record_from_response(final_url, raw_header, raw, url)
                    ctype = raw_header.split(";")[0].strip().lower()
                    html = _decode_body(raw, raw_header) if (
                        depth < max_depth and ctype in ("text/html", "application/xhtml+xml", "")) else ""
                records.append(record)
                if html:
                    # Relative links belong to the page we actually landed on,
                    # not the URL we asked for: a redirect to another path (or
                    # host) would otherwise resolve every link against the
                    # wrong base.
                    for link in extract_links(html, final_url):
                        if same_origin and urlparse(link).netloc.lower() != origin:
                            continue
                        key = canonical_url(link)
                        if key not in visited:
                            visited.add(key)
                            queue.append((link, depth + 1))
            except DiscoverError as exc:
                skipped.append({"url": url, "reason": str(exc)})
        return records, skipped
    finally:
        if close:
            client.close()


# ---------------------------------------------------------------------------
# Orchestration -> InputItem records
# ---------------------------------------------------------------------------

def to_input_items(
    records: Sequence[RawRecord],
    max_chars: int | None = None,
    min_chars: int | None = None,
    allowed_evidence: Sequence[str] | None = None,
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> list[InputItem]:
    """Validate discovered records into closed InputItems.

    Ids are deterministic across processes: collisions get a sha256 suffix of
    the source URI (never the salted builtin hash, which breaks rerun
    identity and --only-ids compounding).

    Mechanical quality gates run before any LLM sees the text: ``min_chars``
    drops stub records, ``allowed_evidence`` admits only the named evidence
    grades (e.g. ``("fetched", "profile")`` keeps snippet ``indicator``
    records out of grounding-grade campaigns), and ``required_stack`` /
    ``excluded_stack`` annotate each item with a deterministic
    ``stack_signal`` (excluded hits set ``stack_veto``).
    """
    items: list[InputItem] = []
    seen: set[str] = set()
    for rec in records:
        text = rec.text.strip()
        if not text:
            continue
        if min_chars is not None and len(text) < min_chars:
            continue
        if allowed_evidence is not None and rec.metadata.get("evidence") not in allowed_evidence:
            continue
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        metadata = dict(rec.metadata)
        if required_stack or excluded_stack:
            metadata["stack_signal"] = stack_signal(text, required_stack, excluded_stack)
            if metadata["stack_signal"]["verdict"] == "excluded":
                metadata["stack_veto"] = True
        item_id = slugify_id(rec.item_id or record_id(rec.source_uri, rec.title))
        if item_id in seen:
            base_id = item_id
            suffix = _stable_suffix(rec.source_uri) if rec.source_uri else _stable_suffix(rec.text)
            item_id = slugify_id(f"{base_id}-{suffix}")
            counter = 1
            while item_id in seen:
                item_id = slugify_id(f"{base_id}-{suffix}-{counter}")
                counter += 1
        seen.add(item_id)
        items.append(InputItem(
            item_id=item_id,
            text=text,
            title=rec.title,
            source_uri=rec.source_uri or None,
            metadata=metadata,
        ))
    return items


def write_items_csv(items: Sequence[InputItem], path: str | Path) -> Path:
    """Write items as accounts.csv (reloadable via load_input_items)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["item_id", "title", "text", "source_uri"])
        writer.writeheader()
        for item in items:
            writer.writerow({
                "item_id": item.item_id,
                "title": item.title or "",
                "text": item.text,
                "source_uri": item.source_uri or "",
            })
    return out


def write_items_jsonl(items: Sequence[InputItem], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item.model_dump(mode="json", by_alias=True), ensure_ascii=False) + "\n")
    return out


def run_discovery(
    queries: Sequence[str],
    backends: Sequence[str] = ("ddgs", "hn"),
    max_results: int = 10,
    fetch_full_text: bool = True,
    searxng_url: str | None = None,
    timeout: float = 20.0,
    delay: float = 1.0,
    respect_robots: bool = True,
    max_chars: int | None = None,
    render_js: bool = False,
    reddit_subreddits: Sequence[str] = (),
    se_tagged: Sequence[str] = (),
    se_site: str = "stackoverflow",
    discourse_url: str | None = None,
    lemmy_instance: str = LEMMY_DEFAULT,
    min_source_coverage: float | None = None,
    min_chars: int | None = None,
    allowed_evidence: Sequence[str] | None = None,
    required_stack: Sequence[str] = (),
    excluded_stack: Sequence[str] = (),
) -> tuple[list[InputItem], dict[str, Any]]:
    """Search queries broadly, fetch hits, return (items, report).

    Backend misconfiguration raises immediately; per-hit/per-query failures
    are collected into ``report["skipped"]`` and never abort the run.

    Snippet records (``fetch_full_text=False``) are triage indicators only:
    they carry ``metadata["evidence"] == "indicator"`` and must not back
    tier-1 claims. Re-run without ``--snippets-only`` for grounding-grade
    full text.
    """
    if min_source_coverage is not None and not 0.0 <= min_source_coverage <= 1.0:
        raise DiscoverError("min_source_coverage must be between 0.0 and 1.0")
    if render_js:
        require_playwright()
    backends = _validate_backends(backends, searxng_url, discourse_url)
    records: list[RawRecord] = []
    skipped: list[dict[str, str]] = []
    hits_seen = 0
    backend_metrics: dict[str, dict[str, int]] = {
        backend: {"queries": 0, "hits": 0, "unique_hits": 0, "captured": 0, "skipped": 0}
        for backend in backends
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        # Breadth first: every (query, backend) surface is probed in parallel, so a
        # surface that produces nothing costs wall-clock but not the run. No single
        # surface owes us an output; the run succeeds if any of them produced.
        pairs = [
            (index, query, backend)
            for index, query in enumerate(queries) if query.strip()
            for backend in backends
        ]
        # One in-flight request per source: a source's queries are walked in order
        # (spaced), while different sources run at once. Parallelising queries
        # *within* a source is how a run gets itself rate-limited.
        for query in queries:
            if not query.strip():
                skipped.append({"query": query, "reason": "empty query"})
        found: dict[tuple[int, str], list[SearchHit]] = {}
        failures: list[tuple[int, str, str]] = []
        #: Queries that only answered after a retry. Reported, never hidden: a
        #: run whose breadth came from the third attempt is a different fact
        #: from a run where every source answered first time.
        retried: list[dict[str, Any]] = []
        if pairs:
            def probe_source(backend: str, source_queries: list[tuple[int, str]]) -> None:
                for index, query in source_queries:
                    if not _source_ready(backend, delay):
                        continue
                    try:
                        hits, used = run_backend_retrying(
                            backend, query,
                            max_results=max_results,
                            searxng_url=searxng_url, client=client,
                            reddit_subreddits=reddit_subreddits, se_tagged=se_tagged,
                            se_site=se_site, discourse_url=discourse_url,
                            lemmy_instance=lemmy_instance,
                        )
                        found[(index, backend)] = hits
                        if used > 1:
                            retried.append({
                                "query": query, "backend": backend, "attempts": used,
                                "hits": len(hits),
                                "reason": (
                                    f"source answered nothing once and {len(hits)} hit(s) on attempt {used}"
                                    if hits else f"source answered nothing on all {used} attempts"
                                ),
                            })
                    except DiscoverError as exc:
                        failures.append((index, backend, f"{query}: {exc}"))
                    except Exception as exc:  # a surface must not take the run down
                        failures.append((index, backend, f"{query}: {type(exc).__name__}: {exc}"))

            by_source: dict[str, list[tuple[int, str]]] = {}
            for index, query, backend in pairs:
                by_source.setdefault(backend, []).append((index, query))
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(by_source)))) as pool:
                list(pool.map(lambda item: probe_source(*item), by_source.items()))

        for index, query in enumerate(queries):
            if not query.strip():
                continue
            seen_urls: set[str] = set()
            ordered: list[SearchHit] = []
            for backend in backends:
                metrics = backend_metrics.setdefault(
                    backend, {"queries": 0, "hits": 0, "unique_hits": 0, "captured": 0, "skipped": 0}
                )
                metrics["queries"] += 1
                backend_hits = found.get((index, backend), [])
                metrics["hits"] += len(backend_hits)
                for hit in backend_hits:
                    key = canonical_url(hit.url)
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    ordered.append(hit)
                    metrics["unique_hits"] += 1
            for _index, _backend, reason in failures:
                if reason.startswith(f"{query}: "):
                    skipped.append({"query": query, "backend": _backend, "reason": reason.split(": ", 1)[1]})
            if not ordered:
                skipped.append({"query": query, "reason": "0 hits from backends"})
            pending: list[tuple[int, SearchHit]] = []
            for position, hit in enumerate(ordered):
                host = host_of(hit.url)
                if host and not is_source_host(host):
                    # Unknown provenance is recorded, never guessed at: the
                    # growth pass proposes, a person promotes, and only then
                    # does it classify.
                    from . import registry

                    if registry.lookup(host) is None:
                        try:
                            registry.observe(hit.url, hit.title or "")
                        except Exception:
                            pass
                if is_noise_host(host_of(hit.url)):
                    skipped.append({
                        "url": hit.url,
                        "reason": "academic publisher or paper aggregator; not an evidence surface "
                                  "for company work (fetch it explicitly with --url if you want it)",
                    })
                    continue
                hits_seen += 1
                metrics = backend_metrics.setdefault(
                    hit.backend or "unknown",
                    {"queries": 0, "hits": 0, "unique_hits": 0, "captured": 0, "skipped": 0},
                )
                if not fetch_full_text:
                    records.append(RawRecord(
                        text=hit.snippet or hit.title,
                        source_uri=hit.url,
                        title=hit.title or None,
                        item_id=record_id(hit.url, hit.title),
                        metadata={
                            "backend": hit.backend,
                            "discovery_backend": hit.backend,
                            "discovery_query": query,
                            "discovered_from": hit.url,
                            "evidence": "indicator",
                        },
                    ))
                    continue
                pending.append((position, hit))

            # Fetch many hosts at once, one request per host at a time. The record
            # order stays the discovery order, so a run is reproducible however the
            # network interleaves.
            if pending:
                outcomes: dict[int, Any] = {}
                by_host: dict[str, list[tuple[int, SearchHit]]] = {}
                for position, hit in pending:
                    by_host.setdefault(host_of(hit.url) or "unknown", []).append((position, hit))

                def fetch_one_host(
                    host: str,
                    entries: list[tuple[int, SearchHit]],
                    _outcomes: dict[int, Any] = outcomes,
                ) -> None:
                    for position, hit in entries:
                        _space_host(host, delay)
                        try:
                            # Stack Exchange question pages answer 403 to
                            # non-browser clients now; the API still serves the
                            # same body.
                            if hit.backend == "stackexchange" and se_site_for_url(hit.url):
                                _outcomes[position] = fetch_stackexchange_question(
                                    hit.url, timeout=timeout, client=client)
                            else:
                                _outcomes[position] = fetch_smart_url(
                                    hit.url, client=client, respect_robots=respect_robots,
                                    render_js=render_js)
                        except DiscoverError as exc:
                            _outcomes[position] = exc

                with ThreadPoolExecutor(max_workers=max(1, min(8, len(by_host)))) as pool:
                    list(pool.map(lambda item: fetch_one_host(*item), by_host.items()))

                for position, hit in pending:
                    outcome = outcomes.get(position)
                    metrics = backend_metrics.setdefault(
                        hit.backend or "unknown",
                        {"queries": 0, "hits": 0, "unique_hits": 0, "captured": 0, "skipped": 0},
                    )
                    if isinstance(outcome, DiscoverError):
                        skipped.append({"url": hit.url, "reason": str(outcome)})
                        metrics["skipped"] += 1
                        continue
                    if outcome is None:
                        continue
                    outcome.metadata.update({
                        "discovery_backend": hit.backend,
                        "discovery_query": query,
                        "discovered_from": hit.url,
                    })
                    records.append(outcome)
    items = to_input_items(
        records,
        max_chars=max_chars,
        min_chars=min_chars,
        allowed_evidence=allowed_evidence,
        required_stack=required_stack,
        excluded_stack=excluded_stack,
    )
    for item in items:
        backend = item.metadata.get("discovery_backend") or item.metadata.get("backend") or "unknown"  # type: ignore[assignment]
        metrics = backend_metrics.setdefault(
            str(backend), {"queries": 0, "hits": 0, "unique_hits": 0, "captured": 0, "skipped": 0}
        )
        metrics["captured"] += 1
    for backend, metrics in backend_metrics.items():
        if metrics["hits"] == 0 and backend in LISTING_FILTER_BACKENDS:
            skipped.append({
                "source": backend,
                "reason": f"0 matches in the recent listing it filters ({LISTING_FILTER_BACKENDS[backend]}); "
                          "use --backend ddgs for keyword search over the open web",
            })
    attempted = sum(metrics["unique_hits"] for metrics in backend_metrics.values())
    captured = len(items)
    coverage = (captured / attempted) if attempted else 0.0
    source_quality = {
        "threshold": min_source_coverage,
        "attempted": attempted,
        "captured": captured,
        "coverage": round(coverage, 3),
        # A zero threshold disables the gate, including for a run where every
        # source failed before yielding a hit: otherwise the documented escape
        # hatch cannot be used to inspect exactly that case.
        "meets_threshold": min_source_coverage is None or coverage >= min_source_coverage,
        "backends": {
            backend: {
                **metrics,
                "coverage": round(metrics["captured"] / metrics["unique_hits"], 3)
                if metrics["unique_hits"] else None,
            }
            for backend, metrics in backend_metrics.items()
        },
    }
    return items, {
        "queries": list(queries),
        "hits": hits_seen,
        "items": len(items),
        "skipped": skipped,
        "retried": retried,
        "source_quality": source_quality,
    }

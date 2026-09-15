"""Lane 1: Sourcing & Ingestion.
Discovers and onboards target companies and source records using harness_fleet.discover primitives.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, cast
from urllib.parse import urlparse

from career_fleet.community_sources import COMMUNITY_SOURCE_TYPES
from career_fleet.store import CareerStore

logger = logging.getLogger("career_fleet.lane1")

try:
    from harness_fleet.discover import (
        crawl_site,
        domain_of,
        fetch_ashby_org,
        fetch_devto_tag,
        fetch_discourse_search,
        fetch_greenhouse_board,
        fetch_lemmy,
        fetch_lever_org,
        fetch_lobsters,
        fetch_reddit_posts,
        fetch_reddit_rss,
        fetch_stackexchange_questions,
        fetch_yc_companies,
        run_discovery,
        slugify_id,
    )
    HAS_DISCOVER = True
except ImportError:
    HAS_DISCOVER = False


REMOTE_NEGATION_PATTERN = re.compile(
    r"\b(?:no|not|without)\s+(?:(?:a|an)\s+)?(?:fully\s+)?remote(?:[- ]?(?:role|position|job))?\b|"
    r"\bremote\s+(?:work\s+)?(?:is\s+)?(?:not|never)\s+(?:required|allowed|available|permitted|possible|offered)\b|"
    r"\bremote\s+(?:work\s+)?(?:is\s+)?optional\b|"
    r"\b(?:cannot|can't|will\s+not|won't|may\s+not)\s+(?:(?:be|work)\s+)?(?:fully\s+)?remote(?:ly)?\b",
    re.I,
)
REMOTE_FIRST_PATTERN = re.compile(r"\bremote[- ]first\b", re.I)
REMOTE_POSITIVE_PATTERN = re.compile(
    r"\b(?:fully|100%|completely|entirely)?\s*remote\b|"
    r"\bremote[- ]first\b|\bwork[- ]from[- ]anywhere\b",
    re.I,
)
REMOTE_ROLE_POSITIVE_PATTERN = re.compile(
    r"\bremote[- ]?(?:role|position|job)\b|"
    r"\b(?:this|the|a|your)\s+(?:role|position|job)\s+(?:is\s+)?(?:fully\s+)?remote\b|"
    r"\b(?:can|may|will)\s+work\s+(?:fully\s+)?remotely\b|"
    r"\bremote\s+(?:work\s+)?(?:is\s+)?required\b|"
    r"\bwork\s+from\s+anywhere\b",
    re.I,
)
REMOTE_LOCATION_PATTERN = re.compile(r"\b(?:remote|anywhere)\b", re.I)


CAREER_SIGNAL_PATTERNS = {
    "hiring": re.compile(
        r"\b(?:hiring|hire|recruit(?:ing|er)?|job(?:s)?|opening(?:s)?|apply|application(?:s)?|career(?:s)?)\b",
        re.I,
    ),
    "role": re.compile(
        r"\b(?:engineer(?:ing)?|developer|developers|designer|product manager|staff|principal|intern|contractor|cofounder|co-founder)\b",
        re.I,
    ),
    "workplace": re.compile(
        r"\b(?:remote|hybrid|on[- ]site|onsite|distributed|async|timezone|relocation)\b",
        re.I,
    ),
    "compensation": re.compile(
        r"\b(?:salary|compensation|equity|benefits|paid|bonus|pay range|total rewards)\b",
        re.I,
    ),
    "leadership": re.compile(
        r"\b(?:founder|founders|leadership|manager|management|team|culture|join us|working with)\b",
        re.I,
    ),
}

COMMUNITY_HOSTS = {
    "reddit.com", "redd.it", "news.ycombinator.com", "stackoverflow.com",
    "stackexchange.com", "serverfault.com", "superuser.com", "askubuntu.com",
    "ycombinator.com",
    "lobste.rs", "programming.dev", "dev.to", "github.com", "gitlab.com",
    "bitbucket.org", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "medium.com", "substack.com", "notion.so", "google.com", "docs.google.com",
}
COMPANY_URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]\"']+", re.I)
CAREER_LINK_PATH_PATTERN = re.compile(
    r"(?:career|job|hiring|join|apply|opening|opportunit|work-with-us)", re.I
)
CAREER_LINK_CONTEXT_PATTERN = re.compile(
    r"\b(?:hiring|hire|recruit(?:ing|er)?|job(?:s)?|career(?:s)?|role(?:s)?|position(?:s)?|apply|join|opening(?:s)?|opportunit(?:y|ies)|salary|compensation)\b",
    re.I,
)


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _profile_focus_terms(profile: Any | None) -> list[str]:
    if profile is None:
        return []
    terms: list[str] = []
    for field in ("wedge_capabilities", "required_stack", "hiring_catalysts", "target_leadership"):
        for value in getattr(profile, field, []) or []:
            normalized = str(value).strip().casefold()
            if normalized and normalized not in terms:
                terms.append(normalized)
    return terms


def score_career_signal(record: Any, profile: Any | None = None) -> dict[str, Any]:
    """Classify a community record for career research without inventing fit."""
    title = str(_record_value(record, "title", "") or "")
    text = str(_record_value(record, "text", "") or "")
    metadata = _record_value(record, "metadata", {}) or {}
    haystack = "\n".join([title, text, json.dumps(metadata, ensure_ascii=False)]).strip()
    signal_types = [name for name, pattern in CAREER_SIGNAL_PATTERNS.items() if pattern.search(haystack)]
    profile_terms = _profile_focus_terms(profile)
    matched_profile_terms = [term for term in profile_terms if term in haystack.casefold()]
    # A community item is career-relevant when it has an opportunity/work
    # signal, or an explicit hiring/compensation signal. Profile matches rank
    # the lead but never turn an unrelated post into a company-fit claim.
    relevant = bool(
        "hiring" in signal_types
        or "compensation" in signal_types
        or ("role" in signal_types and ("workplace" in signal_types or "leadership" in signal_types))
    )
    relevance_score = min(
        1.0,
        0.2 * len(signal_types) + 0.1 * min(len(matched_profile_terms), 3),
    )
    return {
        "career_relevant": relevant,
        "relevance_score": round(relevance_score, 3),
        "signal_types": signal_types,
        "profile_matches": matched_profile_terms,
    }


def _registrable_domain(domain: str) -> str:
    parts = [part for part in domain.casefold().split(".") if part]
    if len(parts) <= 2:
        return domain.casefold()
    if len(parts) >= 3 and parts[-1] in {"uk", "au", "nz", "za"} and parts[-2] in {"co", "com", "org", "net"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _community_company_links(
    record: Any,
    ignored_domains: list[str] | None = None,
    *,
    include_source_uri: bool = False,
) -> list[tuple[str, str]]:
    """Return unambiguous external company links, excluding community hosts."""
    text = str(_record_value(record, "text", "") or "")
    metadata = _record_value(record, "metadata", {}) or {}
    ignored = {_registrable_domain(domain_of(value)) for value in (ignored_domains or []) if domain_of(value)}
    if isinstance(metadata, dict) and metadata.get("instance"):
        instance_domain = domain_of(str(metadata["instance"]))
        if instance_domain:
            ignored.add(_registrable_domain(instance_domain))
    candidates: list[tuple[str, bool, str]] = []
    for match in COMPANY_URL_PATTERN.finditer(text):
        start = max(0, match.start() - 140)
        end = min(len(text), match.end() + 140)
        candidates.append((match.group(0), False, text[start:end]))
    source_uri = _record_value(record, "source_uri", "")
    if source_uri and include_source_uri:
        candidates.append((str(source_uri), True, ""))
    for key in ("link", "website", "url"):
        value = metadata.get(key) if isinstance(metadata, dict) else None
        if value:
            candidates.append((str(value), True, ""))
    links: dict[str, str] = {}
    for raw_url, explicit, context in candidates:
        clean_url = raw_url.rstrip(".,;:!?)]}>")
        try:
            host = domain_of(clean_url)
        except Exception:
            host = ""
        if not host:
            continue
        base = _registrable_domain(host)
        if base in COMMUNITY_HOSTS or base in ignored or base in {"t.co", "bit.ly", "tinyurl.com"}:
            continue
        if not explicit:
            path = urlparse(clean_url).path
            if not CAREER_LINK_PATH_PATTERN.search(path) and not CAREER_LINK_CONTEXT_PATTERN.search(context):
                continue
        links.setdefault(base, clean_url)
    return sorted(links.items())


def _company_name_from_domain(domain: str) -> str:
    parts = domain.split(".")
    label = parts[-3] if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net"} and parts[-1] in {"uk", "au", "nz", "za"} else parts[-2]
    return label.replace("-", " ").replace("_", " ").title() or domain


def _source_key(source_type: str, target: str, query: str | None, options: dict[str, Any]) -> str:
    payload = {"source": source_type, "target": target, "query": query or "", "options": options}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]
    return f"{source_type}:{digest}"


def _items_to_records(items: list[Any]) -> list[Any]:
    """Keep the small RawRecord/InputItem adapter boundary in one place."""
    return items


def _expand_hn_records(records: list[Any], max_items: int) -> list[Any]:
    """Split multi-comment HN hiring threads into attributable career leads."""
    expanded: list[Any] = []
    for record in records:
        text = str(_record_value(record, "text", "") or "").strip()
        chunks = re.split(r"\n\n(?=\[[^\]]+\]: )", text)
        if len(chunks) <= 1:
            expanded.append(record)
        else:
            base_id = str(_record_value(record, "item_id", "hn") or "hn")
            parent_title = str(_record_value(record, "title", "Hacker News") or "Hacker News")
            source_uri = _record_value(record, "source_uri", "")
            metadata = _record_value(record, "metadata", {}) or {}
            for index, chunk in enumerate(chunks):
                chunk = chunk.strip()
                if not chunk:
                    continue
                chunk_metadata = dict(metadata) if isinstance(metadata, dict) else {}
                chunk_metadata["parent_item_id"] = base_id
                chunk_metadata["comment_index"] = index
                expanded.append(
                    {
                        "item_id": f"{base_id}-comment-{index}",
                        "title": f"{parent_title} — comment {index}",
                        "text": chunk,
                        "source_uri": source_uri,
                        "metadata": chunk_metadata,
                    }
                )
                if len(expanded) >= max_items:
                    return expanded[:max_items]
        if len(expanded) >= max_items:
            return expanded[:max_items]
    return expanded[:max_items]


def _fetch_community_records(
    source_type: str,
    target: str,
    *,
    query: str | None = None,
    max_items: int,
    subreddit: list[str] | None = None,
    reddit_rss: bool = False,
    subreddit_sort: str = "new",
    se_tagged: list[str] | None = None,
    se_site: str = "stackoverflow",
    se_answers: bool = False,
    discourse_url: str | None = None,
    lemmy_instance: str = "https://programming.dev",
    delay: float = 0.2,
    timeout: float = 20.0,
) -> tuple[list[Any], list[dict[str, str]]]:
    """Fetch full community records using the shared keyless adapters."""
    subreddit = subreddit or []
    se_tagged = se_tagged or []
    search_query = (query or target).strip()
    if not search_query and source_type not in {"discourse", "lobsters", "devto"}:
        raise ValueError(f"{source_type} requires a non-empty career query")
    skipped: list[dict[str, str]] = []
    if source_type == "reddit":
        records: list[Any] = []
        if reddit_rss:
            if not subreddit:
                raise ValueError("reddit RSS requires at least one --subreddit")
            for sub in subreddit:
                try:
                    records.extend(fetch_reddit_rss(sub, sort=subreddit_sort, max_results=max_items, timeout=timeout))
                except Exception as exc:
                    skipped.append({"source": f"reddit-rss:{sub}", "reason": str(exc)})
                if len(records) >= max_items:
                    break
        else:
            try:
                records = fetch_reddit_posts(search_query, subreddits=subreddit, max_posts=max_items, timeout=timeout)
            except Exception as exc:
                skipped.append({"source": "reddit-search", "reason": str(exc)})
        return records[:max_items], skipped
    if source_type == "discourse":
        base = discourse_url or target
        discourse_query = query if query is not None else (target if discourse_url else None)
        records, skipped = fetch_discourse_search(
            base, query=discourse_query, max_topics=max_items, max_posts_each=5, timeout=timeout,
        )
        return records, skipped
    if source_type == "stackexchange":
        try:
            return fetch_stackexchange_questions(
                search_query, tagged=se_tagged, site=se_site, max_questions=max_items,
                include_answers=se_answers, timeout=timeout,
            ), skipped
        except Exception as exc:
            skipped.append({"source": "stackexchange", "reason": str(exc)})
            return [], skipped
    if source_type == "lemmy":
        try:
            return fetch_lemmy(search_query, instance=lemmy_instance, max_results=max_items, timeout=timeout), skipped
        except Exception as exc:
            skipped.append({"source": f"lemmy:{lemmy_instance}", "reason": str(exc)})
            return [], skipped
    if source_type == "devto":
        try:
            return fetch_devto_tag(target.strip(), max_articles=max_items, full_body=True, timeout=timeout), skipped
        except Exception as exc:
            skipped.append({"source": "dev.to", "reason": str(exc)})
            return [], skipped
    if source_type == "lobsters":
        try:
            if query:
                items, report = run_discovery(
                    [query], backends=["lobsters"], max_results=max_items,
                    fetch_full_text=True, timeout=timeout, delay=delay,
                )
                return _items_to_records(items), list(report.get("skipped") or [])
            tag = target.strip() or None
            if tag in {"new", "newest", "all"}:
                tag = None
            return fetch_lobsters(tag=tag, max_results=max_items, timeout=timeout), skipped
        except Exception as exc:
            skipped.append({"source": "lobsters", "reason": str(exc)})
            return [], skipped
    if source_type == "hn":
        try:
            items, report = run_discovery(
                [search_query], backends=["hn"], max_results=max_items,
                fetch_full_text=True, timeout=timeout, delay=delay,
            )
            return _expand_hn_records(_items_to_records(items), max_items), list(report.get("skipped") or [])
        except Exception as exc:
            skipped.append({"source": "hn", "reason": str(exc)})
            return [], skipped
    raise ValueError(f"Unknown community source_type '{source_type}'")


def _is_remote_listing(text: str, location: str | None = None) -> bool:
    """Return True only when source text contains positive remote evidence."""
    value = text or ""
    if REMOTE_NEGATION_PATTERN.search(value):
        return False
    if location and not REMOTE_LOCATION_PATTERN.search(location):
        return bool(REMOTE_ROLE_POSITIVE_PATTERN.search(value))
    if REMOTE_FIRST_PATTERN.search(value) and not REMOTE_ROLE_POSITIVE_PATTERN.search(value):
        # "Remote-first" describes a company policy, not necessarily this role.
        return False
    return bool(REMOTE_POSITIVE_PATTERN.search(value))


def _item_metadata(item: Any, key: str) -> str | None:
    metadata = getattr(item, "metadata", {}) or {}
    value = metadata.get(key)
    return str(value).strip() if value is not None else None


def _item_int_metadata(item: Any, key: str) -> int | None:
    value = _item_metadata(item, key)
    if not value:
        return None
    try:
        return int(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _item_domain(item: Any) -> str | None:
    # YC's source URI is often the shared directory page. It is not a
    # company domain and must not be used as a unique-domain fallback.
    website = _item_metadata(item, "website")
    if not website:
        return None
    candidate = str(website).strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    return domain_of(candidate) or None


def run_lane1_sourcing(
    store: CareerStore,
    source_type: str,
    target: str,
    max_items: int = 50,
    *,
    profile: Any | None = None,
    query: str | None = None,
    subreddit: list[str] | None = None,
    reddit_rss: bool = False,
    subreddit_sort: str = "new",
    se_tagged: list[str] | None = None,
    se_site: str = "stackoverflow",
    se_answers: bool = False,
    discourse_url: str | None = None,
    lemmy_instance: str = "https://programming.dev",
    include_low_signal: bool = False,
    delay: float = 0.2,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Discover companies or postings and record them in the store.

    Company/job sources use their existing targets. Community sources use
    career-focused queries/tags and retain unlinked records as durable leads;
    a record is linked to a company only when exactly one external company
    domain can be identified.
    """
    if not HAS_DISCOVER:
        raise RuntimeError("harness_fleet.discover is required. Install with pip install 'career-fleet[discover]'.")
    if max_items < 1:
        raise ValueError("max_items must be at least 1")

    profile_revision_id = store.save_profile(profile) if profile is not None else None
    discovered = 0
    jobs_added = 0
    pages_skipped = 0
    community_signals_added = 0
    unlinked_signals = 0
    low_signal_skipped = 0
    warnings: list[str] = []

    try:
        if source_type == "yc":
            # Target could be batch e.g. "W24" or keyword
            normalized_target = target.strip()
            is_batch = bool(re.fullmatch(r"[WSws]\d+", normalized_target))
            items = fetch_yc_companies(
                batch=normalized_target.upper() if is_batch else None,
                query=None if is_batch else normalized_target,
                max_companies=max_items,
            )
            snapshots: dict[str, list[dict[str, Any]]] = {}
            for it in items:
                source_record_id = it.item_id
                cid = cast(str, source_record_id)
                name = it.title or cid
                website = _item_metadata(it, "website") or it.source_uri
                cid = store.upsert_company(
                    company_id=cid,
                    name=name,
                    domain=_item_domain(it),
                    headcount=_item_int_metadata(it, "team_size"),
                    hq_location=_item_metadata(it, "hq_location") or _item_metadata(it, "location"),
                    timezone=_item_metadata(it, "timezone"),
                    website_url=website,
                    status="discovered",
                )
                snapshots.setdefault(cid, []).append(
                    {
                        "id": f"job-{source_record_id}-profile",
                        "company_id": cid,
                        "title": f"{name} - Overview",
                        "raw_text": it.text,
                        "job_url": it.source_uri,
                        "timezone": _item_metadata(it, "timezone"),
                        "is_remote": _is_remote_listing(it.text),
                    }
                )
                discovered += 1
            for cid, postings in snapshots.items():
                if store.replace_company_source_snapshot(cid, source_type, postings):
                    jobs_added += len(postings)

        elif source_type == "greenhouse":
            items = fetch_greenhouse_board(board=target, max_jobs=max_items)
            cid = slugify_id(target)
            cid = store.upsert_company(
                company_id=cid,
                name=target.capitalize(),
                ats_provider="greenhouse",
                ats_token=target,
                status="discovered",
            )
            discovered += 1
            postings = [
                {
                    "id": it.item_id,
                    "company_id": cid,
                    "title": it.title or "Unknown Role",
                    "raw_text": it.text,
                    "job_url": it.source_uri,
                    "location": _item_metadata(it, "location"),
                    "timezone": _item_metadata(it, "timezone"),
                    "is_remote": _is_remote_listing(it.text, _item_metadata(it, "location")),
                }
                for it in items
            ]
            if postings:
                store.replace_company_source_snapshot(cid, source_type, postings)
                jobs_added += len(postings)
            else:
                warnings.append(f"{source_type} returned no postings; kept the previous snapshot for {cid}")

        elif source_type == "ashby":
            items = fetch_ashby_org(org=target, max_jobs=max_items)
            cid = slugify_id(target)
            cid = store.upsert_company(
                company_id=cid,
                name=target.capitalize(),
                ats_provider="ashby",
                ats_token=target,
                status="discovered",
            )
            discovered += 1
            postings = [
                {
                    "id": it.item_id,
                    "company_id": cid,
                    "title": it.title or "Unknown Role",
                    "raw_text": it.text,
                    "job_url": it.source_uri,
                    "location": _item_metadata(it, "location"),
                    "timezone": _item_metadata(it, "timezone"),
                    "is_remote": _is_remote_listing(it.text, _item_metadata(it, "location")),
                }
                for it in items
            ]
            if postings:
                store.replace_company_source_snapshot(cid, source_type, postings)
                jobs_added += len(postings)
            else:
                warnings.append(f"{source_type} returned no postings; kept the previous snapshot for {cid}")

        elif source_type == "lever":
            items = fetch_lever_org(org=target, max_jobs=max_items)
            cid = slugify_id(target)
            cid = store.upsert_company(
                company_id=cid,
                name=target.capitalize(),
                ats_provider="lever",
                ats_token=target,
                status="discovered",
            )
            discovered += 1
            postings = [
                {
                    "id": it.item_id,
                    "company_id": cid,
                    "title": it.title or "Unknown Role",
                    "raw_text": it.text,
                    "job_url": it.source_uri,
                    "location": _item_metadata(it, "location"),
                    "timezone": _item_metadata(it, "timezone"),
                    "is_remote": _is_remote_listing(it.text, _item_metadata(it, "location")),
                }
                for it in items
            ]
            if postings:
                store.replace_company_source_snapshot(cid, source_type, postings)
                jobs_added += len(postings)
            else:
                warnings.append(f"{source_type} returned no postings; kept the previous snapshot for {cid}")

        elif source_type == "site":
            parsed_target = urlparse(target)
            site_domain = domain_of(target)
            if parsed_target.scheme not in ("http", "https") or not site_domain:
                raise ValueError("site target must be an absolute http:// or https:// URL with a host")
            items, skipped = crawl_site(start_url=target, max_pages=max_items)
            pages_skipped = len(skipped)
            existing = store.get_company_by_domain(site_domain)
            cid = existing["id"] if existing else slugify_id(site_domain)
            cid = store.upsert_company(
                company_id=cid,
                name=existing["name"] if existing else site_domain,
                domain=site_domain,
                website_url=target,
                status="discovered",
            )
            discovered += 1
            postings = [
                {
                    "id": it.item_id,
                    "company_id": cid,
                    "title": it.title or "Site Page",
                    "raw_text": it.text,
                    "job_url": it.source_uri,
                    "location": _item_metadata(it, "location"),
                    "timezone": _item_metadata(it, "timezone"),
                    "is_remote": _is_remote_listing(it.text, _item_metadata(it, "location")),
                }
                for it in items
            ]
            if postings:
                store.replace_company_source_snapshot(cid, source_type, postings)
                jobs_added += len(postings)
            else:
                warnings.append(f"{source_type} returned no pages; kept the previous snapshot for {cid}")

        elif source_type in COMMUNITY_SOURCE_TYPES:
            options = {
                "subreddit": subreddit or [],
                "reddit_rss": reddit_rss,
                "subreddit_sort": subreddit_sort,
                "se_tagged": se_tagged or [],
                "se_site": se_site,
                "se_answers": se_answers,
                "discourse_url": discourse_url or "",
                "lemmy_instance": lemmy_instance,
            }
            records, skipped = _fetch_community_records(
                source_type,
                target,
                query=query,
                max_items=max_items,
                subreddit=subreddit,
                reddit_rss=reddit_rss,
                subreddit_sort=subreddit_sort,
                se_tagged=se_tagged,
                se_site=se_site,
                se_answers=se_answers,
                discourse_url=discourse_url,
                lemmy_instance=lemmy_instance,
                delay=delay,
                timeout=timeout,
            )
            pages_skipped = len(skipped)
            source_key = _source_key(source_type, target, query, options)
            signals: list[dict[str, Any]] = []
            linked_company_ids: set[str] = set()
            seen_signal_ids: set[str] = set()
            for record in records:
                source_uri = str(_record_value(record, "source_uri", "") or "").strip()
                raw_text = str(_record_value(record, "text", "") or "").strip()
                if not source_uri or not raw_text:
                    warnings.append(f"{source_type} record skipped because it has no durable source URI or text")
                    continue
                focus = score_career_signal(record, profile)
                if not include_low_signal and not focus["career_relevant"]:
                    low_signal_skipped += 1
                    continue
                title = str(_record_value(record, "title", "") or "Community career signal").strip()[:500]
                metadata = _record_value(record, "metadata", {}) or {}
                metadata = dict(metadata) if isinstance(metadata, dict) else {}
                metadata["career_focus"] = {
                    "relevant": focus["career_relevant"],
                    "relevance_score": focus["relevance_score"],
                    "signal_types": focus["signal_types"],
                    "profile_matches": focus["profile_matches"],
                }
                ignored_domains: list[str] = []
                if source_type == "discourse":
                    ignored_domains.append(discourse_url or target)
                elif source_type == "lemmy":
                    ignored_domains.append(lemmy_instance)
                links = (
                    _community_company_links(
                        record,
                        ignored_domains=ignored_domains,
                        include_source_uri=source_type == "hn",
                    )
                    if focus["career_relevant"]
                    else []
                )
                company_id = None
                if len(links) == 1:
                    company_domain, company_url = links[0]
                    existing = store.get_company_by_domain(company_domain)
                    company_id = store.upsert_company(
                        company_id=existing["id"] if existing else slugify_id(company_domain),
                        name=existing["name"] if existing else _company_name_from_domain(company_domain),
                        domain=company_domain,
                        website_url=existing.get("website_url") if existing else company_url,
                        status="discovered",
                    )
                    linked_company_ids.add(company_id)
                    metadata["company_domain"] = company_domain
                else:
                    unlinked_signals += 1
                    if len(links) > 1:
                        metadata["company_link_status"] = "ambiguous_multiple_domains"
                raw_id = str(_record_value(record, "item_id", "") or "").strip() or source_uri
                signal_id = slugify_id(f"community-{source_type}-{raw_id}")
                if signal_id in seen_signal_ids:
                    suffix = hashlib.sha256(f"{source_uri}\n{raw_text}".encode()).hexdigest()[:8]
                    signal_id = slugify_id(f"{signal_id}-{suffix}")
                seen_signal_ids.add(signal_id)
                signals.append(
                    {
                        "id": signal_id,
                        "title": title or "Community career signal",
                        "source_uri": source_uri,
                        "raw_text": raw_text,
                        "relevance_score": focus["relevance_score"],
                        "signal_types": focus["signal_types"],
                        "metadata": metadata,
                        "company_id": company_id,
                    }
                )
            snapshot = store.replace_community_source_snapshot(
                source_type,
                source_key,
                signals,
                profile_revision_id=profile_revision_id,
            )
            community_signals_added = snapshot["signals_added"]
            jobs_added = snapshot["linked_postings_added"]
            discovered = snapshot["linked_companies"]
            if not records:
                warnings.append(f"{source_type} returned no records; kept the previous snapshot")
            if low_signal_skipped:
                warnings.append(f"{low_signal_skipped} records omitted by the career-focus filter")
            if skipped:
                warnings.extend(f"{entry.get('source', source_type)}: {entry.get('reason', 'skipped')}" for entry in skipped)

        else:
            choices = ", ".join(("yc", "greenhouse", "ashby", "lever", "site", *COMMUNITY_SOURCE_TYPES))
            raise ValueError(f"Unknown source_type '{source_type}'. Choose from {choices}.")

        result = {
            "status": "success",
            "source_type": source_type,
            "target": target,
            "companies_discovered": discovered,
            "postings_added": jobs_added,
            "pages_skipped": pages_skipped,
        }
        if source_type in COMMUNITY_SOURCE_TYPES:
            result.update(
                {
                    "community_signals_added": community_signals_added,
                    "unlinked_signals": unlinked_signals,
                    "low_signal_skipped": low_signal_skipped,
                }
            )
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as exc:
        error = str(exc)
        if "career-fleet" not in error:
            error = error.replace("account-fleet", "career-fleet")
        logger.error(f"Error fetching from {source_type} ({target}): {error}")
        return {
            "status": "error",
            "source_type": source_type,
            "target": target,
            "error": error,
            "companies_discovered": discovered,
            "postings_added": jobs_added,
            "pages_skipped": pages_skipped,
        }

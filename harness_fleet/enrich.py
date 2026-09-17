"""Go to an entity's own surfaces and collect the evidence it is missing.

Discovery finds pages *about* a company. A page about a company is not the
company: what it builds, what it delivered, who it hired and what it charges
live on its own site, on its applicant-tracking board, on the directories that
review it, and in the stories its vendors publish naming it. Scoring a search
result as though it were a dossier is how a lane ends up demanding evidence it
never went to get.

This is the stage that walks those surfaces. It is deliberately shared: the
mechanism is identical whether the entity is a target account, an implementation
partner or an employer, so it lives here once and every lane calls it. What
differs between products is *which kinds they are missing*, and that is already
declared — the lane's bar, and the central contracts that map a kind to the
surfaces carrying it.

Surface knowledge (which paths a website keeps its case studies under, which
ATS hosts exist) is data in ``data/source_surfaces.json``; nothing about a
product appears in this module.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .sources import canonicalize_entity_id

if TYPE_CHECKING:  # the record type lives with the fetchers; importing it at
    # runtime would drag the whole discovery stack into anything that only wants
    # to know where a kind of evidence lives.
    from .discover import RawRecord

SURFACES_PATH = Path(__file__).resolve().parent / "data" / "source_surfaces.json"
#: A name shorter than this is too generic to prove attribution on its own
#: ("acme", "data", "ably"), so only the full domain counts for those. This is
#: the threshold the partner product shipped with, and it now applies
#: everywhere: loosening it would let a common English word attribute a page to
#: a company in every lane at once.
_ATTRIBUTION_MIN_NAME = 6
#: CMS assets live under the same paths as stories (background images, headers,
#: logos) and are never prose, so they are dropped before any fetch.
_ASSET_HINTS = (
    "background", "asset", "header", "logo", "icon", "font", "sprite",
    "screenshot", "thumbnail", "avatar", "placeholder",
    ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".zip",
)
_SURFACE_CACHE: dict[str, Any] = {}


class EnrichError(RuntimeError):
    """The surfaces this install ships could not be read."""


def _without_comments(value: Any) -> Any:
    """Drop ``_``-prefixed keys: in this plan they are prose, not surfaces.

    The plan explains itself inline, and the explanation sits *inside* the maps
    a reader enumerates. So ``first_party_paths`` was read as naming a surface
    called ``_comment`` with 103 paths — the comment string, taken character by
    character — and ``channels`` and ``community`` each named one too. Nothing
    fetched those, but a report of what this install reads counted three
    surfaces that do not exist, and any code that walked the maps would have
    built a URL out of the letter "F". Comments are for people; leave them out
    of the maps the walk reads.
    """
    if isinstance(value, dict):
        return {
            key: _without_comments(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_without_comments(item) for item in value]
    return value


def load_surfaces(path: str | Path | None = None) -> dict[str, Any]:
    """The shipped surface plan: where each kind of evidence lives."""
    target = Path(path).expanduser() if path else SURFACES_PATH
    key = str(target)
    if key in _SURFACE_CACHE:
        return _SURFACE_CACHE[key]
    try:
        payload = _without_comments(json.loads(target.read_text(encoding="utf-8")))
    except OSError as exc:
        raise EnrichError(f"could not read the surface plan at {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EnrichError(f"surface plan is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnrichError(f"surface plan at {target} is not an object")
    _SURFACE_CACHE[key] = payload
    return payload


def entity_domain(entity: str) -> str:
    """The domain to visit for this entity, or "" when it does not name one.

    An entity key is usually a domain, but not always: a posting on a niche
    board can be filed under the employer's slug ("viking_cloud_inc"), and a
    page that names nobody keeps its own host. There is no website to walk in
    those cases, and saying so is better than crawling something unrelated.
    """
    key = canonicalize_entity_id(str(entity or "").strip())
    if not key or key == "unknown_entity" or "." not in key:
        return ""
    host = key.split("/")[0].strip(".")
    if not host or host.count(".") < 1:
        return ""
    label = host.rsplit(".", 1)[0]
    if not label or len(label) < 2:
        return ""
    return host


# ---------------------------------------------------------------------------
# Attribution: a page counts for an entity only when it is about them
# ---------------------------------------------------------------------------

def attribution_terms(entity: str) -> tuple[str, ...]:
    """Terms that prove a page is about this entity, strongest first."""
    raw = str(entity or "").strip()
    if not raw:
        return ()
    canonical = (canonicalize_entity_id(raw) or raw).strip().lower()
    if not canonical:
        return ()
    terms = [canonical]
    label = canonical.split(".")[0]
    if len(label) >= _ATTRIBUTION_MIN_NAME and label != canonical:
        terms.append(label)
    return tuple(dict.fromkeys(terms))


def is_attributed(text: str | None, uri: str | None, entity: str) -> bool:
    """True when this page is about `entity`, not merely matched by keywords."""
    uri_l = (uri or "").lower()
    text_l = (text or "").lower()
    for term in attribution_terms(entity):
        if term in uri_l:
            return True
        if any(ch.isdigit() for ch in term):
            # "trace3" is that entity's name and does not appear by accident —
            # but it must be the whole word, so "trace33" is a different firm.
            if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text_l):
                return True
        elif f" {term} " in f" {text_l} ":
            # A bare word must stand alone: "trace" must not match "tracing".
            return True
    return False


def filter_attributed(
    records: Iterable[RawRecord], entity: str
) -> tuple[list[RawRecord], list[RawRecord]]:
    """Split records into (about this entity, everything else)."""
    kept: list[RawRecord] = []
    dropped: list[RawRecord] = []
    for record in records:
        target = kept if is_attributed(record.text, record.source_uri, entity) else dropped
        target.append(record)
    return kept, dropped


# ---------------------------------------------------------------------------
# Raw markup: link and sitemap discovery needs hrefs, not prose
# ---------------------------------------------------------------------------

def fetch_markup(url: str, *, timeout: float = 20.0, respect_robots: bool = True) -> str:
    """Raw HTML/XML for link and sitemap discovery.

    Prose extraction strips the markup, so hrefs and <loc> entries have to come
    from the raw response: ``fetch_text`` is for the story text, not for
    finding it.
    """
    import httpx

    from .discover import USER_AGENT, robots_allowed

    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        if respect_robots and not robots_allowed(url, client, timeout=timeout):
            raise EnrichError(f"robots.txt disallows {url}")
        response = client.get(url)
    if response.status_code != 200:
        raise EnrichError(f"HTTP {response.status_code} for {url}")
    return response.text


# Kept as a private alias so callers that patched the old name keep working.
_fetch_markup = fetch_markup


# ---------------------------------------------------------------------------
# Vendor stories: independent prose published by a vendor, naming the entity
# ---------------------------------------------------------------------------

#: How many files of a vendor's sitemap index to open before giving up on
#: filling the quota. A story index is a handful of files; the rest of a large
#: index is product and blog pages we were downloading for nothing.
max_nested_maps = 6


def vendor_story_urls(
    sources: dict[str, Any] | None = None,
    *,
    vendors: Sequence[str] | None = None,
    max_per_vendor: int = 20,
    timeout: float = 20.0,
    respect_robots: bool = True,
) -> tuple[list[str], list[dict[str, str]]]:
    """Enumerate a vendor's customer/partner story URLs from its own site.

    Vendor-published stories are independent prose about the work, and the plan
    lists where they live: a story hub whose links are the stories (AWS), or a
    sitemap index walked one level and filtered to the story paths (Snowflake,
    Databricks, Elastic, Datadog, MongoDB). Enumerated rather than searched,
    because review sites that used to supply this now answer 403.
    """
    configured = ((sources or load_surfaces()).get("vendor_stories") or {}).get("vendors") or {}
    wanted = [v for v in (vendors or configured) if v in configured]
    # Vendors publish on their own hosts, so their indexes are walked at once:
    # sequentially, six vendors' sitemap chains were six slow hosts in a row.
    # Each chain stays sequential inside its own worker — every step depends on
    # the last — and vendor order is preserved in what comes back.
    specs = [(vendor, configured.get(vendor) or {}) for vendor in wanted]
    if not specs:
        return [], []
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(specs)))) as pool:
        walked = list(pool.map(
            lambda pair: _vendor_story_urls_one(
                pair[0], pair[1], max_per_vendor=max_per_vendor,
                timeout=timeout, respect_robots=respect_robots,
            ),
            specs,
        ))
    urls: list[str] = []
    skipped: list[dict[str, str]] = []
    for found, vendor_skipped in walked:
        urls.extend(found)
        skipped.extend(vendor_skipped)
    return list(dict.fromkeys(urls)), skipped


def _vendor_story_urls_one(
    vendor: str,
    spec: dict[str, Any],
    *,
    max_per_vendor: int,
    timeout: float,
    respect_robots: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """One vendor's story URLs, walked on its own host."""
    urls: list[str] = []
    skipped: list[dict[str, str]] = []
    hub = spec.get("hub")
    prefix = spec.get("story_path_prefix") or ""
    if hub:
        try:
            markup = fetch_markup(hub, timeout=timeout, respect_robots=respect_robots)
        except Exception as exc:
            skipped.append({"vendor": vendor, "reason": str(exc)[:200]})
            return urls, skipped
        found = []
        for match in re.finditer(r'href="([^"#?]+)"', markup):
            link = match.group(1)
            if prefix not in link:
                continue
            # A story has a slug after the prefix; the hub itself (and its
            # localized twins) does not, so those are not stories.
            if not link.split(prefix, 1)[1].strip("/"):
                continue
            found.append(link if link.startswith("http") else f"https://{urlparse(hub).netloc}{link}")
        urls.extend(list(dict.fromkeys(found))[:max_per_vendor])
        return urls, skipped

    sitemap = spec.get("sitemap")
    if not sitemap:
        return urls, skipped
    paths = tuple(spec.get("story_paths") or ())
    # Walk the index only as far as it takes to fill this vendor's quota,
    # and only into the nested sitemaps that can hold stories. Downloading
    # every nested file of a large index and then keeping forty URLs is what
    # turned a gather stage into most of an hour: a vendor's index can list
    # hundreds of thousands of pages across dozens of files.
    queue: list[str] = [sitemap]
    visited_maps = 0
    story_urls: list[str] = []
    while queue and len(story_urls) < max_per_vendor and visited_maps < max_nested_maps:
        sitemap_url = queue.pop(0)
        try:
            text = fetch_markup(sitemap_url, timeout=timeout, respect_robots=respect_robots)
        except Exception as exc:
            skipped.append({"vendor": vendor, "reason": str(exc)[:200]})
            continue
        visited_maps += 1
        locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)\s*(?:\]\]>)?\s*</loc>", text)
        nested = [loc for loc in locs if loc.endswith(".xml")]
        pages = [loc for loc in locs if not loc.endswith(".xml")]
        if nested and not pages:
            # An index: queue the files that can hold stories, stories first.
            # A vendor names them freely — Snowflake's 58 children carry none
            # of the story words — so the named ones are a preference, not a
            # filter, and the walk keeps opening files until the quota is met
            # or the budget runs out.
            named = [loc for loc in nested if any(str(path).strip("/").split("/")[0] in loc for path in paths)]
            rest = [loc for loc in nested if loc not in named]
            queue.extend((named + rest)[: max_nested_maps * 3])
            continue
        # A flat sitemap lists its pages directly — Elastic publishes 13,645
        # of them in one 7MB file with nothing nested. Treating that as an
        # index collected nothing at all.
        story_urls.extend(
            loc for loc in pages
            if not paths or any(path in loc for path in paths)
        )
        if len(story_urls) >= max_per_vendor:
            break
    story_urls = list(dict.fromkeys(story_urls))
    filtered = [u for u in story_urls if not any(hint in u.lower() for hint in _ASSET_HINTS)]
    urls.extend(list(dict.fromkeys(filtered))[:max_per_vendor])
    return urls, skipped


def _pace_host(url: str, delay: float) -> None:
    """Space requests to one host while other hosts proceed.

    The walk handles many entities at once, and a hiring board or a code host is
    the same host for all of them, so politeness has to be per host rather than
    per entity.
    """
    from .discover import _space_host
    from .sources import host_of

    host = host_of(url)
    if host:
        _space_host(host, delay)


def _fetch_pages_many(
    urls: Sequence[str],
    fetch: Callable[..., Any],
    *,
    timeout: float,
    respect_robots: bool,
    pace: float,
    workers: int = 8,
) -> list[tuple[str, bool, Any]]:
    """Fetch independent pages at once; results arrive in input order.

    Vendors publish on their own hosts, so one slow host must not serialize
    every other host's pages: sequentially, a partner lane's story budget ran
    for tens of minutes. Per-host pacing still applies inside each worker, so
    breadth does not cost politeness, and input order is preserved so reports
    read the same at any worker count.
    """
    targets = list(urls)

    def fetch_one(url: str) -> tuple[str, bool, Any]:
        try:
            if pace > 0:
                _pace_host(url, pace)
            return (url, True, fetch(url, timeout=timeout, respect_robots=respect_robots))
        except Exception as exc:
            return (url, False, exc)

    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
        return list(pool.map(fetch_one, targets))


def story_could_name(url: str, entity: str, *, sources: dict[str, Any] | None = None) -> bool:
    """Whether a story URL could possibly be about this entity, before fetching it.

    A vendor's index holds hundreds of stories and almost none of them are about
    the company in hand: fetching all of them to test attribution reads a
    hundred pages to keep one or two. The story's own address already names its
    subject — that is how candidates are found in the first place — so it can be
    compared with the entity before a single request is spent.

    Deliberately permissive. A slug is a guess at a name, not a name: AWS files
    its stories as ``<customer>-<partner>`` so any token may be the subject, and
    a vendor may slug a story after the product rather than the company. So this
    only rejects a story whose address has nothing of the entity in it, and the
    caller falls back to reading everything when the filter would leave nothing
    at all.
    """
    from .sources import host_of

    domain = canonicalize_entity_id(entity) or entity
    label = domain.split(".")[0].lower()
    if not label:
        return True
    slug, _vendor = story_entity(url, sources=sources)
    haystack = f"{slug} {urlparse(url).path} {host_of(url)}".lower()
    # Compare on letters and digits only: "publicis-groupe" is publicisgroupe.
    squashed = re.sub(r"[^a-z0-9]", "", haystack)
    return label in haystack or re.sub(r"[^a-z0-9]", "", label) in squashed


def _story_address_is_opaque(url: str, *, sources: dict[str, Any] | None = None) -> bool:
    """Whether a story's address cannot be read as a name at all.

    AWS files stories as ``<customer>-<partner>`` under one hub: the slug holds
    two companies and nothing says which half is which, so an address like that
    cannot rule a candidate out and has to be read. Every other vendor files
    under ``/customers/<slug>``, which is a name we can compare.
    """
    slug, vendor = story_entity(url, sources=sources)
    return bool(vendor) and not slug


def fetch_vendor_stories(
    entity: str,
    *,
    sources: dict[str, Any] | None = None,
    vendors: Sequence[str] | None = None,
    max_per_vendor: int = 20,
    timeout: float = 20.0,
    respect_robots: bool = True,
    urls: Sequence[str] | None = None,
    pace: float = 0.0,
) -> tuple[list[RawRecord], list[dict[str, str]]]:
    """Vendor stories that actually name *entity*.

    Enumerating a vendor's stories is not evidence about an entity: the story
    has to name them. Attribution decides, exactly as it does for search hits,
    so a story that credits nobody is dropped rather than filed under whoever
    happened to be nearby.
    """
    from .discover import fetch_text

    domain = canonicalize_entity_id(entity) or entity
    # An already-enumerated list is reused: enumerating a vendor's index costs a
    # dozen requests, and doing that again for every entity turned the walk into
    # the slowest stage of a run.
    if urls is None:
        candidates, skipped = vendor_story_urls(
            sources, vendors=vendors, max_per_vendor=max_per_vendor, timeout=timeout,
            respect_robots=respect_robots,
        )
    else:
        candidates, skipped = list(urls), []
    # One request per story that could be about this entity, not one per story
    # the vendor ever published. Reading the whole index to keep one or two was
    # the single largest cost in a run; the address usually settles it.
    # Filter where the address can be read, and read blind only where it cannot.
    # A whole-index fallback ("nothing resembles them, so read everything") is
    # not a safety net: it fires for every company that simply is not in the
    # index, which is most of them, and it turned this filter into a no-op —
    # measured at 65 of 79 requests on a single entity.
    plausible = [url for url in candidates if story_could_name(url, domain, sources=sources)]
    blind = [url for url in candidates if _story_address_is_opaque(url, sources=sources)]
    plausible = list(dict.fromkeys(plausible + blind))
    ruled_out = len(candidates) - len(plausible)
    if ruled_out:
        skipped.append({
            "source": "vendor_stories",
            "reason": (
                f"{ruled_out} of {len(candidates)} stories do not name {domain} in their "
                "address, so they were not read"
            ),
        })
    fetched = _fetch_pages_many(
        plausible, fetch_text, timeout=timeout,
        respect_robots=respect_robots, pace=pace,
    )
    records: list[RawRecord] = []
    for url, ok, outcome in fetched:
        if not ok:
            skipped.append({"url": url, "reason": str(outcome)[:200]})
            continue
        if is_attributed(outcome.text, outcome.source_uri, domain):
            records.append(outcome)
    return records, skipped


# ---------------------------------------------------------------------------
# The stage itself
# ---------------------------------------------------------------------------

@dataclass
class EnrichReport:
    """What the walk did, so a run can explain itself instead of thinning."""

    entity: str = ""
    domain: str = ""
    kinds: list[str] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    visited: int = 0
    kept: int = 0
    #: Records collected per surface. A surface that returned nothing is as
    #: much a fact about the run as one that returned five.
    by_surface: dict[str, int] = field(default_factory=dict)
    #: Records a surface produced that were left out of the dossier, by surface.
    trimmed: dict[str, int] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "domain": self.domain,
            "kinds": list(self.kinds),
            "surfaces": list(self.surfaces),
            "visited": self.visited,
            "kept": self.kept,
            "by_surface": dict(self.by_surface),
            "trimmed": dict(self.trimmed),
            "skipped": self.skipped,
        }


def story_entity(
    url: str, *, sources: dict[str, Any] | None = None, partner_half: bool = False
) -> tuple[str, str]:
    """The company a vendor story is about, and the vendor that published it.

    A story's own address names the customer: vendors file them under
    ``/customers/<slug>`` or ``/case-studies/<slug>``, declared per vendor in the
    surface plan. That slug is a *name*, not a host, so a candidate found this
    way can be scored on the vendor's prose — which is exactly the independent
    evidence the bar asks for — but the walk has no website to visit and says so
    rather than looking for one.

    AWS is the exception and is left unnamed: its stories are filed as
    ``<customer>-<partner>`` and nothing in the slug says which half is which,
    and guessing would attribute the story to the wrong firm.
    """
    from .sources import host_of

    plan = sources or load_surfaces()
    vendors = (plan.get("vendor_stories") or {}).get("vendors") or {}
    path = urlparse(url).path.lower()
    host = host_of(url)
    for vendor, spec in vendors.items():
        if not isinstance(spec, dict):
            continue
        declared = host_of(str(spec.get("sitemap") or spec.get("hub") or ""))
        # The vendor is whoever's own site the story is on. Matching on the path
        # prefix instead labelled a Snowflake story as Databricks, because both
        # file their stories under /customers/.
        if not declared or not (host == declared or host.endswith("." + declared)):
            continue
        if spec.get("hub"):
            # A hub feed files stories as ``<customer>-<partner>``, which the
            # plan declares: the customer comes first, so the partner half is
            # everything after the first token. ``partner_half`` asks for it.
            slug = urlparse(url).path.strip("/").split("/")[-1].strip()
            if not slug:
                return "", vendor
            if partner_half and "-" in slug:
                return slug.split("-", 1)[1].replace("-", "_"), vendor
            return "", vendor
        for prefix in spec.get("story_paths") or ():
            marker = str(prefix).lower()
            if marker not in path:
                continue
            tail = path.split(marker, 1)[1]
            slug = tail.strip("/").split("/")[0].strip()
            slug = re.sub(r"\.(html?|php|aspx)$", "", slug)
            slug = re.sub(r"[-_](?:\d{4,}|[0-9a-f]{8,})$", "", slug)
            if len(slug) < 3:
                continue
            return slug.replace("-", "_"), vendor
    return "", ""


#: Hosts that answer a "who is this company" query without being the company:
#: encyclopedias, directories, job boards and social platforms. A candidate's
#: own site is what is left after these are removed.
NOT_THE_COMPANY = (
    "wikipedia.org", "wikidata.org", "crunchbase.com", "bloomberg.com",
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "glassdoor.com", "indeed.com", "g2.com", "clutch.co", "zoominfo.com",
    "youtube.com", "medium.com", "reddit.com", "github.com", "gitlab.com",
    "apollo.io", "pitchbook.com", "owler.com", "dnb.com", "trustpilot.com",
)


def resolve_entity_domain(name: str, *, timeout: float = 20.0, backends: Sequence[str] = ("ddgs",)) -> str:
    """A company name to its own domain, or "" when that cannot be established.

    The story indexes give hundreds of candidate companies by name, and a name
    cannot be walked: the evidence the bar wants lives on the company's own
    site. A vendor's story page does not link the customer (Databricks' 13 links
    are all CDNs), so the name has to be resolved. The site a company query
    returns most often, once encyclopedias, directories and job boards are
    removed, is the company's site.

    Returns "" rather than guessing: a wrong domain would attach a stranger's
    case studies to this candidate, which is worse than leaving it un-walkable.
    """
    from .discover import web_search
    from .sources import host_of, is_source_host

    cleaned = str(name or "").replace("_", " ").strip()
    if len(cleaned) < 3:
        return ""
    try:
        hits = web_search(f"{cleaned} official site", backends=list(backends), max_results=5, timeout=timeout)
    except Exception:
        return ""
    counts: dict[str, int] = {}
    for hit in hits:
        domain = host_of(hit.url)
        if not domain or "." not in domain:
            continue
        if is_source_host(domain) or any(bad in domain for bad in NOT_THE_COMPANY):
            continue
        counts[domain] = counts.get(domain, 0) + 1
    if not counts:
        return ""
    best = max(counts.items(), key=lambda pair: (pair[1], -len(pair[0])))
    # A single mention among five is a different claim from three of five.
    return best[0] if best[1] >= 1 else ""


def story_candidates(
    *,
    per_vendor: int = 40,
    story_paths: Sequence[str] = (),
    sources: dict[str, Any] | None = None,
    timeout: float = 20.0,
    respect_robots: bool = True,
    max_pages: int | None = None,
    resolve_limit: int = 0,
    partner_half: bool = False,
    pace: float = 1.0,
) -> tuple[list[Any], list[dict[str, str]]]:
    """Candidate entities from the stories vendors publish about their customers.

    One search query returns a handful of hits. A vendor's own story index holds
    hundreds, and each story is prose the vendor published about a named
    company: the independent half of the evidence bar, available in bulk
    without scraping a directory that refuses to be read.
    """
    from .discover import fetch_text
    from .models import InputItem

    plan = sources or load_surfaces()
    # Which published stories name the kind of company this lane is looking for.
    # A vendor's /customers/ index names firms that *buy* the product; its
    # /partners/ paths and award pages name the firms that implement it. For a
    # partner lane those are different populations, and the wrong one produces
    # a list of software vendors' customers.
    if story_paths:
        plan = json.loads(json.dumps(plan))
        for spec in (plan.get("vendor_stories") or {}).get("vendors", {}).values():
            if isinstance(spec, dict) and spec.get("story_paths"):
                spec["story_paths"] = [path for path in spec["story_paths"] if path in story_paths]
        for vendor, spec in list((plan.get("vendor_stories") or {}).get("vendors", {}).items()):
            if isinstance(spec, dict) and not spec.get("story_paths") and not spec.get("hub"):
                del plan["vendor_stories"]["vendors"][vendor]
    urls, skipped = vendor_story_urls(
        plan, max_per_vendor=per_vendor, timeout=timeout, respect_robots=respect_robots,
    )
    budget = max_pages if max_pages is not None else per_vendor * 6
    # Attribute first (pure string work), then fetch the attributed pages at
    # once: sequentially, hundreds of story fetches were the slowest part of a
    # partner run. Only first occurrences are prefetched — a repeat slug is
    # skipped once its first fetch lands, exactly as the sequential walk did —
    # and the assembly below still walks every URL in order, so the budget,
    # the dedupe and the skipped reasons read the same at any worker count.
    attributed: list[tuple[str, str, str]] = []
    first_urls: list[str] = []
    first_occurrence: set[str] = set()
    for url in urls:
        entity, vendor = story_entity(url, sources=plan, partner_half=partner_half)
        if not entity:
            skipped.append({
                "url": url,
                "reason": "the story's address does not name one company, so it cannot be attributed",
            })
            continue
        attributed.append((url, entity, vendor))
        if entity not in first_occurrence:
            first_occurrence.add(entity)
            first_urls.append(url)
    prefetched: dict[str, tuple[bool, Any]] = {}
    for url, ok, outcome in _fetch_pages_many(
        first_urls, fetch_text, timeout=timeout,
        respect_robots=respect_robots, pace=pace,
    ):
        prefetched[url] = (ok, outcome)
    items: list[Any] = []
    seen: set[str] = set()
    resolved_count = 0
    resolved_total = 0
    for url, entity, vendor in attributed:
        if len(items) >= budget:
            skipped.append({
                "source": "vendor_stories",
                "reason": f"stopped after {budget} stories (the run's own page budget)",
            })
            break
        story_slug = entity
        if entity in seen:
            continue
        if url in prefetched:
            # A repeat slug whose first fetch failed has no prefetched result
            # left for it, so it gets its own fetch below instead of none.
            ok, outcome = prefetched.pop(url)
        else:
            try:
                if pace > 0:
                    _pace_host(url, pace)
                outcome = fetch_text(url, timeout=timeout, respect_robots=respect_robots)
                ok = True
            except Exception as exc:
                ok, outcome = False, exc
        if not ok:
            skipped.append({"url": url, "reason": str(outcome)[:200]})
            continue
        record = outcome
        seen.add(entity)
        metadata = dict(getattr(record, "metadata", None) or {})
        # A name cannot be walked, and without walking there is no first-party
        # evidence — which is the kind the bar requires. Resolve the name to a
        # domain where that can be established, and keep it a name where it
        # cannot, so the difference is recorded rather than assumed.
        resolved = ""
        if resolve_limit and resolved_count < resolve_limit:
            resolved_count += 1
            resolved = resolve_entity_domain(entity, timeout=timeout)
        if resolved:
            resolved_total += 1
            skipped.append({
                "source": "vendor_stories", "url": url,
                "reason": f"'{entity}' resolved to {resolved}",
            })
            entity = resolved
        metadata.update({
            "backend": f"stories:{vendor}",
            "enrich_surface": "vendor_stories",
            "attribution": "vendor story slug" if not resolved else "vendor story slug -> resolved domain",
            "story_slug": story_slug,
        })
        items.append(InputItem(
            item_id=entity,
            text=str(getattr(record, "text", "") or ""),
            title=getattr(record, "title", None),
            source_uri=getattr(record, "source_uri", None),
            metadata=metadata,
        ))
    return items, skipped


def surface_urls(
    surface: str,
    domain: str,
    *,
    surfaces: dict[str, Any] | None = None,
) -> list[str]:
    """The candidate URLs for one surface on one domain.

    A template may name ``{slug}`` (the first label of the domain), which is how
    the shared ATS and review probes address a company.
    """
    plan = surfaces or load_surfaces()
    slug = domain.split(".")[0]
    values = {"domain": domain, "slug": slug, "origin": f"https://{domain}"}
    if surface in (plan.get("first_party_paths") or {}):
        return [f"https://{domain}{path}" for path in plan["first_party_paths"][surface] or []]
    templates = (plan.get("templates") or {}).get(surface) or []
    return [str(t).format(**values) for t in templates]


def classify_page(url: str, *, surfaces: dict[str, Any] | None = None) -> str:
    """Which surface a discovered URL belongs to, or "" when none claims it.

    Read off the path, because a page's address is what says what it is. The
    patterns are data: a site that keeps its stories under /our-work is a
    configuration, not a code change.
    """
    plan = surfaces or load_surfaces()
    path = urlparse(url).path.lower()
    for surface, patterns in (plan.get("match") or {}).items():
        if surface.startswith("_") or not isinstance(patterns, list):
            # A note is not a surface. The file documents itself with
            # _-prefixed keys precisely so a loop cannot read prose as data.
            continue
        if any(str(pattern).lower() in path for pattern in patterns):
            return str(surface)
    return ""


def _deepest_first(urls: Iterable[str]) -> list[str]:
    """Prefer specific pages over index pages.

    A sitemap lists both ``/case-studies`` and ``/case-studies/acme-bank``. The
    index page is navigation; the deeper one is the story, and reading stories
    is the point.
    """
    return sorted(dict.fromkeys(urls), key=lambda url: (-urlparse(url).path.count("/"), url))


def discover_pages(
    domain: str,
    *,
    surfaces: dict[str, Any] | None = None,
    timeout: float = 20.0,
    respect_robots: bool = True,
    scan_cap: int = 3000,
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """The pages a site publishes, grouped by the surface they belong to.

    A sitemap answered for every domain probed (8/8) and names the real
    case-study and careers URLs; guessing at paths produced 200s that were the
    wrong kind of page. Returns ({surface: [url, ...]}, skipped).
    """
    from .discover import discover_sitemap_url, fetch_sitemap_entries

    plan = surfaces or load_surfaces()
    found: dict[str, list[str]] = {}
    skipped: list[dict[str, str]] = []
    try:
        sitemap = discover_sitemap_url(f"https://{domain}", timeout=timeout)
    except Exception as exc:
        return {}, [{"surface": "sitemap", "url": f"https://{domain}/sitemap.xml", "reason": str(exc)[:200]}]
    if not sitemap:
        return {}, [{
            "surface": "sitemap",
            "url": f"https://{domain}/robots.txt",
            "reason": "no sitemap found in the common paths or in robots.txt",
        }]
    try:
        entries = fetch_sitemap_entries(sitemap, timeout=timeout, max_urls=scan_cap)
    except Exception as exc:
        return {}, [{"surface": "sitemap", "url": sitemap, "reason": str(exc)[:200]}]
    declares_home = "home" in (plan.get("first_party_paths") or {})
    for url, _lastmod in entries:
        path = urlparse(url).path
        if declares_home and path in ("", "/"):
            # The domain root is the landing page, and a path-pattern classifier
            # cannot see that: "/" matches no pattern by design. A lane that
            # lists `home` gets it; one that does not is unaffected.
            found.setdefault("home", []).append(url)
            continue
        surface = classify_page(url, surfaces=plan)
        if surface:
            found.setdefault(surface, []).append(url)
    return {surface: _deepest_first(urls) for surface, urls in found.items()}, skipped


def _channel_records(
    channel: str,
    domain: str,
    *,
    surfaces: dict[str, Any],
    timeout: float,
    respect_robots: bool,
    max_per_channel: int,
    pace: float = 0.0,
) -> tuple[list[Any], list[dict[str, str]]]:
    """Records from a surface the engine already knows how to fetch.

    Named channels rather than re-implemented ones: discover.py owns how to talk
    to a hiring board and to a code host, and this decides when to ask. A board
    that does not exist raises, and the reason is kept — a company without an
    ATS account is a fact about that company, not a failed fetch.
    """
    from . import discover

    slug = domain.split(".")[0]
    records: list[Any] = []
    skipped: list[dict[str, str]] = []

    if channel == "ats":
        for name, fetcher in (
            ("greenhouse", discover.fetch_greenhouse_board),
            ("ashby", discover.fetch_ashby_org),
            ("lever", discover.fetch_lever_org),
        ):
            if name not in (surfaces.get("channels") or {}).get("ats", []):
                continue
            try:
                records.extend(fetcher(slug, max_jobs=max_per_channel, timeout=timeout))
            except Exception as exc:
                skipped.append({"surface": "ats", "board": name, "url": f"{name}:{slug}", "reason": str(exc)[:200]})
        return records, skipped

    if channel == "code":
        if "github" not in (surfaces.get("channels") or {}).get("code", []):
            return [], []
        try:
            org_records, org_skipped = discover.fetch_github_org(
                slug, max_repos=max_per_channel, timeout=timeout,
            )
            records.extend(org_records)
            skipped.extend(org_skipped[:5])
        except Exception as exc:
            skipped.append({"surface": "code", "url": f"github:{slug}", "reason": str(exc)[:200]})
        return records, skipped

    if channel == "community":
        backends = list((surfaces.get("community") or {}).get("backends") or [])
        # The backends share nothing: different hosts, no dependency, and a
        # mention on Hacker News does not inform the search for one on Reddit.
        # Looping them one at a time made this the single largest cost in a walk
        # — 38 of 62 measured seconds — for a two-record yield.
        #
        # Pacing still applies, per source, because the limit that is real is
        # across entities: a hundred companies each searching the same six
        # backends must queue on that backend, not fire at once.
        hits: list[Any] = []
        results: dict[str, list[Any]] = {}

        def search_one(backend: str) -> None:
            try:
                discover._source_ready(backend, pace)
                results[backend] = list(discover.web_search(
                    f'"{domain}"', backends=[backend], max_results=max_per_channel, timeout=timeout,
                ))
            except Exception as exc:
                skipped.append({"surface": "community", "backend": backend, "reason": str(exc)[:200]})

        if backends:
            with ThreadPoolExecutor(max_workers=min(6, len(backends))) as pool:
                list(pool.map(search_one, backends))
        for backend in backends:
            hits.extend(results.get(backend, []))
        for hit in hits[: max_per_channel * 2]:
            try:
                site = discover.se_site_for_url(hit.url)
                if site:
                    # Stack Exchange serves 403 to every plain HTML fetch; its
                    # API serves the same question. This is the difference
                    # between a channel that reports refusals and one that
                    # reads the answers.
                    records.append(discover.fetch_stackexchange_question(
                        hit.url, timeout=timeout,
                    ))
                else:
                    # HN items and Reddit posts resolve through their own APIs.
                    records.append(discover.fetch_smart_url(
                        hit.url, timeout=timeout, respect_robots=respect_robots,
                    ))
            except Exception as exc:
                skipped.append({"surface": "community", "url": hit.url, "reason": str(exc)[:200]})
        return records, skipped

    return [], [{"surface": channel, "reason": f"no channel wired for '{channel}'"}]


def enrich_entity(
    entity: str,
    *,
    kinds: Sequence[str] = (),
    surfaces: dict[str, Any] | None = None,
    bar_kinds: Sequence[str] = (),
    max_pages: int = 8,
    per_surface: int = 3,
    timeout: float = 20.0,
    delay: float = 1.0,
    respect_robots: bool = True,
    vendor_stories: bool = True,
    vendor_story_limit: int = 20,
    story_urls: Sequence[str] | None = None,
    pace: float = 0.0,
    surface_order: Sequence[str] = (),
) -> tuple[list[Any], EnrichReport]:
    """Walk the surfaces that carry the kinds this entity is missing.

    Returns the records collected and a report of what was visited and what was
    refused. Every refusal is recorded with its reason: a surface that answered
    404 is a fact about the run, and a silent zero is not allowed.
    """
    from . import contracts
    from .discover import crawl_site, fetch_text

    report = EnrichReport(entity=str(entity or ""), kinds=[str(k) for k in kinds])
    domain = entity_domain(entity)
    report.domain = domain
    if not domain:
        report.skipped.append({
            "surface": "all",
            "reason": (
                f"'{entity}' does not name a domain, so there is no website to walk. "
                "Attribution gave this entity a slug rather than a host."
            ),
        })
        return [], report

    wanted_kinds = [str(k) for k in (kinds or bar_kinds)]
    # A rung is the authority on what this call reads. The rung's surfaces are
    # read in the lane's own order, landing first; the walk itself stays
    # lane-agnostic and only reads the names.
    #
    # This used to *union* the rung with the surfaces the missing kinds map to,
    # which made the ladder decorative: the partner lane names neither community
    # nor code nor vendor_stories, and yet every entity short of
    # independent_validation searched six community backends and read a vendor's
    # story index — 38 of 51 measured seconds on a single entity. A lane that
    # wants those surfaces asks for them in a rung.
    declared = list(dict.fromkeys(str(surface) for surface in surface_order))
    if declared:
        wanted = declared
        narrowed = [
            surface for surface in contracts.surfaces_for_kinds(wanted_kinds)
            if surface not in wanted
        ]
    else:
        # No rung was given — a lane with no ladder — so the kinds decide, as
        # they did before ladders existed.
        wanted = list(contracts.surfaces_for_kinds(wanted_kinds))
        narrowed = []
    report.surfaces = list(wanted)
    for surface in narrowed:
        report.skipped.append({
            "surface": surface,
            "reason": (
                "the lane's ladder does not name this surface, so it was not read "
                f"(it carries evidence this entity is missing: {', '.join(wanted_kinds)})"
            ),
        })
    if not wanted:
        report.skipped.append({
            "surface": "all",
            "reason": "no surface is declared for the evidence this entity is missing",
        })
        return [], report

    record_budget = max(1, int(per_surface))
    plan = surfaces or load_surfaces()
    paths = plan.get("first_party_paths") or {}
    templates = plan.get("templates") or {}
    channels = plan.get("channels") or {}
    # Channel surfaces are declared in two places — the named-fetcher map and
    # the community block — so the dispatch reads both. Missing one silently
    # disabled a whole channel.
    channel_names = {name for name in channels if not name.startswith("_")}
    if plan.get("community"):
        channel_names.add("community")
    first_party = [s for s in wanted if s in paths]
    probe_surfaces = [s for s in wanted if s in templates]
    channel_surfaces = [s for s in wanted if s in channel_names]
    records: list[Any] = []
    seen: set[str] = set()
    #: Records reached by addressing the entity's own account (a hiring board
    #: under its slug) rather than by finding them. They are about the entity by
    #: construction, even though a board URL does not spell the domain out.
    addressed: list[Any] = []

    def add(record: Any, surface: str, *, by_address: bool = False) -> None:
        # Bounded on purpose. A dossier is one input item to one model request,
        # and a channel can hand back a dozen records at once; the run that
        # bundled 46 sources died in scoring with a context overflow. What is
        # left out is counted and reported rather than silently dropped.
        if report.by_surface.get(surface, 0) >= record_budget:
            report.trimmed[surface] = report.trimmed.get(surface, 0) + 1
            return
        key = str(getattr(record, "source_uri", "") or getattr(record, "item_id", "") or "")
        if key and key in seen:
            return
        if key:
            seen.add(key)
        # Tag the record with the surface that produced it, so the lane report
        # can say which surface a claim came from instead of folding every
        # enrichment into one anonymous "enriched" bucket.
        metadata = getattr(record, "metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("enrich_surface", surface)
        records.append(record)
        if by_address:
            if isinstance(metadata, dict):
                metadata.setdefault("attribution", "addressed by slug")
            addressed.append(record)
        report.by_surface[surface] = report.by_surface.get(surface, 0) + 1

    def fetch_into(url: str, surface: str) -> bool:
        report.visited += 1
        if pace > 0:
            _pace_host(url, pace)
        try:
            add(fetch_text(url, timeout=timeout, respect_robots=respect_robots), surface)
            return True
        except Exception as exc:
            report.skipped.append({"surface": surface, "url": url, "reason": str(exc)[:200]})
            return False

    # First party: the sitemap names the pages, so fetch those instead of
    # guessing at paths. A site with no sitemap falls back to the paths.
    if first_party:
        pages, sitemap_skipped = discover_pages(
            domain, surfaces=plan, timeout=timeout, respect_robots=respect_robots,
        )
        report.skipped.extend(sitemap_skipped)
        report.visited += 1  # the sitemap lookup itself
        if pages:
            # A sitemap answered for every domain probed, so when one is found
            # it is the authority: a page absent from it is a page that is not
            # there, and probing the guess list anyway only buys 404s.
            for surface in first_party:
                for url in pages.get(surface, [])[: max(1, per_surface)]:
                    fetch_into(url, surface)
        else:
            # No sitemap. Crawl the site's own links first — a site that names
            # its pages is better evidence than a guess list — and fall back to
            # the paths for whatever the crawl did not reach.
            covered: set[str] = set()
            try:
                walked, crawl_skipped = crawl_site(
                    f"https://{domain}", max_pages=max_pages, max_depth=2,
                    timeout=timeout, delay=delay, respect_robots=respect_robots,
                )
                report.visited += len(walked)
                report.skipped.extend(
                    {"surface": "first_party", "url": str(s.get("url", "")), "reason": str(s.get("reason", ""))[:200]}
                    for s in crawl_skipped[:5]
                )
                for record in walked:
                    surface = classify_page(str(getattr(record, "source_uri", "") or ""), surfaces=plan)
                    if surface in first_party:
                        add(record, surface)
                        covered.add(surface)
            except Exception as exc:
                report.skipped.append({
                    "surface": "first_party", "url": f"https://{domain}", "reason": str(exc)[:200],
                })
            for surface in first_party:
                if surface in covered:
                    continue
                for url in surface_urls(surface, domain, surfaces=plan):
                    fetch_into(url, surface)

    for surface in probe_surfaces:
        for url in surface_urls(surface, domain, surfaces=plan):
            fetch_into(url, surface)

    for surface in channel_surfaces:
        channel_records, channel_skipped = _channel_records(
            surface, domain, surfaces=plan, timeout=timeout,
            respect_robots=respect_robots, max_per_channel=max(1, per_surface), pace=pace,
        )
        report.visited += len(channel_records)
        for record in channel_records:
            add(record, surface, by_address=True)
        report.skipped.extend(channel_skipped)

    if vendor_stories and "vendor_stories" in wanted:
        try:
            story_records, story_skipped = fetch_vendor_stories(
                domain, sources=plan, max_per_vendor=vendor_story_limit,
                timeout=timeout, respect_robots=respect_robots,
                urls=story_urls, pace=pace,
            )
            report.visited += len(story_records)
            for record in story_records:
                add(record, "vendor_stories")
            report.skipped.extend(story_skipped[:5])
        except Exception as exc:
            report.skipped.append({"surface": "vendor_stories", "reason": str(exc)[:200]})

    # A surface that was looked at and yielded nothing says so. "We searched
    # the vendor stories and none names this entity" is a different fact from
    # "we never looked", and a zero with no reason is the one thing this fleet
    # does not report.
    for surface, left_out in sorted(report.trimmed.items()):
        report.skipped.append({
            "surface": surface,
            "reason": (
                f"kept the first {report.by_surface.get(surface, 0)} record(s) from this surface; "
                f"{left_out} more were not bundled (dossier size is bounded)"
            ),
        })
    spoken_for = {str(entry.get("surface") or "") for entry in report.skipped}
    attempted = set(first_party) | set(probe_surfaces) | set(channel_surfaces)
    if vendor_stories and "vendor_stories" in wanted:
        attempted.add("vendor_stories")
    for surface in sorted(attempted):
        if report.by_surface.get(surface) or surface in spoken_for:
            continue
        report.skipped.append({
            "surface": surface,
            "reason": (
                "looked, and no vendor story naming this entity was found"
                if surface == "vendor_stories"
                else "visited, and nothing on this surface was about the entity"
            ),
        })

    # A first-party page carries its own host, so attribution passes on the URL;
    # anything that names nobody is dropped rather than filed under this entity.
    # Records reached by addressing the entity's own account are exempt: the
    # board's slug is the address we asked for, not a name we have to find.
    by_address = {id(record) for record in addressed}
    attributed, _dropped = filter_attributed(
        [record for record in records if id(record) not in by_address], domain
    )
    report.kept = len(attributed) + len(addressed)
    return attributed + addressed, report


def evidence_gaps(text: str, uri: str, bar: Sequence[str]) -> list[str]:
    """Which of the bar's evidence kinds this entity's evidence does not carry.

    One reader for the question, so the walk and the report of what the walk was
    missing cannot disagree: ``evidence.coverage`` is what says whether a text
    evidences a kind, and an entity short of three kinds is walked for those
    three rather than for everything.
    """
    from .evidence import coverage

    if not bar:
        return []
    kinds = coverage(text or "", uri or "")
    return [kind for kind in bar if not kinds.get(kind)]


def walk_entities(
    entities: Sequence[Any],
    lane: Any = None,
    *,
    per_surface: int = 3,
    max_pages: int = 8,
    max_entities: int = 10_000,
    timeout: float = 20.0,
    delay: float = 1.0,
    respect_robots: bool = True,
    vendor_stories: bool = True,
    surface_order: Sequence[str] | None = None,
    workers: int = 8,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Go to each entity's own surfaces for the evidence its bar is missing.

    Discovery finds pages *about* an entity. The bar asks for evidence a page
    about somebody rarely carries — the stack they actually deliver, the people
    they are hiring, what they charge — because that lives on their own site,
    their hiring board and the stories their vendors publish. This is the stage
    that stops searching and goes to look, and it is the same walk for every
    lane: only the missing kinds and the surfaces differ.

    ``surface_order`` is the ladder's own order when a caller has one: a rung
    names the pages it may read, and passing that through means the walk and the
    funnel agree about what a candidate is entitled to. Entities are independent
    of each other, so they walk concurrently; the shared hosts are paced per host
    inside the walk, so breadth does not cost politeness.

    Returns the extra items to bundle, and a report of what each walk did.
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import contracts
    from .models import InputItem

    bar: tuple[str, ...] = ()
    if lane is not None:
        bar = tuple(lane.require_kinds) or tuple(contracts.TIER_MINIMUMS.get(lane.tier or "", ()))

    # Enumerate the vendor story indexes once for the whole run. Doing it inside
    # each entity's walk cost a dozen requests per entity and made this the
    # slowest stage of a run.
    shared_story_urls: list[str] = []
    if vendor_stories:
        try:
            shared_story_urls, _skipped = vendor_story_urls(
                max_per_vendor=40, timeout=timeout, respect_robots=respect_robots,
            )
        except Exception:
            shared_story_urls = []

    if surface_order is None:
        surface_order = (
            list(dict.fromkeys(
                surface for rung in lane.funnel.rungs() for surface in rung.surfaces
            ))
            if lane is not None else []
        )

    extra: list[Any] = []
    report: list[dict[str, Any]] = []
    targets: list[Any] = []
    for dossier in list(entities)[: max(1, max_entities)]:
        entity = str(getattr(dossier, "item_id", "") or "")
        missing = evidence_gaps(
            getattr(dossier, "text", "") or "", getattr(dossier, "source_uri", "") or "", bar
        )
        if not missing:
            continue
        targets.append((entity, missing))

    def walk_one(target: tuple[str, list[str]]) -> tuple[str, list[str], list[Any], Any]:
        entity, missing = target
        records, walk = enrich_entity(
            entity,
            kinds=missing,
            per_surface=per_surface,
            max_pages=max_pages,
            timeout=timeout,
            delay=delay,
            respect_robots=respect_robots,
            vendor_stories=vendor_stories,
            story_urls=shared_story_urls,
            pace=max(0.0, delay),
            surface_order=list(surface_order),
        )
        return entity, missing, records, walk

    workers = max(1, min(workers, len(targets))) if targets else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        walked = list(pool.map(walk_one, targets))

    for entity, missing, records, walk in walked:
        summary = walk.as_dict()
        summary["missing"] = missing
        report.append(summary)
        for record in records:
            metadata = dict(getattr(record, "metadata", None) or {})
            # The lane report attributes yield by surface, so carry the surface
            # the walk tagged rather than one flat "enrichment" bucket.
            metadata.setdefault("backend", str(metadata.get("enrich_surface") or "enrich"))
            extra.append(InputItem(
                item_id=entity,
                text=str(getattr(record, "text", "") or ""),
                title=getattr(record, "title", None),
                source_uri=getattr(record, "source_uri", None),
                metadata=metadata,
            ))
    return extra, report

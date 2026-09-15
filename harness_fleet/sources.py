"""Which source a URL is, in the vocabulary every fleet tool shares.

The taxonomy lives here rather than inside the partner bundler because every
product needs it: a job post, a vendor story and a review are different kinds of
evidence regardless of which fleet is reading them, and a coverage readout is
only meaningful when the labels mean the same thing everywhere.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

REGISTRY_DOMAINS = (
    # Vendor-published partner and customer stories: independent prose about the
    # work, and the non-first-party evidence the tier-1 gate asks for.
    "aws.amazon.com",
    "partners.amazonaws.com",
    "snowflake.com",
    "appsource.microsoft.com",
    "cloud.google.com",
    "datadoghq.com",
    "ecosystem.hubspot.com",
    "salesforce.com",
    "databricks.com",
    "elastic.co",
    "mongodb.com",
    "confluent.io",
)

REVIEW_DOMAINS = (
    "clutch.co",
    "g2.com",
    "goodfirms.co",
    "themanifest.com",
    "upcity.com",
)

COMMUNITY_DOMAINS = (
    "github.com",
    "youtube.com",
    "substack.com",
    "medium.com",
    "dev.to",
    # The community backends the fleet searches. Without these, an HN thread or
    # a Reddit post was filed as `general_web` and — in the sourcing runner —
    # could be mistaken for a partner domain.
    "news.ycombinator.com",
    "reddit.com",
    "redd.it",
    "stackoverflow.com",
    "stackexchange.com",
    "serverfault.com",
    "superuser.com",
    "lobste.rs",
    "lemmy.world",
    "lemmy.ml",
    "programming.dev",
)

ATS_DOMAINS = (
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
    "jobs.",
)

# Professional/social networks and hosting or publishing platforms. A page on
# one of these hosts is *about* a firm — a profile, a post, a personal site on
# a hosting domain — and is never the firm itself: the sourcing runner must not
# admit these hosts as candidate partner entities.
PLATFORM_HOSTS = (
    "linkedin.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "vercel.app",
    "netlify.app",
    "github.io",
    "gitlab.io",
    "pages.dev",
    "webflow.io",
    "notion.site",
    "wixsite.com",
    "squarespace.com",
    "wordpress.com",
    "blogspot.com",
    "carrd.co",
    "readthedocs.io",
)

# Hosts that *talk about* entities rather than being entities: directories,
# registries, community platforms and job boards. A page on one of these is
# evidence about an entity; the host itself is never the entity, so it must not
# become an entity id and must not be counted as a domain a page names.
SOURCE_HOSTS = (
    REGISTRY_DOMAINS + REVIEW_DOMAINS + COMMUNITY_DOMAINS + ATS_DOMAINS + PLATFORM_HOSTS
)
LINKED_DOMAIN_RE = re.compile(
    r'https?://([a-z0-9][a-z0-9.\-]{2,80}?)(?=[/\s"\'<>)\]]|$)', re.IGNORECASE
)
# Only these suffixes are treated as a firm's own site when linked from a
# community or directory page; a link to a blog post is not an entity.
ENTITY_SUFFIXES = (
    ".com", ".io", ".ai", ".dev", ".net", ".co", ".org", ".cloud", ".tech",
    ".sh", ".app", ".us", ".uk", ".de", ".ca", ".au", ".nl", ".se", ".fr",
)

CASE_STUDY_PATH_RE = re.compile(r"/(case-stud|work|customers?|success-stor|clients|portfolio)", re.IGNORECASE)
PRACTICE_PATH_RE = re.compile(r"/(services?|solutions?|practices?|about|partners?|consulting)", re.IGNORECASE)


def canonicalize_entity_id(identifier_or_url: str) -> str:
    """Extract clean, canonical domain/entity id from a URL or raw identifier."""
    raw = (identifier_or_url or "").strip().lower()
    if not raw:
        return "unknown_entity"

    # If it contains a URL scheme or looks like a URL
    if "://" in raw or "/" in raw:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.netloc.lower().strip()
        path = parsed.path.strip("/")

        # Strip common prefixes
        if host.startswith("www."):
            host = host[4:]

        # Handle ATS URLs where company slug is the first path segment
        if any(ats in host for ats in ("ashbyhq.com", "greenhouse.io", "lever.co", "workable.com")):
            parts = [p for p in path.split("/") if p]
            if parts:
                slug = parts[0].replace("-", "_")
                # Append .com if slug looks like a domain name
                return f"{slug}.com" if "." not in slug else slug

        # Handle vendor registry paths: e.g. partners.amazonaws.com/partners/trace3
        if "partners.amazonaws.com" in host or "snowflake.com" in host:
            parts = [p for p in path.split("/") if p and p not in ("partners", "en-us", "marketplace")]
            if parts:
                slug = parts[-1].replace("-", "_")
                return f"{slug}.com" if "." not in slug else slug

        # Handle Clutch/G2 profiles: clutch.co/profile/trace3
        if any(rev in host for rev in ("clutch.co", "g2.com")):
            parts = [p for p in path.split("/") if p and p not in ("profile", "it-services", "products")]
            if parts:
                slug = parts[-1].replace("-", "_")
                return f"{slug}.com" if "." not in slug else slug

        if host:
            return host

    # Plain text identifier
    cleaned = re.sub(r"[^a-z0-9_.-]+", "_", raw).strip("_.")
    return cleaned or "unknown_entity"


def classify_source_category(source_uri: str, entity_id: str = "") -> str:
    """Classify a source URL into one of the canonical evidence categories.

    The seed lists below are the starting point. A domain the registry has
    promoted — because it was seen often enough and someone confirmed it — is
    classified from the registry, so the taxonomy grows without a code change
    and without guessing: an unpromoted domain falls through to the seed rules
    and, at worst, lands in ``general_web``, which is a lead rather than proof.
    """
    uri = (source_uri or "").strip().lower()
    if not uri:
        return "general_web"
    try:
        from .registry import lookup

        promoted = lookup(host_of(uri))
    except Exception:
        promoted = None
    if promoted:
        return promoted

    # 1. Vendor registries and vendor-published stories. Anything a vendor
    # publishes about a partner or customer is third-party evidence for that
    # partner, so a story path counts even when the word "partner" is absent
    # (a /customers/ story labelled first-party would wear the partner's own
    # marketing label and could not satisfy the independent-evidence gate).
    if any(d in uri for d in REGISTRY_DOMAINS) and (
        "partner" in uri or "/customers" in uri or "/success" in uri
        or CASE_STUDY_PATH_RE.search(uri)
    ):
        return "vendor_registry"

    # 2. Review and audit platforms
    if any(d in uri for d in REVIEW_DOMAINS):
        return "b2b_directory_audit"

    # 3. Community and social platforms
    if any(d in uri for d in COMMUNITY_DOMAINS):
        return "community_and_social"

    # 4. ATS / Hiring requisitions
    if any(ats in uri for ats in ATS_DOMAINS):
        return "ats_requisitions"

    # 5. First-party case studies
    if CASE_STUDY_PATH_RE.search(uri):
        return "first_party_case_study"

    # 6. First-party practices and services
    if PRACTICE_PATH_RE.search(uri):
        return "first_party_practice"

    # Fallback to first-party if host matches entity domain
    if entity_id and entity_id in uri:
        return "first_party_practice"

    return "general_web"
# Hosts that are never evidence for this product: academic publishers and
# paper aggregators, which a broad web query drags in ("Kafka migration" hits
# journals) and which say nothing about a company's delivery. They are dropped
# before any fetch, so a run neither waits on them nor reports their 403s as
# its own failure. An explicit `fetch --url` still fetches whatever you name.
NOISE_DOMAINS = (
    "oup.com", "sciencedirect.com", "springer.com", "link.springer.com", "wiley.com",
    "tandfonline.com", "jstor.org", "sagepub.com", "cambridge.org", "ieee.org",
    "acm.org", "mdpi.com", "frontiersin.org", "plos.org", "nature.com", "science.org",
    "ssrn.com", "researchgate.net", "academia.edu", "semanticscholar.org", "arxiv.org",
    "biorxiv.org", "medrxiv.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "doaj.org", "scilit.net", "paperity.org", "core.ac.uk", "openreview.net",
    "sci-hub.se", "encyclopedia.pub", "europepmc.org",
)


def is_noise_host(host: str) -> bool:
    """True when a host cannot carry evidence about a company's work."""
    return any(marker in (host or "").lower() for marker in NOISE_DOMAINS)

def host_of(url: str | None) -> str:
    """The host of a URL or bare domain, lowercased and without ``www.``."""
    raw = (url or "").strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc.lower().strip()
    return host[4:] if host.startswith("www.") else host


def is_source_host(host: str) -> bool:
    """True when a host publishes *about* entities rather than being one."""
    return any(marker in host for marker in SOURCE_HOSTS)


def linked_domains(text: str | None) -> list[str]:
    """Firm domains linked from a page, in order, source hosts excluded.

    This is how a page that is *about* an entity gets attributed: a vendor case
    study or a forum thread names the firms it discusses, and those names are
    the only attributions allowed, because a page never gets to invent one.
    """
    out: list[str] = []
    for match in LINKED_DOMAIN_RE.finditer(text or ""):
        domain = match.group(1).lower().strip(".")
        if domain.startswith("www."):
            domain = domain[4:]
        if "." not in domain or is_source_host(domain):
            continue
        if not domain.endswith(ENTITY_SUFFIXES):
            continue
        out.append(domain)
    return list(dict.fromkeys(out))


def entity_key_for(source_uri: str, text: str = "", metadata: dict[str, Any] | None = None) -> str:
    """The entity a source belongs to, preferring real attribution over the host.

    An explicit entity or website in the metadata wins. Otherwise the URL
    decides — *unless* the URL only identifies a source host, which happens for
    a vendor story or a platform page: those are about somebody else, so the
    entity is a domain the text actually names. When the text names nobody the
    page keeps its own host rather than inventing an attribution.
    """
    meta = metadata or {}
    explicit = str(meta.get("entity") or meta.get("website") or "").strip()
    if explicit:
        return explicit
    key = canonicalize_entity_id(source_uri or "")
    if key and is_source_host(key):
        named = [domain for domain in linked_domains(text) if not is_source_host(domain)]
        if named:
            return named[0]
    return key or "unknown_entity"

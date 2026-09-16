"""The central contracts: one declaration per thing, read by every altitude.

Discovery, bundling, scoring and presentation all need to agree about the same
things — what a source category is worth, what each evidence kind proves, what
a claim requires, how confidence is priced, and what a tier may claim. When
each layer carries its own copy of those rules, every new surface finds a new
loose end; here they are declared once and imported.

Products stay specific only at the edges: their query set, their preset name,
their checklist item ids (which map onto the concepts below by name), and any
weight overrides they deliberately want.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# --- The scoring signals, declared once ------------------------------------
# These are the canonical evidence signals. Every layer that needs to know
# whether a text states an outcome, a delivery, growth, certification or
# commercial scale imports them from here; none re-declares its own.

OUTCOME_RE = re.compile(
    r"(\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?x\b|\$\s?\d[\d,.]*|\b\d[\d,]{2,}\s+(?:customers|clients|users|employees|stores|sites)\b)",
    re.IGNORECASE,
)
DELIVERY_RE = re.compile(
    r"\b(implement(?:ed|s|ing)?|migrat(?:ed|es|ing|ion)|integrat(?:ed|es|ing|ion)|deliver(?:ed|s|ing|y)?|"
    r"deploy(?:ed|s|ing|ment)?|built|modernis(?:ed|ing)|moderniz(?:ed|ing)|roll(?:ed)?\s+out)\b",
    re.IGNORECASE,
)
GROWTH_RE = re.compile(
    r"\b(funding|raised|series [A-E]|acquisition|acquired|merger|new office|expanded|award|"
    r"partner of the year|investment)\b",
    re.IGNORECASE,
)
CERT_RE = re.compile(
    r"\b(certified|premier partner|select[- ]tier|partner tier|advanced partner|elite partner|"
    r"competency|marketplace listing)\b",
    re.IGNORECASE,
)
COMMERCIAL_RE = re.compile(
    r"(\$\s?\d[\d,.]*\s?(?:k|K)?\s*(?:minimum|min\b)|\bminimum (?:project|engagement)\b|"
    r"\$\s?\d[\d,.]*\s*(?:/|per\s)?\s*(?:hour|hr|day)\b|\b\d[\d,]*\s+employees\b|"
    r"\bheadcount\b|\bblended rate\b)",
    re.IGNORECASE,
)


def claim_concept(item_id: str) -> str:
    """The central concept behind a product's checklist item id."""
    cleaned = re.sub(r"^[a-z]{0,3}\d*_", "", (item_id or "").strip().lower())
    if cleaned in CLAIM_CONCEPTS:
        return cleaned
    for concept in CLAIM_CONCEPTS:
        if cleaned.endswith(concept):
            return concept
    return ""


def quote_addresses_claim(
    item_id: str, quote_text: str, category: str = "", terms: Any = ()
) -> tuple[bool, str]:
    """Whether a quote may carry this claim, and why not when it may not."""
    concept = claim_concept(item_id)
    if not concept:
        return True, ""
    _kind, requirement, needs_third_party, test = CLAIM_CONCEPTS[concept]
    category = (category or "").upper()
    if needs_third_party and category and category not in INDEPENDENT_CATEGORIES:
        return False, f"quote must come from a third party, not {category.lower().replace('_', ' ')}"
    vocabulary = [str(term) for term in (terms or ()) if str(term).strip()]
    if test(quote_text or "", category, vocabulary):
        return True, ""
    return False, f"quote does not state {requirement}"


def confidence_for(strength: float, *, category: str = "", sources: int = 1) -> str:
    """An insight is reported when a qualifying source carries it, else a lead."""
    return "reported" if strength > 0 else "unsupported"


def validate_contracts() -> list[str]:
    """Self-check: every cross-reference in these contracts resolves."""
    problems: list[str] = []
    for concept, (kind, _requirement, _third_party, _test) in CLAIM_CONCEPTS.items():
        if kind not in EVIDENCE_KINDS:
            problems.append(f"claim concept '{concept}' names unknown evidence kind '{kind}'")
    for tier, kinds in TIER_MINIMUMS.items():
        for kind in kinds:
            if kind not in EVIDENCE_KINDS:
                problems.append(f"tier '{tier}' requires unknown evidence kind '{kind}'")
    for category in (*FIRST_PARTY_CATEGORIES, *INDEPENDENT_CATEGORIES, *CORROBORATING_CATEGORIES):
        if category not in SOURCE_CATEGORIES:
            problems.append(f"category '{category}' is referenced but not declared")
    for level, weight in CONFIDENCE_LEVELS.items():
        if weight < 0:
            problems.append(f"confidence level '{level}' has a negative weight")
    for kind, categories in QUALIFYING_CATEGORIES.items():
        for category in categories:
            if category not in SOURCE_CATEGORIES:
                problems.append(f"evidence bar for '{kind or 'any'}' allows undeclared category '{category}'")
    for kind in KIND_SURFACES:
        if kind not in EVIDENCE_KINDS:
            problems.append(f"surface mapping names unknown evidence kind '{kind}'")
    return problems


# ---------------------------------------------------------------------------
# 1. Source categories: what kind of page this is, and what it can vouch for.
# ---------------------------------------------------------------------------
#: category -> (what it is, may it count as third-party corroboration?)
SOURCE_CATEGORIES: dict[str, tuple[str, bool]] = {
    "VENDOR_REGISTRY": ("a vendor's own story about a partner or customer", True),
    "B2B_DIRECTORY_AUDIT": ("a directory, review or audit platform profile", True),
    "COMMUNITY_AND_SOCIAL": ("a forum, community or social post", True),
    "ATS_REQUISITIONS": ("an open requisition on an applicant-tracking board", False),
    "FIRST_PARTY_CASE_STUDY": ("the entity's own case study", False),
    "FIRST_PARTY_PRACTICE": ("the entity's own services or practice page", False),
    "GENERAL_WEB": ("a page with no evidence role", False),
    "PREAMBLE": ("text before any labelled section", False),
}
FIRST_PARTY_CATEGORIES = ("FIRST_PARTY_CASE_STUDY", "FIRST_PARTY_PRACTICE")
INDEPENDENT_CATEGORIES = tuple(
    name for name, (_what, independent) in SOURCE_CATEGORIES.items()
    if independent and name not in ("ATS_REQUISITIONS",)
)
CORROBORATING_CATEGORIES = INDEPENDENT_CATEGORIES + ("ATS_REQUISITIONS",)

# ---------------------------------------------------------------------------
# 2. Evidence kinds: what each one proves, and where it may come from.
# ---------------------------------------------------------------------------
EVIDENCE_KINDS: dict[str, str] = {
    "delivery_proof": "work was delivered, with an outcome attached",
    "named_clients": "clients are named",
    "stack_delivery": "a named technology was delivered",
    "independent_validation": "a third party vouches for the work",
    "delivery_hiring": "the entity is hiring for delivery work",
    "commercial_terms": "commercial terms are stated",
    "certification": "a certification or partner tier is claimed",
    "engineering_output": "the entity publishes engineering work",
    "growth_signal": "funding, acquisition or award activity",
    "dated_events": "dated events are present",
}

# ---------------------------------------------------------------------------
# 3. Claim concepts: what a claim requires before a quote may carry it.
# ---------------------------------------------------------------------------
#: concept -> (evidence kind it feeds, what a supporting quote must state,
#:             does it require a third-party source?, the mechanical test)
CLAIM_CONCEPTS: dict[str, tuple[str, str, bool, Callable[[str, str, list[str]], bool]]] = {
    "stack_delivery": (
        "stack_delivery", "one of this task's technologies", False,
        lambda text, category, terms: any(
            re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", text or "", re.IGNORECASE) for term in terms
        ),
    ),
    "billable_delivery": (
        "delivery_proof", "delivery with an outcome", False,
        lambda text, category, terms: bool(OUTCOME_RE.search(text or "") and DELIVERY_RE.search(text or "")),
    ),
    "delivery_proof": (
        "delivery_proof", "delivery with an outcome", False,
        lambda text, category, terms: bool(OUTCOME_RE.search(text or "") and DELIVERY_RE.search(text or "")),
    ),
    "client_outcome": (
        "delivery_proof", "a delivered outcome with a result", False,
        lambda text, category, terms: bool(OUTCOME_RE.search(text or "")),
    ),
    "delivery_hiring": (
        "delivery_hiring", "an open requisition", False,
        lambda text, category, terms: category == "ATS_REQUISITIONS" or bool(
            re.search(r"\b(hiring|we.re looking for|open role|join our team|apply now)\b", text or "", re.I)
        ),
    ),
    "independent_validation": (
        "independent_validation", "an independent source that speaks to delivery or outcomes", True,
        lambda text, category, terms: bool(DELIVERY_RE.search(text or "") or OUTCOME_RE.search(text or "")),
    ),
    "growth_signal": (
        "growth_signal", "a funding, acquisition or award event", False,
        lambda text, category, terms: bool(GROWTH_RE.search(text or "")),
    ),
    "certification": (
        "certification", "a certification or partner tier", False,
        lambda text, category, terms: bool(CERT_RE.search(text or "")),
    ),
    "commercial_scale": (
        "commercial_terms", "commercial scale (headcount, rates or engagement size)", False,
        lambda text, category, terms: bool(COMMERCIAL_RE.search(text or "")),
    ),
    "vendor_alliance": (
        "certification", "a vendor relationship, tier or marketplace listing", False,
        lambda text, category, terms: bool(CERT_RE.search(text or "") or re.search(r"\bpartner(ship)?\b", text or "", re.I)),
    ),
    "published_engineering": (
        "engineering_output", "published engineering work", False,
        lambda text, category, terms: bool(re.search(r"\b(open source|github|architecture|we built|engineering blog)\b", text or "", re.I)),
    ),
}

# ---------------------------------------------------------------------------
# 5. Tiers: what each band claims, and the evidence it must be able to point at.
# ---------------------------------------------------------------------------
TIER_MINIMUMS: dict[str, tuple[str, ...]] = {
    "tier_1": ("delivery_proof", "independent_validation", "stack_delivery"),
    "tier_2": ("delivery_proof", "stack_delivery"),
    "tier_3": ("stack_delivery",),
}
TIER_ORDER = ("tier_1", "tier_2", "tier_3")

# ---------------------------------------------------------------------------
# 6. Review: what to go and find when a claim is not yet well supported.
# ---------------------------------------------------------------------------
#: evidence kind -> the query that tends to surface a source carrying it. A
#: review round searches for exactly the kinds an entity is missing, instead of
#: re-running the original query and hoping.
KIND_QUERIES: dict[str, str] = {
    "delivery_proof": '"{entity}" ("case study" OR "implemented" OR "migrated")',
    "independent_validation": '"{entity}" (partner OR customer OR "case study") -site:{entity}',
    "stack_delivery": '"{entity}" (engineering OR architecture OR platform)',
    "delivery_hiring": '"{entity}" (hiring OR "open role" OR careers)',
    "certification": '"{entity}" (certified OR "partner tier" OR competency)',
    "engineering_output": '"{entity}" (github OR "engineering blog" OR "open source")',
    "growth_signal": '"{entity}" (funding OR acquisition OR award)',
    "commercial_terms": '"{entity}" (rates OR "engagement minimum" OR headcount)',
}

#: The kinds a claim concept feeds — so a gap in a claim becomes a search.
def kind_for_claim(item_id: str) -> str:
    concept = claim_concept(item_id)
    return CLAIM_CONCEPTS[concept][0] if concept else ""


def queries_for_gaps(entity: str, missing_kinds: Any, limit: int = 3) -> list[str]:
    """Targeted queries for the evidence an entity is missing, within the contract."""
    queries: list[str] = []
    for kind in missing_kinds:
        template = KIND_QUERIES.get(str(kind))
        if not template:
            continue
        queries.append(template.format(entity=entity))
        if len(queries) >= limit:
            break
    return queries


#: ``KIND_QUERIES`` above is what to *search* for a kind an entity is missing.
#: This is the other half: which of the entity's own surfaces carry that kind,
#: for the stage that stops searching and goes to look. The names are a
#: vocabulary — the URLs behind them are one file of web knowledge
#: (``data/source_surfaces.json``), so a surface is renamed in one place and the
#: mapping never drifts from what a fetch can actually do.
#:
#: Every lane reads this. What differs between products is which kinds they
#: demand, and that is the lane's bar; there is no per-product variant of where
#: a company keeps its case studies.
KIND_SURFACES: dict[str, tuple[str, ...]] = {
    "delivery_proof": ("case_studies", "services"),
    "stack_delivery": ("services", "case_studies", "blog", "code"),
    "delivery_hiring": ("ats",),
    "named_clients": ("case_studies",),
    # Review directories answered 403 on every domain probed, so independent
    # validation comes from the vendors who publish stories naming the entity
    # and from the communities that discuss it — sources that answer.
    "independent_validation": ("vendor_stories", "community"),
    "certification": ("registry", "partners"),
    # Rates and headcount are published on the entity's own pages, not on the
    # review sites that used to carry them.
    "commercial_terms": ("services", "about"),
    "growth_signal": ("news",),
    "engineering_output": ("blog", "code"),
    "dated_events": ("news", "blog"),
}


def surfaces_for_kinds(missing_kinds: Any) -> tuple[str, ...]:
    """The surfaces that carry these kinds, deduped, in declaration order."""
    ordered: list[str] = []
    for kind in missing_kinds:
        for surface in KIND_SURFACES.get(str(kind), ()):
            if surface not in ordered:
                ordered.append(surface)
    return tuple(ordered)

# ---------------------------------------------------------------------------
# 7. The evidence bar: what counts, and what is merely a lead.
# ---------------------------------------------------------------------------
# There is no trust continuum to tune. A category either may carry a claim or it
# may not; a source that cannot carry it is not averaged in at any weight, it is
# not evidence. That is the whole rule, and it removes every constant that used
# to need fitting.
QUALIFYING_CATEGORIES: dict[str, tuple[str, ...]] = {
    # Claims about work the entity did: a vendor's story, an audit/directory
    # profile, or the entity's own case study (which is evidence of what it says
    # about itself, and is reported as such).
    "delivery_proof": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "FIRST_PARTY_CASE_STUDY"),
    "stack_delivery": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "FIRST_PARTY_CASE_STUDY"),
    "client_outcome": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "FIRST_PARTY_CASE_STUDY"),
    "commercial_terms": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "FIRST_PARTY_PRACTICE"),
    "certification": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "FIRST_PARTY_PRACTICE"),
    "vendor_alliance": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "FIRST_PARTY_PRACTICE"),
    "published_engineering": ("FIRST_PARTY_PRACTICE", "FIRST_PARTY_CASE_STUDY"),
    # Hiring is only ever the board itself.
    "delivery_hiring": ("ATS_REQUISITIONS",),
    # Independent validation must not be the entity's own material.
    "independent_validation": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "COMMUNITY_AND_SOCIAL"),
    # An item no central concept knows still has to clear the evidence bar:
    # a page that can carry evidence about a company. Community chatter and
    # generic pages are leads, not evidence, whatever the item is called.
    "": ("VENDOR_REGISTRY", "B2B_DIRECTORY_AUDIT", "ATS_REQUISITIONS",
         "FIRST_PARTY_CASE_STUDY", "FIRST_PARTY_PRACTICE"),
}
#: The two levels an insight can be shown at. There is no third: either a
#: qualifying source carries it, or it is a lead and is not scored.
CONFIDENCE_LEVELS: dict[str, float] = {"reported": 1.0, "unsupported": 0.0}


def qualifying_categories(item_id: str) -> tuple[str, ...]:
    concept = claim_concept(item_id)
    kind = CLAIM_CONCEPTS[concept][0] if concept else ""
    return QUALIFYING_CATEGORIES.get(kind, QUALIFYING_CATEGORIES[""])


def qualifies(item_id: str, category: str) -> bool:
    """Whether a source of this category may carry this claim at all.

    A source with no category is text whose provenance we do not know — a
    pasted description, a plain file. It can speak for itself (the first-party
    claims), and it can never validate, hire or corroborate.
    """
    allowed = qualifying_categories(item_id)
    if not (category or "").strip():
        return bool(set(allowed) & set(FIRST_PARTY_CATEGORIES))
    return category.upper() in allowed


def support_strength(item_id: str, categories: Any) -> float:
    """One qualifying source is enough; none means the claim is not scored."""
    return 1.0 if any(qualifies(item_id, category) for category in categories) else 0.0


def confidence_weight(level: str) -> float:
    return CONFIDENCE_LEVELS.get(level, 0.0)

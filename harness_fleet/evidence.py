"""Read a dossier the way the recommendation needs it: kinds, conflicts, change.

Three deterministic readings of a bundled dossier, no model calls:

* **coverage** — which data *kinds* the entity actually has evidence for. Fetch
  statistics can look healthy on a dossier that contains nothing but the firm's
  own marketing; a kind is present only when there is prose that speaks to it.
* **minimums** — the enforced evidence set per tier. An entity that lacks the
  kinds a tier claims cannot be presented at that tier, and the reason is
  recorded rather than averaged away.
* **contradictions** — where a claim stands alone. A first-party claim that no
  independent source corroborates is the signal, and both sides are reported
  with their URIs instead of being blended into one score.
* **diff** — what changed between two captures of the same page, with the
  dates attached, because a firm quietly dropping a case study or adding a
  partner tier is itself evidence.

Everything here works on text, so it is equally usable on a partner dossier, an
account brief or an employer profile.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from . import contracts
from .sources import classify_source_category, entity_key_for

SECTION_RE = re.compile(r"^={3,}[ \t]*SECTION:[ \t]*(?P<category>[A-Z_ ]+?)[ \t]*\(URI:[ \t]*(?P<uri>[^)]*)\)[ \t]*={3,}[ \t]*$", re.M)
HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]*(?P<title>.+?)[ \t]*$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
TECH_RE = re.compile(r"(?<![.!?]\s)(?<![.!?])(?<![\w])([A-Z][A-Za-z0-9.+#-]{2,})\b")
_STOPWORDS = {
    "the", "this", "that", "their", "our", "we", "it", "in", "for", "and", "with", "from",
    "overview", "about", "read", "more", "learn", "contact", "home", "case", "study",
    "client", "clients", "customer", "customers", "they", "she", "he", "you",
    "certified", "premier", "advanced", "elite", "partner", "partners", "company",
}
CLIENT_RE = re.compile(
    r"\b(?:clients?|customers?|for)\s+([A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+){0,2})",
)
#: Category roles and tier minimums are declared centrally (contracts.py) so
#: the taxonomy, the scoring rules and the presentation cannot drift apart.
FIRST_PARTY_CATEGORIES = contracts.FIRST_PARTY_CATEGORIES
INDEPENDENT_CATEGORIES = contracts.INDEPENDENT_CATEGORIES
CORROBORATING_CATEGORIES = contracts.CORROBORATING_CATEGORIES
MINIMUM_KINDS = contracts.TIER_MINIMUMS


def parse_sections(text: str) -> list[dict[str, Any]]:
    """Dossier text -> ``[{category, uri, text}]`` in document order.

    Text before the first section marker is returned as a ``PREAMBLE`` section
    when it carries anything, so a bundle without markers is still readable.
    """
    text = text or ""
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        stripped = text.strip()
        return [{"category": "PREAMBLE", "uri": "", "text": stripped}] if stripped else []
    sections: list[dict[str, Any]] = []
    head = text[: matches[0].start()].strip()
    # The dossier header (`# Multi-Source Evidence Dossier: x`, `# source_count: n`)
    # is metadata, not evidence: keep a preamble only when it carries prose.
    if head and any(not line.lstrip().startswith("#") for line in head.splitlines() if line.strip()):
        sections.append({"category": "PREAMBLE", "uri": "", "text": head})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        sections.append({
            "category": match.group("category").strip().upper().replace(" ", "_"),
            "uri": match.group("uri").strip(),
            "text": body,
        })
    return sections



#: Scoring signals live in the central contracts; these names are the reading
#: layer's view of them so existing callers keep working unchanged.
OUTCOME_RE = contracts.OUTCOME_RE
DELIVERY_VERB_RE = contracts.DELIVERY_RE
COMMERCIAL_RE = contracts.COMMERCIAL_RE
CERT_RE = contracts.CERT_RE
GROWTH_RE = contracts.GROWTH_RE

def document_sections(text: str, source_uri: str = "") -> list[dict[str, Any]]:
    """A document's sections, labelling an unlabelled one from its URL.

    A bundled dossier labels its own sections, so those labels are used as
    written. A single fetched page carries no markers, and guessing it is
    first-party would be wrong in the expensive direction: a vendor's story
    about a firm would satisfy the firm's own first-party gate, and a lone
    certification claim on a vendor page would read as the firm's own. The URL
    says which kind of page it is, so that is what labels it.
    """
    sections = parse_sections(text)
    if source_uri and sections and all(s["category"] == "PREAMBLE" for s in sections):
        category = classify_source_category(source_uri).upper()
        return [{"category": category, "uri": source_uri, "text": sections[0]["text"]}]
    return sections


def _technologies(text: str) -> set[str]:
    """Capitalised tokens that read as technology names.

    Sentence-initial words, stopwords and client names are excluded: a claim
    only counts when the token is used mid-sentence as a thing, because a
    contradiction report full of "We" and "The" is worse than no report.
    """
    client_names = {match.group(1).split()[0] for match in CLIENT_RE.finditer(text)}
    found = set()
    for match in TECH_RE.finditer(text):
        token = match.group(1).strip()
        if token.lower() in _STOPWORDS or token in client_names:
            continue
        found.add(token)
    return found


#: Boilerplate that appears on every page and states nothing.
_BOILERPLATE_RE = re.compile(r"(all rights reserved|©|copyright|privacy policy|terms of (use|service)|cookie)", re.I)


def _has_prose(text: str, min_words: int = 20) -> bool:
    """Real sentences, not chrome: a copyright line is not evidence of anything.

    A kind may only be credited when the page says something. Counting a URL's
    category was enough to make "© 2026 Google LLC" an independent validation of
    a firm's work.
    """
    stripped = _BOILERPLATE_RE.sub(" ", text or "").strip()
    return len(stripped.split()) >= min_words


def coverage(text: str, source_uri: str = "") -> dict[str, bool]:
    """Which data kinds this dossier actually carries, from its own prose."""
    sections = document_sections(text, source_uri)
    first_party = [s for s in sections if s["category"] in FIRST_PARTY_CATEGORIES]
    independent = [s for s in sections if s["category"] in CORROBORATING_CATEGORIES]
    vouching = [s for s in sections if s["category"] in INDEPENDENT_CATEGORIES]
    everything = "\n".join(s["text"] for s in sections)

    first_party_stack = set().union(*[_technologies(s["text"]) for s in first_party]) if first_party else set()
    independent_stack = set().union(*[_technologies(s["text"]) for s in independent]) if independent else set()

    delivery_proof = any(
        OUTCOME_RE.search(s["text"]) and DELIVERY_VERB_RE.search(s["text"])
        for s in sections
    )
    return {
        "delivery_proof": bool(delivery_proof),
        "named_clients": bool(CLIENT_RE.search(everything)),
        "stack_delivery": bool(first_party_stack and any(DELIVERY_VERB_RE.search(s["text"]) for s in sections)),
        "independent_validation": any(_has_prose(s["text"]) for s in vouching),
        # The board's *category* is not the evidence: a "no jobs matching this
        # criterion" page is an ATS page that says nothing about hiring. Ten words
        # keeps real (if terse) postings and drops empty result pages.
        "delivery_hiring": any(
            s["category"] == "ATS_REQUISITIONS" and _has_prose(s["text"], min_words=10)
            for s in sections
        ),
        "commercial_terms": bool(COMMERCIAL_RE.search(everything)),
        "certification": bool(CERT_RE.search(everything)),
        "engineering_output": any(
            _has_prose(s["text"])
            and (
                s["category"] == "COMMUNITY_AND_SOCIAL"
                or "github.com" in s["uri"]
                or "/blog" in s["uri"]
            )
            for s in sections
        ),
        "growth_signal": bool(GROWTH_RE.search(everything) and YEAR_RE.search(everything)),
        "dated_events": bool(YEAR_RE.search(everything)),
        "_independent_stack": bool(independent_stack),
        "_first_party_stack": bool(first_party_stack),
    }


def missing_minimums(kinds: dict[str, bool], tier: str) -> list[str]:
    """Kinds a tier requires that this evidence does not have."""
    required = MINIMUM_KINDS.get(tier, ())
    return [kind for kind in required if not kinds.get(kind)]


def enforce_tier(tier: str | None, kinds: dict[str, bool]) -> tuple[str | None, list[str]]:
    """Cap a claimed tier at what the evidence supports, and say why.

    A tier is a claim about evidence, so an unsupported one is reduced to the
    best tier the kinds do support rather than dropped silently. The reasons
    always describe the *claimed* tier's missing kinds: they are what a reader
    has to go and gather, and a cap reported without them is just a lower
    number.
    """
    if tier is None:
        return None, []
    order = ["tier_1", "tier_2", "tier_3"]
    if tier not in order:
        return tier, []  # unfit or unknown is not capped by evidence minimums
    index = order.index(tier)
    claimed_missing = missing_minimums(kinds, tier)
    reasons = [f"{tier} needs {kind}" for kind in claimed_missing]
    for candidate in order[index:]:
        if not missing_minimums(kinds, candidate):
            return (candidate, [] if candidate == tier else reasons)
    # Not even the floor is supported: report the claimed gaps and the floor's.
    floor_missing = missing_minimums(kinds, "tier_3")
    return "tier_3", reasons + [f"tier_3 needs {kind}" for kind in floor_missing]


def contradictions(text: str, source_uri: str = "") -> list[dict[str, Any]]:
    """Claims that stand alone, with both sides named.

    Only concrete textual signals are reported: a claim is recorded with the
    section that makes it, and the counterpart is expressed as what is missing
    rather than inferred. False positives are worse than misses here.
    """
    sections = document_sections(text, source_uri)
    first_party = [s for s in sections if s["category"] in FIRST_PARTY_CATEGORIES]
    independent = [s for s in sections if s["category"] in CORROBORATING_CATEGORIES]
    vouching = [s for s in sections if s["category"] in INDEPENDENT_CATEGORIES]
    findings: list[dict[str, Any]] = []
    independent_text = "\n".join(s["text"] for s in independent)
    vouching_text = "\n".join(s["text"] for s in vouching)

    claimed_stack: set[str] = set()
    for section in first_party:
        if DELIVERY_VERB_RE.search(section["text"]):
            claimed_stack |= _technologies(section["text"])
    uncorroborated = sorted(
        tech for tech in claimed_stack
        if tech not in independent_text and independent
    )
    if uncorroborated:
        findings.append({
            "kind": "stack_claim_uncorroborated",
            "claim": f"first-party delivery of {', '.join(uncorroborated[:6])}",
            "counterpart": "no independent or vendor source mentions these",
            "uris": [s["uri"] for s in first_party if any(t in s["text"] for t in uncorroborated)][:5],
        })

    if first_party and CERT_RE.search("\n".join(s["text"] for s in first_party)):
        if not CERT_RE.search(vouching_text):
            findings.append({
                "kind": "certification_unverified",
                "claim": "a vendor certification or partner tier is claimed",
                "counterpart": "no vendor-registry or directory source confirms it",
                "uris": [s["uri"] for s in first_party if CERT_RE.search(s["text"])][:5],
            })

    commercial_in_first_party = bool(COMMERCIAL_RE.search("\n".join(s["text"] for s in first_party)))
    if commercial_in_first_party and not COMMERCIAL_RE.search(vouching_text):
        findings.append({
            "kind": "commercial_terms_unverified",
            "claim": "commercial terms are stated",
            "counterpart": "no directory or review source carries them",
            "uris": [s["uri"] for s in first_party if COMMERCIAL_RE.search(s["text"])][:5],
        })

    hiring_text = "\n".join(s["text"] for s in sections if s["category"] == "ATS_REQUISITIONS")
    if hiring_text and claimed_stack:
        hiring_stack = _technologies(hiring_text)
        if hiring_stack and not (hiring_stack & claimed_stack):
            findings.append({
                "kind": "hiring_contradicts_claimed_stack",
                "claim": f"delivery of {', '.join(sorted(claimed_stack)[:4])}",
                "counterpart": f"open roles mention {', '.join(sorted(hiring_stack)[:4])} instead",
                "uris": [s["uri"] for s in sections if s["category"] == "ATS_REQUISITIONS"][:3],
            })

    return findings


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n{2,}", text) if part.strip()]


def diff_snapshots(
    old_text: str,
    new_text: str,
    *,
    old_date: str = "",
    new_date: str = "",
    max_items: int = 25,
) -> dict[str, Any]:
    """What changed between two captures of the same page, dates attached.

    Section-level first (a case study added or dropped), then sentences within a
    section. Deterministic text comparison: the point is to point a reader at
    what changed, not to summarise it.
    """
    old_sections = parse_sections(old_text)
    new_sections = parse_sections(new_text)
    key = lambda s: (s["category"], s["uri"])  # noqa: E731
    old_map = {key(s): s for s in old_sections}
    new_map = {key(s): s for s in new_sections}

    added = [{"category": k[0], "uri": k[1]} for k in new_map if k not in old_map][:max_items]
    removed = [{"category": k[0], "uri": k[1]} for k in old_map if k not in new_map][:max_items]

    changed: list[dict[str, Any]] = []
    for k in new_map:
        if k not in old_map:
            continue
        before = set(_sentences(old_map[k]["text"]))
        after = set(_sentences(new_map[k]["text"]))
        if before == after:
            continue
        changed.append({
            "category": k[0],
            "uri": k[1],
            "sentences_added": [s for s in _sentences(new_map[k]["text"]) if s not in before][:5],
            "sentences_removed": [s for s in _sentences(old_map[k]["text"]) if s not in after][:5],
        })

    return {
        "old_date": old_date,
        "new_date": new_date,
        "sections_added": added,
        "sections_removed": removed,
        "changed": changed[:max_items],
        "changed_count": len(changed),
        "evidence_changed": bool(added or removed or changed),
    }


def claim_confidence(text: str, source_uri: str = "") -> list[dict[str, Any]]:
    """Each kind this document carries, with the confidence it is shown at.

    The reading layer decides *what* is present; the central contract decides
    what that is worth, and this reports the two together so a reader can see
    "this is a real delivery proof" next to "this one rests on the firm's own
    page". Nothing here scores: presentation only carries the level.
    """
    sections = document_sections(text, source_uri)
    findings: list[dict[str, Any]] = []
    kinds = coverage(text, source_uri)
    for kind, present in kinds.items():
        if kind.startswith("_") or not present:
            continue
        carriers = [s["category"] for s in sections if _kind_in_section(kind, s, kinds)]
        third_party = [c for c in carriers if c in INDEPENDENT_CATEGORIES]
        category = (third_party or carriers or [""])[0]
        # Two levels only: a qualifying source carries the insight, or it is a
        # lead. There is no middle band to argue about.
        level = "reported" if carriers else "unsupported"
        findings.append({
            "kind": kind,
            "confidence": level,
            "weight": contracts.confidence_weight(level),
            "category": category or "unlabelled",
            "shown_as": (
                "carried by a qualifying source" if level == "reported"
                else "not evidence yet; a lead to chase"
            ),
        })
    return findings


def _kind_in_section(kind: str, section: dict[str, Any], kinds: dict[str, bool]) -> bool:
    """Whether this section is where the kind came from (best-effort, per kind)."""
    category = section["category"]
    if kind == "independent_validation":
        return category in INDEPENDENT_CATEGORIES
    if kind == "delivery_hiring":
        return category == "ATS_REQUISITIONS"
    if kind in ("delivery_proof", "stack_delivery"):
        return category in FIRST_PARTY_CATEGORIES or category in INDEPENDENT_CATEGORIES
    return bool(kinds.get(kind))


def entity_evidence(
    text: str, *, tier: str | None = None, source_uri: str = ""
) -> dict[str, Any]:
    """Everything above for one entity, ready for a record or a board payload."""
    kinds = coverage(text, source_uri)
    tier_after, reasons = enforce_tier(tier, kinds)
    confidences = claim_confidence(text, source_uri)
    return {
        "kinds": {k: v for k, v in kinds.items() if not k.startswith("_")},
        "missing_for_tier": missing_minimums(kinds, tier) if tier else [],
        "tier_claimed": tier,
        "tier_supported": tier_after,
        "tier_capped": bool(tier and tier_after != tier),
        "tier_reasons": reasons,
        "contradictions": contradictions(text, source_uri),
        "sections": len(document_sections(text, source_uri)),
        # What each insight is shown with, so presentation never has to guess.
        "confidences": confidences,
        "weakest": min((f["weight"] for f in confidences), default=0.0),
    }


def summarize(kinds: Iterable[dict[str, bool]]) -> dict[str, int]:
    """How many entities carry each kind, for a run-level readout."""
    totals: dict[str, int] = {}
    for entry in kinds:
        for kind, present in entry.items():
            totals[kind] = totals.get(kind, 0) + (1 if present else 0)
    return dict(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))


def _entity_sources(items: Iterable[Any]) -> dict[str, dict[str, str]]:
    """Group items as ``{entity: {source_uri: text}}``, the unit a diff needs."""
    grouped: dict[str, dict[str, str]] = {}
    for item in items:
        uri = str(getattr(item, "source_uri", "") or getattr(item, "item_id", "") or "")
        text = str(getattr(item, "text", "") or "")
        metadata = getattr(item, "metadata", None)
        entity = entity_key_for(uri, text, metadata if isinstance(metadata, dict) else {})
        grouped.setdefault(entity, {})[uri or entity] = text
    return grouped


def compare_snapshots(
    old_items: Iterable[Any],
    new_items: Iterable[Any],
    *,
    old_date: str = "",
    new_date: str = "",
    max_items: int = 25,
) -> dict[str, Any]:
    """What changed per entity between two captures, dates attached.

    The unit is the entity, not the row: the same account arrives from several
    sources, so a source appearing or disappearing is entity-level news (a
    vendor published a story about them; their case study page went away).
    Sentence-level changes are reported per source, because "the page changed"
    is not actionable and "they dropped the client they used to name" is.
    """
    old_map = _entity_sources(old_items)
    new_map = _entity_sources(new_items)
    added_entities = sorted(entity for entity in new_map if entity not in old_map)
    removed_entities = sorted(entity for entity in old_map if entity not in new_map)

    sources_added: dict[str, list[str]] = {}
    sources_removed: dict[str, list[str]] = {}
    changed: list[dict[str, Any]] = []
    unchanged = 0
    for entity in sorted(set(old_map) & set(new_map)):
        before, after = old_map[entity], new_map[entity]
        appeared = sorted(uri for uri in after if uri not in before)
        vanished = sorted(uri for uri in before if uri not in after)
        if appeared:
            sources_added[entity] = appeared[:max_items]
        if vanished:
            sources_removed[entity] = vanished[:max_items]
        entity_changed = False
        for uri in sorted(set(before) & set(after)):
            old_sentences = set(_sentences(before[uri]))
            new_sentences = set(_sentences(after[uri]))
            if old_sentences == new_sentences:
                continue
            entity_changed = True
            changed.append({
                "entity": entity,
                "uri": uri,
                "sentences_added": [s for s in _sentences(after[uri]) if s not in old_sentences][:5],
                "sentences_removed": [s for s in _sentences(before[uri]) if s not in new_sentences][:5],
            })
        if not entity_changed and not appeared and not vanished:
            unchanged += 1

    return {
        "old_date": old_date,
        "new_date": new_date,
        "entities_added": added_entities[:max_items],
        "entities_removed": removed_entities[:max_items],
        "sources_added": sources_added,
        "sources_removed": sources_removed,
        "changed": changed[:max_items],
        "changed_count": len(changed),
        "entities_unchanged": unchanged,
        "evidence_changed": bool(added_entities or removed_entities or sources_added or sources_removed or changed),
    }


# ---------------------------------------------------------------------------
# Central claim requirements: what a quote must actually say to back a claim.
#
# Products name their own checklist items, but the *concepts* are shared, and
# so is the mechanical test for each one. A quote that does not address its
# claim contributes nothing, however good its source: an acquirer's press
# release cannot support "delivers this stack" if it names no technology, and
# a forum remark about culture cannot support independent validation of work.
# Products stay specific only at the edges (their own item names and weights).
# ---------------------------------------------------------------------------
def claim_concept(item_id: str) -> str:
    """The shared concept behind a product's checklist item id (see contracts)."""
    return contracts.claim_concept(item_id)


def quote_addresses_claim(
    item_id: str, quote_text: str, category: str = "", terms: Iterable[str] = ()
) -> tuple[bool, str]:
    """Whether this quote can back this claim, and why not when it cannot."""
    return contracts.quote_addresses_claim(item_id, quote_text, category, terms)


def section_category_at(text: str, offset: int) -> str:
    """The dossier section a quote offset falls in, or "" for a plain document."""
    marks = list(SECTION_RE.finditer(text or ""))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        if mark.end() <= offset < end:
            return mark.group("category").strip().upper()
    return ""

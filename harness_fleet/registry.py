"""The growing part of the taxonomy: sources the seed lists do not know yet.

The static lists in ``sources.py`` are a seed, not the truth. A source nobody
listed — an ATS nobody uses yet, a vendor hub launched last week, a directory
specific to one industry — must be able to *become* known without a code change,
and it must not be silently guessed at in the meantime.

Three states, in order:

* **unknown** — seen but unclassified. Recorded as a candidate with the evidence
  that was observed, and treated as a lead, never as proof.
* **proposed** — the offline pass has enough signal to suggest a category, with
  the counts and features behind it. Still a lead.
* **known** — promoted into the registry, by a person or by a high-confidence
  proposal. From then on ``classify_source_category`` returns that category,
  and the source carries claims under the normal evidence bar.

The growth pass is deliberately *not* part of scoring: scoring must stay a pure
function of the contracts and the text, so a source seen during one run can
never change the meaning of another run's score retroactively.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_REGISTRY = Path("source_registry.json")
#: Where a run records what it sees. The environment variable exists so a test
#: suite (or any hosted runner) can keep a workspace registry out of the repo it
#: is running in — the registry is a person's data, never the repository's.
REGISTRY_ENV = "HARNESS_FLEET_REGISTRY"


def default_registry_path() -> Path:
    import os

    configured = os.environ.get(REGISTRY_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_REGISTRY
#: A domain must be seen this often before it is proposed for a category.
MIN_SIGHTINGS_FOR_PROPOSAL = 2
#: Automatic promotion: nobody is going to sit and promote domains, so the
#: registry promotes itself once the evidence is consistent. A domain needs
#: this many sightings, this much confidence, and this much of a lead over the
#: runner-up category before it is promoted without anyone asking.
AUTO_PROMOTE_MIN_SIGHTINGS = 2
AUTO_PROMOTE_MIN_CONFIDENCE = 0.35
AUTO_PROMOTE_MARGIN = 1.5
#: Signals precise enough to promote on the first sighting: a page that
#: publishes a job posting is an ATS board, and waiting for a second look
#: would only lose the evidence.
HIGH_PRECISION_SIGNALS: dict[str, tuple[str, ...]] = {
    "ats_requisitions": ("/careers", "/jobs", "jobposting", "apply now"),
    "community_and_social": ("/comments", "/thread"),
}
#: Signals a growth pass can observe on a page, mapped to the category they
#: suggest. This is the "little math": a likelihood, not a lookup.
CATEGORY_SIGNALS: dict[str, tuple[str, ...]] = {
    "ats_requisitions": ("/jobs", "/careers", "/positions", "jobposting", "apply"),
    "vendor_registry": ("/partners", "/customers", "/case-stud", "/success", "partner"),
    "b2b_directory_audit": ("/profile", "/reviews", "/directory", "clutch", "rating"),
    "community_and_social": ("/comments", "/thread", "/r/", "forum", "discussion"),
    "first_party_case_study": ("/case-stud", "/work", "/clients", "/portfolio"),
    "first_party_practice": ("/services", "/solutions", "/practice", "/about"),
}


def _domain(identifier: str) -> str:
    host = urlparse(identifier if "://" in identifier else f"https://{identifier}").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def load(path: Path | str | None = None) -> dict[str, Any]:
    """The registry as it stands, or an empty one."""
    target = Path(path) if path else default_registry_path()
    if not target.is_file():
        return {"domains": {}, "candidates": {}}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"domains": {}, "candidates": {}}
    loaded.setdefault("domains", {})
    loaded.setdefault("candidates", {})
    return loaded


def save(registry: dict[str, Any], path: Path | str | None = None) -> Path:
    target = Path(path) if path else default_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return target


def score_signals(url: str, text: str = "") -> dict[str, float]:
    """How much this observation looks like each category. No lookup involved.

    A crude likelihood on purpose: paths and page vocabulary are what a growth
    pass can see at scale, and a proposal only has to be good enough to review.
    """
    haystack = f"{url or ''} {text or ''}".lower()
    if not haystack.strip():
        return {}
    hits: dict[str, float] = {}
    for category, signals in CATEGORY_SIGNALS.items():
        matched = sum(1 for signal in signals if signal in haystack)
        if matched:
            hits[category] = matched / len(signals)
    return hits


def observe(url: str, text: str = "", path: Path | str | None = None) -> dict[str, Any]:
    """Record one sighting of an unclassified domain; return the candidate."""
    domain = _domain(url)
    registry = load(path)
    if domain in registry["domains"]:
        return registry["domains"][domain]
    candidate = registry["candidates"].get(domain) or {
        "domain": domain, "sightings": 0, "sample_urls": [], "signals": {}, "first_seen": None,
    }
    candidate["sightings"] += 1
    if url and url not in candidate["sample_urls"]:
        candidate["sample_urls"] = ([url] + candidate["sample_urls"])[:5]
    for category, weight in score_signals(url, text).items():
        candidate["signals"][category] = round(candidate["signals"].get(category, 0.0) + weight, 4)
    candidate["first_seen"] = candidate["first_seen"] or datetime.now(timezone.utc).isoformat()
    registry["candidates"][domain] = candidate
    promoted, why = auto_promotion(candidate)
    if promoted:
        registry["domains"][domain] = {
            "category": promoted,
            "reason": why,
            "promoted_by": "auto",
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "sightings": candidate["sightings"],
            "signals": candidate["signals"],
            "sample_urls": candidate.get("sample_urls", []),
        }
        registry["candidates"].pop(domain, None)
    save(registry, path)
    return registry["domains"].get(domain) or candidate


def demote(domain: str, *, reason: str = "", path: Path | str | None = None) -> dict[str, Any]:
    """Undo a promotion. The safety valve that makes automatic growth safe.

    With promotion automatic, correction has to be one command rather than a
    review queue: a wrong classification is removed, recorded, and its domain
    returns to being a candidate (a lead, not evidence).
    """
    registry = load(path)
    target = _domain(domain)
    entry = registry["domains"].pop(target, None)
    if entry is None:
        raise ValueError(f"{target} is not in the registry")
    registry.setdefault("demotions", {})[target] = {
        "was": entry.get("category"),
        "reason": reason or "demoted",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    save(registry, path)
    return {"domain": target, "was": entry.get("category"), "reason": reason or "demoted"}


def auto_promotion(candidate: dict[str, Any]) -> tuple[str | None, str]:
    """The category this candidate has earned, and why — or (None, reason).

    Pure function of what has been observed: no clock, no randomness, so the
    same sightings always produce the same taxonomy, and every promotion can be
    explained from the record it kept.
    """
    signals = {k: float(v) for k, v in (candidate.get("signals") or {}).items() if v}
    if not signals:
        return None, "no signals observed yet"
    ranked = sorted(signals.items(), key=lambda item: -item[1])
    category, weight = ranked[0]
    samples = " ".join(candidate.get("sample_urls") or []).lower()
    # An unambiguous marker is enough on its own: a page that carries a job
    # posting is a hiring board whether or not it has been seen twice.
    for precise, markers in HIGH_PRECISION_SIGNALS.items():
        matched = [marker for marker in markers if marker in samples]
        decisive = [marker for marker in matched if marker in ("/careers", "/jobs", "jobposting", "/comments", "/thread")]
        if len(matched) >= 2 or decisive:
            return precise, (
                f"precise signal(s) {', '.join(matched)} on {int(candidate.get('sightings') or 0)} sighting(s)"
            )
    sightings = int(candidate.get("sightings") or 0)
    if sightings < AUTO_PROMOTE_MIN_SIGHTINGS:
        return None, f"only {sightings} sighting(s)"
    confidence = weight / max(1.0, sightings)
    if confidence < AUTO_PROMOTE_MIN_CONFIDENCE:
        return None, f"confidence {confidence:.2f} below {AUTO_PROMOTE_MIN_CONFIDENCE}"
    if len(ranked) > 1 and ranked[0][1] < ranked[1][1] * AUTO_PROMOTE_MARGIN:
        return None, f"'{category}' is not clearly ahead of '{ranked[1][0]}'"
    return category, f"{sightings} sighting(s), confidence {confidence:.2f}, lead over {ranked[1][0] if len(ranked) > 1 else 'no rival'}"


def propose(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Candidates with enough evidence to suggest a category, best first."""
    registry = load(path)
    proposals: list[dict[str, Any]] = []
    for domain, candidate in registry["candidates"].items():
        if candidate.get("sightings", 0) < MIN_SIGHTINGS_FOR_PROPOSAL:
            continue
        signals = candidate.get("signals") or {}
        if not signals:
            continue
        category, weight = max(signals.items(), key=lambda item: item[1])
        proposals.append({
            "domain": domain,
            "category": category,
            "confidence": round(min(1.0, weight / max(1, candidate["sightings"] / 2)), 3),
            "sightings": candidate["sightings"],
            "signals": signals,
            "sample_urls": candidate.get("sample_urls", []),
        })
    return sorted(proposals, key=lambda entry: -entry["confidence"])


def promote(
    domain: str, category: str, *, reason: str = "", path: Path | str | None = None
) -> dict[str, Any]:
    """Make a domain known. Returns the registry entry."""
    registry = load(path)
    entry = {
        "category": category,
        "reason": reason or "promoted",
        "promoted_by": "manual",
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "sightings": (registry["candidates"].get(_domain(domain)) or {}).get("sightings", 0),
    }
    registry["domains"][_domain(domain)] = entry
    registry["candidates"].pop(_domain(domain), None)
    save(registry, path)
    return entry


def lookup(host: str, path: Path | str | None = None) -> str | None:
    """The promoted category for a host, if it has one."""
    registry = load(path)
    known = registry["domains"].get(_domain(host))
    return known["category"] if known else None

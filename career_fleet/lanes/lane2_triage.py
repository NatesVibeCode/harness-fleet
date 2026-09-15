"""Lane 2: Gatekeeper Triage.
Rapidly filters candidate companies against hard dealbreakers:
- Excessive headcount (> 80-100 people)
- Mandatory non-local in-person office mandates (SF/NYC 4-5 day office policies)
- Shallow prompt-wrapper architecture
Drops disqualified companies immediately to save time and tokens.
"""
from __future__ import annotations

import re
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from career_fleet.profile import IdealEmployerProfile
from career_fleet.store import CareerStore

OFFICE_MANDATE_PATTERNS = [
    re.compile(r"\b(?:5|4)\s*days?\s*(?:a\s*week\s*)?(?:in\s*(?:the\s*|our\s*)?(?:\w+\s*)?office|on[- ]?site)\b", re.I),
    re.compile(r"\bmandatory\s*(?:in[- ]?office|in[- ]?person|presence)\b", re.I),
    re.compile(r"\b(?:san\s*francisco|sf|new\s*york|nyc)\s*(?:office|in[- ]?person)\s*(?:required|mandatory)\b", re.I),
    re.compile(r"\bno\s*remote\s*(?:option|work|allowed)\b", re.I),
    re.compile(r"\b(?:office|on[- ]?site|in[- ]?person|in[- ]?office)\b[^.\n]{0,60}\b(?:required|mandatory|must)\b", re.I),
    re.compile(r"\b(?:required|mandatory|must)\b[^.\n]{0,60}\b(?:office|on[- ]?site|in[- ]?person|in[- ]?office)\b", re.I),
]

REMOTE_ONLY_DAYS_PATTERN = re.compile(
    r"\b(?:[1-5])\s*days?\s*(?:a\s*week|per\s*week|each\s*week)?\s*"
    r"(?:in\s*(?:the\s*|our\s*)?(?:\w+\s*)?office|on[- ]?site)\b",
    re.I,
)

WRAPPER_PATTERNS = [
    re.compile(r"\bwrapper\s*around\s*(?:chatgpt|openai)\b", re.I),
    re.compile(r"\bprompt\s*engineering\s*agency\b", re.I),
    re.compile(r"\bai\s*sdr\s*(?:spammer|cold\s*email\s*generator)\b", re.I),
]

QUOTA_PATTERNS = [
    re.compile(r"\b(?:pure\s+quota|quota[- ]only|quota[- ]carrying|cold[- ]calling|cold[- ]outbound|boiler[- ]room)\b", re.I),
]

REMOTE_NEGATION_PATTERNS = [
    re.compile(
        r"\b(?:no|not|without)\s+(?:(?:a|an)\s+)?(?:fully\s+)?remote(?:[- ]?(?:role|position|job))?\b|"
        r"\bremote\s+(?:work\s+)?(?:is\s+)?(?:not|never)\s+(?:required|allowed|available|permitted|possible|offered)\b|"
        r"\bremote\s+(?:work\s+)?(?:is\s+)?optional\b|"
        r"\b(?:cannot|can't|will\s+not|won't|may\s+not)\s+(?:(?:be|work)\s+)?(?:fully\s+)?remote(?:ly)?\b",
        re.I,
    ),
]
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
    r"\bwork[- ]from[- ]anywhere\b",
    re.I,
)
HYBRID_FIRST_PATTERN = re.compile(r"\bhybrid[- ]first\b", re.I)
HYBRID_ROLE_POSITIVE_PATTERN = re.compile(
    r"\bhybrid[- ]?(?:role|position|schedule)\b|"
    r"\b(?:this|the|a|your)\s+(?:role|position|job)\s+(?:is\s+)?hybrid\b|"
    r"\b(?:can|may|will)\s+work\s+(?:in\s+)?a\s+hybrid\b",
    re.I,
)
HYBRID_NEGATION_PATTERN = re.compile(
    r"\b(?:no|not|without)\s+(?:(?:a|an)\s+)?hybrid(?:[- ]?(?:role|position|job|work|schedule|model))?\b|"
    r"\bhybrid\s+(?:work|working|schedule|model|role|position)?\s*(?:is|are)\s+(?:not|never)\s+(?:required|available|offered|permitted|allowed|possible|provided)\b|"
    r"\b(?:cannot|can't|will\s+not|won't|may\s+not)\s+(?:(?:be|work)\s+)?(?:a\s+)?hybrid\b",
    re.I,
)
REMOTE_LOCATION_PATTERN = re.compile(r"\b(?:remote|anywhere)\b", re.I)

LOCATION_STOPWORDS = {
    "a", "an", "and", "at", "day", "days", "each", "in", "mandate", "mandatory",
    "of", "office", "on", "onsite", "on-site", "per", "presence", "required", "site",
    "the", "week", "with",
}

# Use one token for multi-word places so either side of an alias comparison
# works ("SF" ↔ "San Francisco", "NYC" ↔ "New York").
LOCATION_CANONICAL_ALIASES = {
    "san francisco": "san_francisco",
    "sf": "san_francisco",
    "new york": "new_york",
    "nyc": "new_york",
    "ny": "new_york",
    "california": "california",
    "ca": "california",
    "colorado": "colorado",
    "co": "colorado",
    "illinois": "illinois",
    "il": "illinois",
    "massachusetts": "massachusetts",
    "ma": "massachusetts",
    "texas": "texas",
    "tx": "texas",
    "washington": "washington",
    "wa": "washington",
}


def _screening_text(company: dict[str, Any], postings: list[dict[str, Any]]) -> str:
    """Combine source text and structured location metadata for screening."""
    parts = [str(company.get("hq_location") or "")]
    for posting in postings:
        parts.extend(
            [
                str(posting.get("raw_text") or ""),
                str(posting.get("location") or ""),
            ]
        )
    return "\n".join(part for part in parts if part.strip())


def _location_matches(text: str, configured_location: str) -> bool:
    """Match the meaningful location words, ignoring policy words."""
    def canonical_tokens(value: str) -> set[str]:
        normalized = " ".join(re.findall(r"[A-Za-z0-9]+", value.casefold()))
        for variant, canonical in sorted(LOCATION_CANONICAL_ALIASES.items(), key=lambda pair: len(pair[0]), reverse=True):
            normalized = re.sub(
                r"(?<!\w)" + re.escape(variant) + r"(?!\w)",
                canonical,
                normalized,
            )
        return {
            token
            for token in re.findall(r"[A-Za-z0-9_]+", normalized)
            if token not in LOCATION_STOPWORDS
        }

    terms = canonical_tokens(configured_location)
    if not terms:
        return False
    return terms <= canonical_tokens(text)


def _has_remote_evidence(posting: dict[str, Any]) -> bool:
    raw_text = str(posting.get("raw_text") or "")
    location = str(posting.get("location") or "").strip()
    text = " ".join(part for part in (raw_text, location) if part)
    if any(pattern.search(text) for pattern in REMOTE_NEGATION_PATTERNS):
        return False
    if bool(posting.get("is_remote")):
        return True
    if location and not REMOTE_LOCATION_PATTERN.search(location):
        # A company-level "remote-first" statement must not override a
        # concrete city in an ATS location field unless the role itself is
        # explicitly remote.
        return bool(REMOTE_ROLE_POSITIVE_PATTERN.search(raw_text))
    if REMOTE_FIRST_PATTERN.search(text) and not REMOTE_ROLE_POSITIVE_PATTERN.search(raw_text):
        return False
    return bool(REMOTE_POSITIVE_PATTERN.search(text))


def _has_remote_or_hybrid_evidence(posting: dict[str, Any]) -> bool:
    raw_text = str(posting.get("raw_text") or "")
    location = str(posting.get("location") or "").strip()
    text = " ".join(part for part in (raw_text, location) if part)
    if HYBRID_NEGATION_PATTERN.search(text):
        return _has_remote_evidence(posting)
    hybrid_location = bool(re.search(r"\bhybrid\b", location, re.I))
    if hybrid_location or HYBRID_ROLE_POSITIVE_PATTERN.search(raw_text):
        return True
    if HYBRID_FIRST_PATTERN.search(text):
        # "Hybrid-first" describes a company policy, not necessarily this role.
        return _has_remote_evidence(posting)
    return _has_remote_evidence(posting)


def _configured_location_mandate(text: str, configured_location: str) -> str | None:
    """Find a non-negated mandate in the same sentence as a configured location."""
    for sentence in re.split(r"(?<=[.!?\n])\s*", text):
        if not _location_matches(sentence, configured_location):
            continue
        office_match = next((pattern.search(sentence) for pattern in OFFICE_MANDATE_PATTERNS if pattern.search(sentence)), None)
        if office_match:
            return sentence.strip()
        for mandate in re.finditer(r"\b(?:required|mandatory|must)\b", sentence, re.I):
            before = sentence[max(0, mandate.start() - 30) : mandate.start()]
            if not re.search(r"\b(?:not|no|without|optional|voluntary)\b", before, re.I):
                return sentence.strip()
    return None


def _business_day_overlap_hours(candidate_timezone: str, employer_timezone: str) -> float:
    """Calculate conservative overlap for a standard 09:00–17:00 workday.

    Checking both winter and summer avoids a false pass when daylight-saving
    changes the offset difference between the candidate and employer zones.
    """
    try:
        candidate_zone = ZoneInfo(candidate_timezone)
        employer_zone = ZoneInfo(employer_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {exc}") from exc

    overlaps = []
    for month in (1, 7):
        reference = datetime(2026, month, 15, 12, tzinfo=dt_timezone.utc)
        candidate_offset = reference.astimezone(candidate_zone).utcoffset()
        employer_offset = reference.astimezone(employer_zone).utcoffset()
        if candidate_offset is None or employer_offset is None:
            overlaps.append(0.0)
            continue

        candidate_start = 9.0 - candidate_offset.total_seconds() / 3600
        candidate_end = 17.0 - candidate_offset.total_seconds() / 3600
        employer_start = 9.0 - employer_offset.total_seconds() / 3600
        employer_end = 17.0 - employer_offset.total_seconds() / 3600
        overlaps.append(max(0.0, min(candidate_end, employer_end) - max(candidate_start, employer_start)))
    return min(overlaps)


def _check_timezone_overlap(
    company: dict[str, Any],
    postings: list[dict[str, Any]],
    profile: IdealEmployerProfile,
) -> dict[str, Any] | None:
    dealbreakers = profile.dealbreakers
    candidate_timezone = (dealbreakers.candidate_timezone or "").strip()
    if not candidate_timezone or dealbreakers.min_timezone_overlap_hours <= 0:
        return None

    employer_timezones = [str(company.get("timezone") or "").strip()]
    employer_timezones.extend(str(posting.get("timezone") or "").strip() for posting in postings)
    employer_timezones = [value for value in employer_timezones if value]
    if not employer_timezones:
        return {
            "disqualified": True,
            "reason": "Timezone overlap could not be verified because the captured source has no timezone metadata.",
            "rule": "timezone_overlap_unknown",
        }

    overlaps = []
    for employer_timezone in employer_timezones:
        try:
            overlaps.append(_business_day_overlap_hours(candidate_timezone, employer_timezone))
        except ValueError as exc:
            return {
                "disqualified": True,
                "reason": str(exc),
                "rule": "invalid_timezone",
            }

    best_overlap = max(overlaps)
    if best_overlap < dealbreakers.min_timezone_overlap_hours:
        return {
            "disqualified": True,
            "reason": (
                f"Best business-hour timezone overlap ({best_overlap:.1f}h) is below the required "
                f"{dealbreakers.min_timezone_overlap_hours:.1f}h."
            ),
            "rule": "timezone_overlap",
            "quote": employer_timezones[overlaps.index(best_overlap)],
        }
    return None


def check_dealbreakers(
    company: dict[str, Any],
    postings: list[dict[str, Any]],
    profile: IdealEmployerProfile,
) -> dict[str, Any] | None:
    """Return disqualification dictionary if any hard dealbreaker triggers, else None."""
    dealbreakers = profile.dealbreakers

    # 1. Headcount check
    headcount = company.get("headcount")
    if dealbreakers.max_headcount is not None:
        if headcount is None:
            if dealbreakers.require_verified_headcount:
                return {
                    "disqualified": True,
                    "reason": "Headcount could not be verified while a maximum headcount is configured.",
                    "rule": "headcount_unknown",
                }
        try:
            hc_int = int(headcount if headcount is not None else "unknown")
            if hc_int > dealbreakers.max_headcount:
                return {
                    "disqualified": True,
                    "reason": f"Headcount ({hc_int}) exceeds maximum threshold ({dealbreakers.max_headcount})",
                    "rule": "headcount_limit",
                }
        except (ValueError, TypeError):
            if dealbreakers.require_verified_headcount:
                return {
                    "disqualified": True,
                    "reason": f"Headcount value {headcount!r} could not be verified while a maximum headcount is configured.",
                    "rule": "headcount_unknown",
                }

    # 2. In-person mandate check across job postings and structured metadata.
    combined_text = _screening_text(company, postings)
    if dealbreakers.policy in ("remote_only", "remote_or_hybrid"):
        office_patterns = (
            [REMOTE_ONLY_DAYS_PATTERN, *OFFICE_MANDATE_PATTERNS]
            if dealbreakers.policy == "remote_only"
            else OFFICE_MANDATE_PATTERNS
        )
        for pat in office_patterns:
            match = pat.search(combined_text)
            if match:
                matched_snippet = combined_text[max(0, match.start() - 40) : min(len(combined_text), match.end() + 40)]
                return {
                    "disqualified": True,
                    "reason": f"Mandatory in-office policy detected: '{matched_snippet.strip()}'",
                    "rule": "office_mandate",
                    "quote": match.group(0),
                }

        if dealbreakers.policy == "remote_only":
            for posting in postings:
                location = str(posting.get("location") or "").strip()
                if location and not _has_remote_evidence(posting):
                    return {
                        "disqualified": True,
                        "reason": f"Remote-only policy conflicts with posting location '{location}'.",
                        "rule": "non_remote_location",
                        "quote": location,
                    }
        elif not postings or not any(_has_remote_or_hybrid_evidence(posting) for posting in postings):
            return {
                "disqualified": True,
                "reason": "Remote-or-hybrid policy could not be verified from the captured source.",
                "rule": "workplace_policy_unknown",
            }

    # Configured location exclusions are hard exclusions even when the user
    # allows other kinds of work arrangements.
    for configured_location in dealbreakers.disallowed_locations:
        for posting in postings:
            location = str(posting.get("location") or "").strip()
            if location and _location_matches(location, configured_location) and not _has_remote_evidence(posting):
                return {
                    "disqualified": True,
                    "reason": f"Posting location matches configured exclusion '{configured_location}': '{location}'",
                    "rule": "configured_location",
                    "quote": location,
                }
        mandate = _configured_location_mandate(combined_text, configured_location)
        if mandate:
            return {
                "disqualified": True,
                "reason": f"Mandatory work location matches configured exclusion '{configured_location}': '{mandate}'",
                "rule": "configured_location",
                "quote": mandate,
            }

    if dealbreakers.policy == "remote_only" and (
        not postings or not any(_has_remote_evidence(posting) for posting in postings)
    ):
        return {
            "disqualified": True,
            "reason": "Remote-only policy could not be verified from the captured source.",
            "rule": "remote_policy_unknown",
        }

    timezone_issue = _check_timezone_overlap(company, postings, profile)
    if timezone_issue:
        return timezone_issue

    # 3. Shallow wrapper check
    if dealbreakers.reject_thin_wrappers:
        for pat in WRAPPER_PATTERNS:
            match = pat.search(combined_text)
            if match:
                return {
                    "disqualified": True,
                    "reason": f"Shallow AI wrapper signal detected: '{match.group(0)}'",
                    "rule": "thin_wrapper",
                    "quote": match.group(0),
                }

    # 4. Pure quota / boiler-room check
    if dealbreakers.reject_pure_quota:
        for pat in QUOTA_PATTERNS:
            match = pat.search(combined_text)
            if match:
                return {
                    "disqualified": True,
                    "reason": f"Pure quota-sales signal detected: '{match.group(0)}'",
                    "rule": "pure_quota",
                    "quote": match.group(0),
                }

    return None


def run_lane2_triage(
    store: CareerStore,
    profile: IdealEmployerProfile,
) -> dict[str, Any]:
    """Execute Gatekeeper Triage across all tracked companies.

    Rechecking processed companies lets profile edits take effect. A new
    Lane 2 result invalidates downstream lane results in the store.
    """
    discovered = store.list_companies()
    profile_revision_id = store.save_profile(profile)
    triaged = 0
    passed = 0
    dropped = 0

    for comp in discovered:
        cid = comp["id"]
        dossier = store.get_company_dossier(cid)
        postings = dossier.get("jobs", []) if dossier else []

        dq = check_dealbreakers(comp, postings, profile)
        if dq:
            store.record_evaluation(
                eval_id=f"eval-triage-{cid}",
                company_id=cid,
                lane="lane2_triage",
                status="disqualified",
                score=0.0,
                verdict="DISQUALIFIED",
                rationale=dq["reason"],
                quotes=[dq["quote"]] if dq.get("quote") else [],
                profile_revision_id=profile_revision_id,
            )
            dropped += 1
        else:
            store.record_evaluation(
                eval_id=f"eval-triage-{cid}",
                company_id=cid,
                lane="lane2_triage",
                status="triaged",
                score=1.0,
                verdict="SURVIVOR",
                rationale="Passed all deterministic gatekeeper dealbreaker checks.",
                profile_revision_id=profile_revision_id,
            )
            passed += 1
        triaged += 1

    return {
        "status": "success",
        "total_triaged": triaged,
        "survivors": passed,
        "disqualified": dropped,
    }

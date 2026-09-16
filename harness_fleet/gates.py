"""Shrink a world of possibilities: gates that each earn the right to the next.

The job is elimination, not retrieval. A run starts with far more candidates
than it can afford to read, and most of them are wrong for reasons that cost
almost nothing to check — the wrong kind of company, the wrong size, the wrong
country. So the funnel runs cheapest-first and stops at the first failure:

    ICP/type -> size -> location -> vertical -> semantics

Two rules make it honest rather than merely fast:

* **A snippet can eliminate; only a fetched page can qualify.** A search result
  is somebody else's summary, good enough to rule a company out and never good
  enough to rule one in. So a disqualifier firing on a snippet ends the
  candidate, while a qualifier seen only in a snippet leaves the gate
  ``unknown`` and the candidate alive — it has earned a page fetch, not a pass.
* **Unknown is not a pass and not a failure.** A gate we cannot resolve from
  what we have already read is recorded as unknown, with what would resolve it.
  Dropping it would be a silent zero; passing it would invent evidence.

The outcome per candidate is therefore one of: qualified (passed everything it
could be resolved on), lead (unresolved on something expensive, gap named), or
eliminated (a gate failed, with the reason).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["pass", "fail", "unknown"]

#: Evidence grades, cheapest first. A gate resolved from a snippet may only
#: eliminate; resolving one *for* a candidate requires a page we actually read.
SNIPPET = "snippet"
FETCHED = "fetched"

#: Firmographic vocabulary. The words an SI uses about itself, and the words a
#: product company uses — the first distinction the funnel has to make, because
#: a software company is never a services partner.
SERVICES_TERMS = (
    "consultancy", "consulting", "systems integrator", "system integrator",
    "implementation partner", "professional services", "managed services",
    "advisory", "we build", "we implement", "our clients", "our customers",
    "staff augmentation", "solution provider", "services firm",
)
SOFTWARE_TERMS = (
    "our platform", "our product", "book a demo", "request a demo", "free trial",
    "pricing plans", "per seat", "saas", "sign up free", "start your free trial",
    "our software", "download the app",
)

#: Headcount written the way companies write it.
_SIZE_PATTERNS = (
    re.compile(r"(?P<n>\d[\d,]{0,6})\+?\s*(?:employees|people|staff|consultants|engineers|professionals|experts)\b", re.I),
    re.compile(r"\bteam of\s*(?P<n>\d[\d,]{0,6})\b", re.I),
    re.compile(r"\b(?P<n>\d[\d,]{0,6})\+?\s*(?:strong|person)\b", re.I),
)
_LOCATION_RE = re.compile(
    r"\b(?:headquartered|based|offices?)\s+in\s+([A-Z][A-Za-z.\- ]{2,40}?)(?:[,.;)]|\swith\b|\sand\b)",
    re.I,
)


@dataclass
class Firmographics:
    """What a page or a snippet says about a company's shape."""

    kind: str = ""          # "services" | "software" | "" when unread
    size: int | None = None
    location: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "size": self.size, "location": self.location}


def read_size(text: str) -> int | None:
    """The headcount a text states, or None. Never inferred from a company name."""
    for pattern in _SIZE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            try:
                return int(match.group("n").replace(",", ""))
            except ValueError:
                continue
    return None


def read_location(text: str) -> str:
    """The place a text says the company is, or ""."""
    match = _LOCATION_RE.search(text or "")
    return match.group(1).strip().rstrip(",") if match else ""


def read_kind(text: str) -> str:
    """Whether a text reads as a delivery firm, a product company, or neither.

    Delivery wins a tie: a consultancy that also sells a small product is still
    a services firm, which is the population a partner lane wants.
    """
    lowered = (text or "").lower()
    services = any(term in lowered for term in SERVICES_TERMS)
    software = any(term in lowered for term in SOFTWARE_TERMS)
    if services:
        return "services"
    if software:
        return "software"
    return ""


def read_firmographics(text: str) -> Firmographics:
    return Firmographics(
        kind=read_kind(text), size=read_size(text), location=read_location(text),
    )


@dataclass
class GateResult:
    """One gate's verdict, and what it was entitled to see."""

    gate: str
    outcome: Outcome
    reason: str
    evidence: str = SNIPPET

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate, "outcome": self.outcome,
            "reason": self.reason, "evidence": self.evidence,
        }


@dataclass
class FunnelReport:
    """What the funnel decided for one candidate, gate by gate."""

    candidate: str
    results: list[GateResult] = field(default_factory=list)
    fetched: bool = False

    @property
    def eliminated(self) -> bool:
        return any(result.outcome == "fail" for result in self.results)

    @property
    def qualified(self) -> bool:
        """Every gate passed. A gate that could only be resolved by fetching
        leaves the candidate a lead, not a pass."""
        return bool(self.results) and all(result.outcome == "pass" for result in self.results)

    @property
    def unresolved(self) -> list[str]:
        return [result.gate for result in self.results if result.outcome == "unknown"]

    @property
    def verdict(self) -> str:
        if self.eliminated:
            return "eliminated"
        if self.qualified:
            return "qualified"
        return "lead"

    def because(self) -> str:
        for result in self.results:
            if result.outcome == "fail":
                return f"{result.gate}: {result.reason}"
        if self.unresolved:
            return "unresolved on " + ", ".join(self.unresolved)
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "verdict": self.verdict,
            "because": self.because(),
            "fetched": self.fetched,
            "gates": [result.as_dict() for result in self.results],
        }


def _gate(name: str, verdict: Outcome, reason: str, evidence: str) -> GateResult:
    return GateResult(gate=name, outcome=verdict, reason=reason, evidence=evidence)


def check_kind(text: str, *, allows: str, evidence: str) -> GateResult:
    """The ICP gate: is this the kind of company the profile asks for."""
    kind = read_kind(text)
    if kind == "software" and allows == "services":
        return _gate("kind", "fail", "reads as a product company, not a delivery firm", evidence)
    if kind == "services" and allows == "services":
        if evidence == SNIPPET:
            return _gate(
                "kind", "unknown",
                "snippet reads as a delivery firm; only a page can confirm it", evidence,
            )
        return _gate("kind", "pass", "reads as a delivery firm", evidence)
    return _gate("kind", "unknown", "nothing here says what kind of company it is", evidence)


def check_size(size: int | None, *, minimum: int = 0, maximum: int = 0) -> GateResult:
    """Can they buy or partner at this scale. A wrong size is decisive."""
    if size is None:
        return _gate("size", "unknown", "no headcount stated anywhere we have read", SNIPPET)
    if minimum and size < minimum:
        return _gate("size", "fail", f"{size} people is below the profile's floor of {minimum}", SNIPPET)
    if maximum and size > maximum:
        return _gate("size", "fail", f"{size} people is above the profile's ceiling of {maximum}", SNIPPET)
    return _gate("size", "pass", f"{size} people is within the profile's range", SNIPPET)


def check_location(location: str, *, allowed: tuple[str, ...]) -> GateResult:
    """In territory, or out. An empty location is unknown, never a pass."""
    if not allowed:
        return _gate("location", "unknown", "the profile names no territory", SNIPPET)
    if not location:
        return _gate("location", "unknown", "no location stated anywhere we have read", SNIPPET)
    lowered = location.lower()
    if any(place.lower() in lowered for place in allowed):
        return _gate("location", "pass", f"in territory ({location})", SNIPPET)
    return _gate("location", "fail", f"{location} is outside the profile's territory", SNIPPET)


def check_vertical(verticals: tuple[str, ...], *, wanted: tuple[str, ...]) -> GateResult:
    """The expensive gate: verticals usually only appear in the case studies."""
    if not wanted:
        return _gate("vertical", "unknown", "the profile names no vertical", FETCHED)
    if not verticals:
        return _gate(
            "vertical", "unknown",
            "verticals are not visible yet; they live in the case studies", FETCHED,
        )
    matched = [v for v in verticals if any(w.lower() in v.lower() for w in wanted)]
    if matched:
        return _gate("vertical", "pass", f"serves {', '.join(matched)}", FETCHED)
    return _gate("vertical", "fail", "serves none of the profile's verticals", FETCHED)


def run_funnel(
    candidate: str,
    *,
    snippet: str = "",
    profile: dict[str, Any] | None = None,
) -> FunnelReport:
    """Gate one candidate on what a search result alone can tell us.

    The cheap rungs only. Evaluating the semantic gates needs pages this does
    not fetch: a report that comes back a lead has earned those fetches, and one
    that comes back eliminated never spends them.
    """
    prof = profile or {}
    report = FunnelReport(candidate=candidate)
    report.results.append(
        check_kind(snippet, allows=str(prof.get("allows") or "services"), evidence=SNIPPET)
    )
    if report.eliminated:
        return report
    report.results.append(
        check_size(
            read_size(snippet),
            minimum=int(prof.get("size_min") or 0),
            maximum=int(prof.get("size_max") or 0),
        )
    )
    if report.eliminated:
        return report
    report.results.append(
        check_location(
            read_location(snippet), allowed=tuple(prof.get("locations") or ()),
        )
    )
    return report


def funnel_counts(reports: list[FunnelReport]) -> dict[str, Any]:
    """What the funnel did to a list: the number a person tunes thresholds by.

    Reports eliminated-at per gate, unresolved-at per gate, and how many
    survived to need real retrieval.
    """
    eliminated_at: dict[str, int] = {}
    unresolved_at: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    for report in reports:
        verdicts[report.verdict] = verdicts.get(report.verdict, 0) + 1
        for result in report.results:
            if result.outcome == "fail":
                eliminated_at[result.gate] = eliminated_at.get(result.gate, 0) + 1
            elif result.outcome == "unknown" and not report.eliminated:
                # Only the candidates still standing. An eliminated candidate
                # carries unknowns from the gates it cleared on the way to the
                # gate that killed it, and counting those tells a reader that
                # survivors are stuck when they are not.
                unresolved_at[result.gate] = unresolved_at.get(result.gate, 0) + 1
        if not report.eliminated:
            verdicts["needing_retrieval"] = verdicts.get("needing_retrieval", 0) + 1
    return {
        "candidates": len(reports),
        "verdicts": verdicts,
        "eliminated_at": eliminated_at,
        "unresolved_at": unresolved_at,
    }

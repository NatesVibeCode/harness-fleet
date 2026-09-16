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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["pass", "fail", "unknown"]

#: Evidence grades, cheapest first. A gate resolved from a snippet may only
#: eliminate; resolving one *for* a candidate requires a page we actually read.
SNIPPET = "snippet"
FETCHED = "fetched"

#: The gates that exist, in the order a lane runs them. A ladder rung naming a
#: gate outside this tuple is a lane that lies, and is rejected when it loads.
GATE_ORDER = ("kind", "size", "location", "vertical")

#: The semantic gates need prose only their own site or its case studies carry,
#: so no search-result text may settle them.
_SEMANTIC_GATES = frozenset({"vertical"})

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


#: Terms are matched by their variants, because companies do not use your
#: words: a firm calls itself an "advisory" while the profile says
#: "consultancy", it says "banking" while the profile says "fintech", and it
#: says "London" while the profile says "United Kingdom". Matching literally
#: makes a gate fire on wording instead of on substance, which is how a real
#: consultancy reads as "nothing here says what kind of company it is".
#:
#: This is data, and it is the layer to edit when a gate misjudges a candidate.
SYNONYM_GROUPS: dict[str, tuple[str, ...]] = {
    # what kind of company it is
    "consultancy": ("consultancy", "consulting", "advisory", "advisers", "advisors", "consultants"),
    "systems integrator": (
        "systems integrator", "system integrator", "integration partner", "si partner",
        "solution integrator",
    ),
    "implementation": (
        "implementation partner", "implementation services", "implementing",
        "we implement", "deployment partner", "delivery partner",
    ),
    "managed services": ("managed services", "managed service provider", "msp", "outsourcing"),
    "professional services": (
        "professional services", "services firm", "services company", "services partner",
        "delivery firm", "engineering services",
    ),
    # the industries a firm serves
    "fintech": (
        "fintech", "financial services", "financial technology", "banking", "banks",
        "payments", "capital markets", "insurance", "asset management", "lending",
    ),
    "healthcare": (
        "healthcare", "health care", "life sciences", "medical", "providers",
        "payers", "pharma", "clinical", "biotech",
    ),
    "retail": ("retail", "ecommerce", "e-commerce", "consumer goods", "cpg", "merchandising"),
    "logistics": ("logistics", "supply chain", "freight", "shipping", "fulfilment", "fulfillment"),
    # where it operates: a country is written as its cities and abbreviations
    "united states": (
        "united states", "usa", "u.s.", "us-based", "america", "new york", "san francisco",
        "chicago", "austin", "boston", "seattle", "denver", "atlanta", "dallas", "houston",
        "los angeles", "philadelphia", "phoenix", "minneapolis", "charlotte", "nashville",
    ),
    "united kingdom": (
        "united kingdom", "uk", "u.k.", "britain", "england", "scotland", "wales",
        "london", "manchester", "leeds", "bristol", "edinburgh", "glasgow", "birmingham",
    ),
    "canada": ("canada", "toronto", "vancouver", "montreal", "ottawa", "ontario", "quebec", "calgary"),
    "ireland": ("ireland", "dublin", "cork", "galway"),
    "australia": ("australia", "sydney", "melbourne", "brisbane", "perth", "adelaide"),
    "germany": ("germany", "deutschland", "berlin", "munich", "munchen", "frankfurt", "hamburg", "cologne"),
    "netherlands": ("netherlands", "amsterdam", "rotterdam", "utrecht", "the hague"),
    "india": ("india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai", "delhi", "noida"),
}


def variants(term: str) -> tuple[str, ...]:
    """Every way a term gets written, the term itself first."""
    key = str(term or "").strip().lower()
    if not key:
        return ()
    for canonical, group in SYNONYM_GROUPS.items():
        if key == canonical or key in group:
            return tuple(dict.fromkeys((key, canonical, *group)))
    return (key,)


def matches_any(text: str, terms: Any) -> list[str]:
    """Which of these terms the text says, synonyms included.

    Returns the reader's own term, not the word that happened to match, so a
    report says "serves fintech" whether the page said fintech or banking.
    """
    lowered = (text or "").lower()
    found: list[str] = []
    for term in terms or ():
        if any(variant in lowered for variant in variants(str(term))):
            found.append(str(term))
    return found


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


#: The synonym groups that name somewhere a company can be.
PLACE_GROUPS = (
    "united states", "united kingdom", "canada", "ireland", "australia",
    "germany", "netherlands", "india",
)


def read_location(text: str) -> str:
    """The place a text says the company is, or "".

    An explicit "based in X" first, because that is the company telling us.
    Failing that, a place it names: a snippet reading "200 people in London"
    states its location without ever using the word, and requiring the formal
    phrasing made a resolvable gate read as unknown.
    """
    match = _LOCATION_RE.search(text or "")
    if match:
        return match.group(1).strip().rstrip(",")
    # Otherwise report the words the text actually used: "London" is what the
    # page said, and the gate decides it is the United Kingdom. Quoting the
    # page lets a reader see why a gate fired instead of taking its word.
    lowered = (text or "").lower()
    for place in PLACE_GROUPS:
        for variant in variants(place):
            if variant in lowered:
                return variant.title()
    return ""


#: The synonym groups that name an industry a firm can serve. Kept apart from
#: PLACE_GROUPS because the vertical gate must not read "chatting in San
#: Francisco" as "serves fintech".
VERTICAL_GROUPS = ("fintech", "healthcare", "retail", "logistics")


def read_verticals(text: str) -> tuple[str, ...]:
    """Which industries a text names. Empty when it names none we know.

    The absence of a vertical is not the same as the absence of *evidence*
    about verticals: a page that names no industry we recognise leaves the gate
    unresolved, because the case studies may simply not be in this text.
    """
    found = []
    for group in VERTICAL_GROUPS:
        if any(variant in (text or "").lower() for variant in variants(group)):
            found.append(group)
    return tuple(found)


def read_kind(text: str) -> str:
    """Whether a text reads as a delivery firm, a product company, or neither.

    Delivery wins a tie: a consultancy that also sells a small product is still
    a services firm, which is the population a partner lane wants.
    """
    services = bool(matches_any(text, SERVICES_TERMS))
    software = bool(matches_any(text, SOFTWARE_TERMS))
    if services:
        return "services"
    if software:
        return "software"
    return ""


def read_firmographics(text: str) -> Firmographics:
    return Firmographics(
        kind=read_kind(text), size=read_size(text), location=read_location(text),
    )


def read_grade(metadata: Any) -> str:
    """The evidence grade a source carries, in this funnel's two-value alphabet.

    Discovery publishes three grades (``fetched``, ``profile``, ``indicator``),
    and the distinction that decides what a text may settle is a binary one: was
    this a page we actually read, or somebody's summary of one? ``profile`` is
    structured directory text — real, but not a page read — so it grades as a
    snippet here. Anything unrecognised is the weakest grade, because assuming
    the strongest is how a summary gets to qualify a candidate.
    """
    if not isinstance(metadata, dict):
        return SNIPPET
    return FETCHED if str(metadata.get("evidence") or "").strip().lower() == FETCHED else SNIPPET


@dataclass(frozen=True)
class GateProfile:
    """The funnel's view of a profile, however that profile was authored.

    The engine stays lane-agnostic: it is handed an object or a plain dict and
    reads the four fields the cheap gates need. A profile type with firmographic
    fields adapts itself through ``funnel_profile()``; everything else already
    speaks this shape.
    """

    allows: str = "services"
    size_min: int = 0
    size_max: int = 0
    locations: tuple[str, ...] = ()
    verticals: tuple[str, ...] = ()

    @classmethod
    def from_object(cls, profile: Any) -> GateProfile:
        if profile is None:
            return cls()
        if isinstance(profile, GateProfile):
            return profile
        # A typed profile states the firmographics in its own vocabulary — the
        # partner document says "target_territories", the funnel says
        # "locations" — so ask it to translate rather than guessing its fields.
        adapt = getattr(profile, "funnel_profile", None)
        if callable(adapt):
            try:
                adapted = adapt()
            except Exception:
                adapted = None
            if isinstance(adapted, dict):
                return cls.from_object(adapted)

        def get(key: str) -> Any:
            return profile.get(key) if isinstance(profile, dict) else getattr(profile, key, None)

        def terms(key: str) -> tuple[str, ...]:
            value = get(key)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple, set, frozenset)):
                return ()
            return tuple(str(item) for item in value if str(item).strip())

        def count(key: str) -> int:
            try:
                return int(get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            allows=str(get("allows") or "services"),
            size_min=count("size_min"),
            size_max=count("size_max"),
            locations=terms("locations"),
            verticals=terms("verticals"),
        )

    def intersect(self, other: GateProfile | None) -> GateProfile:
        """Both profiles' gates, with the tighter bound of each.

        A lane and a profile can each state firmographics, and a candidate has
        to satisfy both: the lane says what the product accepts, the profile
        says what this engagement needs. Neither silently overrides the other —
        a lane's size floor does not erase a stricter one from the profile, and
        a profile's territory does not widen the lane's.
        """
        if other is None:
            return self
        floors = [n for n in (self.size_min, other.size_min) if n]
        ceilings = [n for n in (self.size_max, other.size_max) if n]
        return GateProfile(
            allows=self.allows if self.allows != "any" else other.allows,
            size_min=max(floors) if floors else 0,
            size_max=min(ceilings) if ceilings else 0,
            locations=_narrow(self.locations, other.locations),
            verticals=_narrow(self.verticals, other.verticals),
        )


def _narrow(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    """Two term lists, or whichever one is stated, or neither."""
    if not left:
        return right
    if not right:
        return left
    shared = tuple(term for term in left if matches_any(term, right))
    return shared or left


@dataclass(frozen=True)
class LadderRung:
    """One rung of a lane's retrieval ladder.

    A rung names the gates it is entitled to settle and the grade of evidence it
    reads to settle them: a search result settles the cheap gates on what it
    already says, and a rung marked ``fetched`` spends a page visit on the same
    gates so they can be *passed* rather than merely left unknown. The surfaces
    are what that rung reads on the candidate's own site.
    """

    name: str
    evidence: str = SNIPPET
    gates: list[str] = field(default_factory=lambda: ["kind", "size", "location"])
    surfaces: list[str] = field(default_factory=list)
    #: What this rung buys, in the lane's own words. A reader tunes by it.
    earns: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "evidence": self.evidence, "gates": list(self.gates),
            "surfaces": list(self.surfaces), "earns": self.earns,
        }

    def reachable(self, grade: str) -> bool:
        """Whether evidence at this grade can climb this rung.

        Two ways to be out of reach, and both are real. A rung marked
        ``fetched`` needs a page nobody has read yet: the cheap gates *can* be
        settled from a search result — that is what the rung below does — but
        counting this one as climbed would credit a fetch the run never spent.
        And a semantic gate needs prose only the firm's own case studies carry,
        whatever the rung's declared grade.
        """
        if self.evidence == FETCHED and grade != FETCHED:
            return False
        if any(gate in _SEMANTIC_GATES for gate in self.gates):
            return grade == FETCHED
        return True


#: The ladder that applies when a lane declares none: prove the cheap gates on
#: their own pages, and leave the semantic gate to a later rung.
DEFAULT_LADDER: tuple[LadderRung, ...] = (
    LadderRung(
        name="result", evidence=SNIPPET, gates=["kind", "size", "location"],
        earns="eliminates, or earns one page visit",
    ),
    LadderRung(
        name="surface", evidence=FETCHED, gates=["kind", "size", "location"],
        surfaces=["home", "about", "services"],
        earns="qualifies the cheap gates where a snippet could not",
    ),
    LadderRung(
        name="stories", evidence=FETCHED, gates=["vertical"],
        surfaces=["case_studies", "customers", "industries"],
        earns="settles the vertical gate, the expensive one",
    ),
)


def validate_ladder(ladder: Sequence[LadderRung]) -> None:
    """Everything wrong with a lane's ladder, naming the rung and the gate."""
    for index, rung in enumerate(ladder or ()):
        where = f"ladder[{index}]"
        if not str(rung.name or "").strip():
            raise ValueError(f"{where}: every rung has a name")
        if rung.evidence not in (SNIPPET, FETCHED):
            raise ValueError(f"{where}: evidence must be '{SNIPPET}' or '{FETCHED}'")
        if not rung.gates:
            raise ValueError(f"{where}: a rung that settles no gate is not a rung")
        for gate in rung.gates:
            if gate not in GATE_ORDER:
                raise ValueError(f"{where}: '{gate}' is not a gate (have: {', '.join(GATE_ORDER)})")


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
    #: The ladder this candidate was run against, and the rung it has earned.
    #: A lead that says only "unresolved" leaves a reader nowhere to go; one
    #: that says "earned: surface" says what to fetch next.
    rungs: list[LadderRung] = field(default_factory=list)
    earned: str = ""

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

    @property
    def exhausted(self) -> bool:
        """A lead with nothing further to fetch.

        Everything still unresolved needs evidence this report's own grade
        cannot supply and no rung above it would: the pages are already read, so
        fetching them again changes nothing. It is a distinct state from a lead
        that has earned a fetch, and the difference is what stops a run from
        reporting a next step that does not exist.
        """
        return self.verdict == "lead" and not self.earned

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
            "earned": self.earned,
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


def check_size(size: int | None, *, minimum: int = 0, maximum: int = 0,
               evidence: str = SNIPPET) -> GateResult:
    """Can they buy or partner at this scale. A wrong size is decisive.

    A stated headcount is decisive at either grade: the number a page prints is
    the same number a snippet quotes, so a wrong size eliminates without a fetch
    — which is the whole point of running this rung first.
    """
    if size is None:
        return _gate("size", "unknown", "no headcount stated anywhere we have read", evidence)
    if minimum and size < minimum:
        return _gate("size", "fail", f"{size} people is below the profile's floor of {minimum}", evidence)
    if maximum and size > maximum:
        return _gate("size", "fail", f"{size} people is above the profile's ceiling of {maximum}", evidence)
    # A stated headcount means the same thing at either grade, so a size inside
    # the range passes on a snippet. The absolute asymmetry — a snippet may
    # eliminate and never qualify — is enforced where it bites, on ``kind``.
    return _gate("size", "pass", f"{size} people is within the profile's range", evidence)


def _range_phrase(minimum: int, maximum: int) -> str:
    if minimum and maximum:
        return f"the profile's {minimum}-{maximum}"
    if minimum:
        return f"the profile's floor of {minimum}"
    if maximum:
        return f"the profile's ceiling of {maximum}"
    return "the profile's range"


def check_location(location: str, *, allowed: tuple[str, ...],
                   evidence: str = SNIPPET) -> GateResult:
    """In territory, or out. An empty location is unknown, never a pass."""
    if not allowed:
        return _gate("location", "unknown", "the profile names no territory", evidence)
    if not location:
        return _gate("location", "unknown", "no location stated anywhere we have read", evidence)
    if matches_any(location, allowed):
        return _gate("location", "pass", f"in territory ({location})", evidence)
    # Naming a place is what makes this decisive: the text itself said where the
    # company is, and it said somewhere else. Silence is what stays unknown.
    return _gate("location", "fail", f"{location} is outside the profile's territory", evidence)


def check_vertical(verticals: tuple[str, ...], *, wanted: tuple[str, ...]) -> GateResult:
    """The expensive gate: verticals usually only appear in the case studies."""
    if not wanted:
        return _gate("vertical", "unknown", "the profile names no vertical", FETCHED)
    if not verticals:
        return _gate(
            "vertical", "unknown",
            "verticals are not visible yet; they live in the case studies", FETCHED,
        )
    joined = ", ".join(verticals)
    matched = matches_any(joined, wanted)
    if matched:
        return _gate("vertical", "pass", f"serves {', '.join(matched)}", FETCHED)
    # The page names industries and none of them is ours. Before calling that a
    # failure, ask whether it named an industry we know at all: a case-study
    # index we did not reach reads exactly like a firm with no vertical, and
    # eliminating those quietly discards the population the lane exists for.
    known = read_verticals(joined)
    if not known:
        return _gate(
            "vertical", "unknown",
            "no vertical is named anywhere we have read; the case studies would say",
            FETCHED,
        )
    return _gate(
        "vertical", "fail",
        f"serves {', '.join(sorted(known))} and none of the profile's verticals", FETCHED,
    )


def _evaluable_gates(prof: GateProfile) -> frozenset[str]:
    """Which gates this profile actually puts to a candidate.

    ``kind`` always gates. The rest gate only when the profile states something
    to gate on — no territory named means the location gate never runs — or, for
    the semantic gate, when the profile names an industry to match. This is the
    single answer both the ladder walk and the lane validator ask for, so a rung
    that gates on something this profile never evaluates is idle on both sides
    rather than live on one and idle on the other. Callers merge the lane's
    defaults with the run's profile first, so a field left unset in a lane file
    is still gated on once a person supplies it.
    """
    gates = {"kind"}
    if prof.size_min or prof.size_max:
        gates.add("size")
    if prof.locations:
        gates.add("location")
    if prof.verticals:
        gates.add("vertical")
    return frozenset(gates)


def run_funnel(
    candidate: str,
    *,
    snippet: str = "",
    profile: Any = None,
    evidence: str = SNIPPET,
    ladder: Sequence[LadderRung] | None = None,
) -> FunnelReport:
    """Gate one candidate on the text we already have, and say what it earned.

    The cheap rungs only, unless ``evidence`` says the text came from a page we
    read — then the semantic gate runs too. A report that comes back a lead has
    earned its next page visit, and one that comes back eliminated never spends
    one.

    ``snippet`` is the text, whatever its grade; the parameter keeps its name
    because most callers are holding a search result. ``evidence`` is what the
    caller knows about where that text came from, and it is the caller's to
    state: the same prose earns a different verdict on a page than in a result.
    """
    prof = GateProfile.from_object(profile)
    report = FunnelReport(candidate=candidate, fetched=bool(evidence == FETCHED))
    report.results.append(check_kind(snippet, allows=prof.allows, evidence=evidence))
    if report.eliminated:
        return report
    # A gate the profile leaves unset is not put to the candidate at all: no
    # size bounds means nothing to be outside of, and no territory named means
    # nowhere to be outside of. Such a gate would come back `unknown` forever
    # without standing between the candidate and a pass, and counting it as
    # unresolved would name a blocker no page could ever clear.
    if prof.size_min or prof.size_max:
        report.results.append(
            check_size(read_size(snippet), minimum=prof.size_min, maximum=prof.size_max)
        )
        if report.eliminated:
            return report
    if prof.locations:
        report.results.append(check_location(read_location(snippet), allowed=prof.locations))
        if report.eliminated:
            return report
    if prof.verticals:
        # The vertical is gated on even when only a snippet is in hand, and it
        # comes back unknown there. That is the point: leaving it out entirely
        # made a candidate look blocked on the gate a fetch cannot settle, when
        # what actually resolves it is the case studies this hasn't read yet.
        report.results.append(
            check_vertical(
                read_verticals(snippet) if evidence == FETCHED else (),
                wanted=prof.verticals,
            )
        )
    _set_earned(report, ladder, prof)
    return report


def _set_earned(
    report: FunnelReport,
    ladder: Sequence[LadderRung] | None,
    profile: GateProfile | None = None,
) -> None:
    """Name the rung a surviving candidate has earned, and record the ladder.

    The rule is: *earn the first rung carrying something open that this report's
    evidence cannot settle, once everything below it is a grant.* The walk is
    where the two halves of the engine meet — a candidate holding only a search
    result is owed the page, and the same candidate holding the page is owed the
    case studies. That movement is the number a person tunes thresholds by.

    A rung is a *grant* when the evidence in hand answers it: either it is
    already settled, or it carries a gate this profile still puts to candidates
    and this grade can read. Grants are walked through, never reported, because
    there is no fetch left to spend on them.

    What remains is a rung carrying something open that a fetch would settle, and
    that is what gets named. When no such rung exists, the lead has read
    everything that could change the answer and ``earned`` stays empty — the
    ``exhausted`` state, which is an answer rather than a failure to find one.
    """
    rungs = list(ladder or DEFAULT_LADDER)
    report.rungs = rungs
    if report.eliminated or report.qualified:
        return
    grade = FETCHED if report.fetched else SNIPPET
    live = _evaluable_gates(profile) if profile is not None else None

    def settleable_here(rung: LadderRung) -> bool:
        """Whether any gate still open at this rung is one we could settle now.

        Three qualifications, each load-bearing. The gate must be *open* — not
        already settled, and not one this profile never puts to a candidate. It
        must be a gate this rung actually carries, or the rung could be credited
        for work it does not do. And the rung's own evidence grade must be one we
        hold: a page answers a rung written for search results, and a search
        result cannot answer one written for pages. That last one is the
        asymmetry in miniature, and it is why a snippet-only candidate is owed a
        fetch while a fetched candidate is not owed a summary.
        """
        if live is not None and not set(rung.gates) & live:
            return False
        open_gates = {gate for gate in rung.gates if not _settled(report, gate)}
        if not open_gates:
            return False
        if live is not None and not open_gates & live:
            return False
        return rung.reachable(grade) and _rank(rung.evidence) <= _rank(grade)

    for index, rung in enumerate(rungs):
        if settleable_here(rung) or all(_settled(report, gate) for gate in rung.gates):
            # A grant: the evidence in hand settles this rung, or already has.
            # No fetch is owed, so keep walking up.
            continue
        if any(settleable_here(other) for other in rungs[index + 1:]):
            # Out of reach on evidence we do not hold, with something further up
            # still open. Behind the candidate, not ahead of it.
            continue
        report.earned = rung.name
        return
    report.earned = ""


def _rank(grade: str) -> int:
    """How much evidence a grade is, so a rung can be ordered against a report."""
    return 1 if grade == FETCHED else 0


def _settled(report: FunnelReport, gate: str) -> bool:    return any(
        result.gate == gate and result.outcome in ("pass", "fail") for result in report.results
    )


def funnel_counts(reports: list[FunnelReport]) -> dict[str, Any]:
    """What the funnel did to a list: the number a person tunes thresholds by.

    Reports eliminated-at per gate, unresolved-at per gate, and how many
    survived to need real retrieval.
    """
    eliminated_at: dict[str, int] = {}
    unresolved_at: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    #: Who, not just how many. A count with no names cannot be tuned against:
    #: a person sees "12 eliminated at kind" and still has to guess whether the
    #: gate is right or the vocabulary is missing a word.
    eliminated: dict[str, list[dict[str, str]]] = {}
    unresolved: dict[str, list[dict[str, str]]] = {}
    for report in reports:
        verdicts[report.verdict] = verdicts.get(report.verdict, 0) + 1
        for result in report.results:
            if result.outcome == "fail":
                eliminated_at[result.gate] = eliminated_at.get(result.gate, 0) + 1
                eliminated.setdefault(result.gate, []).append({
                    "candidate": report.candidate, "reason": result.reason,
                })
            elif result.outcome == "unknown" and not report.eliminated:
                # Only the candidates still standing. An eliminated candidate
                # carries unknowns from the gates it cleared on the way to the
                # gate that killed it, and counting those tells a reader that
                # survivors are stuck when they are not.
                unresolved_at[result.gate] = unresolved_at.get(result.gate, 0) + 1
                unresolved.setdefault(result.gate, []).append({
                    "candidate": report.candidate, "reason": result.reason,
                })
        if not report.eliminated:
            verdicts["needing_retrieval"] = verdicts.get("needing_retrieval", 0) + 1
    return {
        "candidates": len(reports),
        "verdicts": verdicts,
        "eliminated_at": eliminated_at,
        "unresolved_at": unresolved_at,
        "eliminated": eliminated,
        "unresolved": unresolved,
    }


def run_evidence_funnel(
    items: Iterable[Any],
    *,
    profile: Any = None,
    candidate: str = "",
    ladder: Sequence[LadderRung] | None = None,
    snippet: str | None = None,
) -> FunnelReport:
    """Gate one candidate on the sources gathered for it.

    The entity's grade is its best source's grade: a bundle carrying one page we
    actually read is read as a page, because the gates read the bundle's text
    and that text includes the fetch. Every other source stays what it was.
    """
    sources = [item for item in items]
    grade = SNIPPET
    for item in sources:
        if read_grade(getattr(item, "metadata", None)) == FETCHED:
            grade = FETCHED
            break
    text = snippet if snippet is not None else "\n".join(
        str(getattr(item, "text", "") or "") for item in sources
    )
    name = candidate or _candidate_name(sources)
    report = run_funnel(name, snippet=text, profile=profile, evidence=grade, ladder=ladder)
    report.fetched = grade == FETCHED
    return report


def _candidate_name(sources: Sequence[Any]) -> str:
    for item in sources:
        name = str(getattr(item, "item_id", "") or "").strip()
        if name:
            return name
    return ""


def funnel_entities(
    items: Iterable[Any],
    *,
    profile: Any = None,
    ladder: Sequence[LadderRung] | None = None,
) -> tuple[list[FunnelReport], dict[str, Any], list[FunnelReport]]:
    """Run the funnel over sources grouped by the entity they are about.

    Returns the reports for the candidates still standing, the counts, and every
    report including the eliminated. The two views answer different questions: a
    caller that keeps going needs the survivors, and a caller writing the run's
    account of itself needs everyone it ruled out, by name and reason.
    """
    grouped: dict[str, list[Any]] = {}
    for item in items:
        key = str(getattr(item, "item_id", "") or "")
        if key:
            grouped.setdefault(key, []).append(item)
    reports = [
        run_evidence_funnel(sources, profile=profile, candidate=key, ladder=ladder)
        for key, sources in grouped.items()
    ]
    counts = funnel_counts(reports)
    return [report for report in reports if not report.eliminated], counts, reports

"""Ideal Employer Profile (IEP) and candidate fit specifications."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class Dealbreakers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_headcount: int | None = Field(
        default=None,
        ge=1,
        description="Optional maximum company headcount. Leave unset to avoid assuming a size preference."
    )
    require_verified_headcount: bool = Field(
        default=False,
        description=(
            "When true, an unknown or non-numeric headcount disqualifies a company; "
            "leave false when sources such as ATS boards do not publish headcount."
        ),
    )
    policy: Literal["remote_only", "remote_or_hybrid", "any"] = Field(
        default="any",
        description=(
            "Optional workplace policy. 'any' is the neutral default; choose "
            "remote_only or remote_or_hybrid only when the user requests it."
        )
    )
    disallowed_locations: list[str] = Field(
        default_factory=list,
        description="Mandatory in-office locations that trigger disqualification."
    )
    reject_thin_wrappers: bool = Field(
        default=False,
        description="Reject shallow AI wrappers with no proprietary state, wedge, or moat."
    )
    reject_pure_quota: bool = Field(
        default=False,
        description="Reject pure cold outbound bag-carrying or narrow execution silos."
    )
    min_timezone_overlap_hours: float = Field(
        default=4.0,
        ge=0.0,
        le=8.0,
        description="Minimum domestic/regional timezone overlap required for effective sync."
    )
    candidate_timezone: str | None = Field(
        default=None,
        description="Optional IANA timezone for the candidate, used with timezone metadata from a posting."
    )


class IdealEmployerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The store's kind for this document. Without it a saved IEP fell back to
    #: "ideal_company" and filed an employer profile under the account kind,
    #: which is how two different onboardings ended up sharing one active slot.
    profile_kind: ClassVar[str] = "ideal_employer"

    profile_name: str = Field(
        default="My Career Fit Profile",
        description="Descriptive name for the candidate or role target."
    )
    version: str = Field(
        default="1.0.0",
        description="Profile specification version."
    )
    wedge_capabilities: list[str] = Field(
        default_factory=list,
        description="Core high-leverage capabilities the candidate deploys."
    )
    required_stack: list[str] = Field(
        default_factory=list,
        description="Core technical stack or infrastructure required in the target company."
    )
    negative_stack: list[str] = Field(
        default_factory=list,
        description="Technologies indicating legacy bloat or misaligned engineering culture."
    )
    dealbreakers: Dealbreakers = Field(
        default_factory=Dealbreakers,
        description="Hard dealbreakers that disqualify companies immediately."
    )
    hiring_catalysts: list[str] = Field(
        default_factory=list,
        description="Catalyst events that create urgent leadership budget and mandate."
    )
    target_leadership: list[str] = Field(
        default_factory=list,
        description="Leadership traits and counterpart profiles."
    )
    anchor_companies: list[str] = Field(
        default_factory=list,
        description="Exemplar companies retained as context for calibration and external evaluation; not a direct deterministic score."
    )
    #: Firmographics the gates run on. A lane ships no thresholds, so this is
    #: where the operator states theirs; an unset field leaves its gate off
    #: rather than inventing a bound.
    size_min: int = Field(default=0, ge=0, description="Employer headcount floor; 0 leaves it unbounded.")
    size_max: int = Field(default=0, ge=0, description="Employer headcount ceiling; 0 leaves it unbounded.")
    target_territories: list[str] = Field(
        default_factory=list, description="Where the employer must be (e.g. United Kingdom)."
    )
    target_industries: list[str] = Field(
        default_factory=list, description="Industries the employer must be in (e.g. fintech)."
    )
    #: The role family this search is for — "enterprise sales", "sales operations".
    #: The one axis the IEP could not state before, which left the career lane's
    #: seed (the role it is looking for) with nowhere in the onboarding to come
    #: from.
    target_roles: list[str] = Field(
        default_factory=list, description="Role families to search for (e.g. enterprise sales, sales operations)."
    )

    def query_terms(self) -> dict[str, list[str]]:
        """The values this IEP supplies to the lane's query templates.

        The role family leads: a career lane's question is the job it is looking
        for, and everything else — the stack, the industry — narrows that search
        rather than replacing it.

        ``anchor_companies`` is deliberately absent. Exemplars calibrate a
        judgement about a population; they are not members to go and find, and a
        company name as a query returns that company's own site rather than the
        kind of employer this profile describes.
        """
        return {
            "role": list(self.target_roles),
            "tech": list(self.required_stack),
            "vertical": list(self.target_industries),
            "pain": list(self.hiring_catalysts),
        }

    def funnel_profile(self) -> dict[str, Any]:
        """The firmographics the gate engine runs on, in the shape it reads.

        An employer is never asked the partner question — "is this a delivery
        firm?" — because a product company employs people too, so this lane asks
        nothing of the kind. The ceiling falls back to the dealbreaker this
        profile already states, so an author who filled that in gets the gate.
        """
        return {
            "allows": "any",
            "size_min": self.size_min,
            "size_max": self.size_max or self.dealbreakers.max_headcount or 0,
            "locations": tuple(self.target_territories),
            "verticals": tuple(self.target_industries),
        }

    @classmethod
    def load(cls, path: Path | str) -> IdealEmployerProfile:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Profile file not found at: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def save(self, path: Path | str) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def to_triage_criteria(self) -> dict[str, Any]:
        """Produce structured criteria dictionary for Lane 2 Gatekeeper Triage."""
        return {
            "max_headcount": self.dealbreakers.max_headcount,
            "require_verified_headcount": self.dealbreakers.require_verified_headcount,
            "policy": self.dealbreakers.policy,
            "disallowed_locations": self.dealbreakers.disallowed_locations,
            "reject_thin_wrappers": self.dealbreakers.reject_thin_wrappers,
            "reject_pure_quota": self.dealbreakers.reject_pure_quota,
            "min_timezone_overlap_hours": self.dealbreakers.min_timezone_overlap_hours,
            "candidate_timezone": self.dealbreakers.candidate_timezone,
        }

    def to_evaluation_prompt(self) -> str:
        """Render the rubric for an external evaluator or future model integration."""
        caps = "\n".join(f"- {c}" for c in self.wedge_capabilities)
        stack = "\n".join(f"- {s}" for s in self.required_stack)
        negative_stack = "\n".join(f"- {s}" for s in self.negative_stack)
        catalysts = "\n".join(f"- {c}" for c in self.hiring_catalysts)
        leaders = "\n".join(f"- {c}" for c in self.target_leadership)
        anchors = "\n".join(f"- {c}" for c in self.anchor_companies)
        dealbreakers = json.dumps(self.to_triage_criteria(), indent=2)
        return (
            f"EVALUATION CRITERIA: {self.profile_name} (v{self.version})\n\n"
            f"1. CORE WEDGE CAPABILITIES:\n{caps}\n\n"
            f"2. REQUIRED STACK (every listed requirement must be evidenced):\n{stack}\n\n"
            f"3. NEGATIVE STACK SIGNALS:\n{negative_stack}\n\n"
            f"4. HIRING CATALYSTS (URGENT PAIN):\n{catalysts}\n\n"
            f"5. LEADERSHIP & CULTURE REQUIREMENTS:\n{leaders}\n\n"
            f"6. ANCHOR EXEMPLARS:\n{anchors}\n\n"
            f"7. HARD DEALBREAKERS:\n{dealbreakers}\n\n"
            f"EVIDENCE REQUIREMENT: Every positive claim MUST reference exact verbatim quotes "
            f"from captured source text. Never invent budget, traction, or role suitability."
        )

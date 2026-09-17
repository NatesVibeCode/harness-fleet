"""The typed profile contracts the shared engine onboards.

One document per lane family: the Ideal Company Profile an account run
searches for, and the Ideal Employer Profile a career run does. The partner
contract lives beside them in ``partner.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class IdealCompanyProfile(BaseModel):
    """The durable ICP contract for account research.

    The JSON file is the human-editable authoring form. The SQLite store keeps
    immutable revisions so a run can be traced back to the exact ICP that was
    selected when it started.
    """

    model_config = ConfigDict(extra="forbid")

    profile_kind: ClassVar[str] = "ideal_company"
    #: The filename this document is authored in, declared by the contract so
    #: the engine never keeps a kind-to-filename map of its own.
    authoring_file: ClassVar[str] = "ideal_company_profile.json"

    profile_name: str = Field(default="My Ideal Company Profile")
    version: str = Field(default="1.0.0")
    product_category: str = Field(default="")
    architectural_layer: str = Field(default="")
    required_stack: list[str] = Field(default_factory=list)
    negative_stack_exclusions: list[str] = Field(default_factory=list)
    trigger_pain_phrases: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    anchor_logos: list[str] = Field(default_factory=list)
    #: Firmographics the gates run on. Nothing is hard-coded per lane — a lane
    #: ships no size range, no territory and no vertical list — so this is where
    #: an operator states theirs. An unset field leaves its gate off rather than
    #: inventing a bound, which is the honest reading and names the field to add.
    size_min: int = Field(default=0, ge=0, description="Headcount floor; 0 leaves it unbounded (e.g. 50)")
    size_max: int = Field(default=0, ge=0, description="Headcount ceiling; 0 leaves it unbounded (e.g. 5000)")
    target_territories: list[str] = Field(
        default_factory=list, description="Where the company must be (e.g. United Kingdom, United States)"
    )
    target_industries: list[str] = Field(
        default_factory=list, description="Industries the company must be in (e.g. fintech, healthcare)"
    )
    calibrated_scoring_rubric: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_interview_document(cls, value: Any) -> Any:
        """Flatten the nested shape emitted by the ICP interview guide."""
        if not isinstance(value, dict) or not isinstance(value.get("icp_profile"), dict):
            return value
        flattened = dict(value["icp_profile"])
        for key in ("profile_name", "version", "calibrated_scoring_rubric"):
            if key in value:
                flattened[key] = value[key]
        return flattened

    @classmethod
    def load(cls, path: Path | str) -> IdealCompanyProfile:
        profile_path = Path(path).expanduser()
        if not profile_path.exists():
            raise FileNotFoundError(f"Ideal Company Profile not found at: {profile_path}")
        return cls.model_validate(json.loads(profile_path.read_text(encoding="utf-8")))

    def save(self, path: Path | str) -> None:
        profile_path = Path(path).expanduser()
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def funnel_profile(self) -> dict[str, Any]:
        """The firmographics the gate engine runs on, as far as this profile has them.

        An ICP authored before the funnel existed carries no size range and no
        territory, so it gates on what it does say. That is not a gap to paper
        over with invented bounds: a profile that names no territory leaves the
        location gate unknown, which is the honest reading and tells the author
        exactly which field to add.

        The rubric is free-form JSON, so these are read defensively: a size that
        is not a number, or a territory that is not a list, is treated as unset
        rather than crashing a run before it starts.
        """
        rubric = self.calibrated_scoring_rubric

        def whole(key: str) -> int:
            try:
                return int(str(rubric.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        def terms(key: str) -> tuple[str, ...]:
            value = rubric.get(key)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return ()
            return tuple(str(item) for item in value if str(item).strip())

        return {
            # A target account is a *buyer*. It may be a product company, so the
            # ICP never puts the partner question ("is this a delivery firm?") to
            # a candidate: that test belongs to the partner profile, and asking
            # it here eliminated firms this lane exists to find.
            "allows": "any",
            # Typed fields first, the rubric behind them so a profile authored
            # before these fields existed still gates on what it does say.
            "size_min": self.size_min or whole("size_min"),
            "size_max": self.size_max or whole("size_max"),
            "locations": tuple(self.target_territories) or terms("locations"),
            "verticals": tuple(self.target_industries) or terms("verticals"),
        }

    def query_terms(self) -> dict[str, list[str]]:
        """The values this ICP supplies to the lane's query templates.

        The onboarding's contribution. A lane's templates name axes —
        ``{tech}``, ``{vertical}``, ``{role}``, ``{pain}`` — and this is where
        the operator's own answers land, so discovery searches what the ICP says
        rather than the lane's illustrative literals.

        ``anchor_logos`` is deliberately absent. An anchor is a calibration
        exemplar, and a company name as a query returns that one company's own
        pages instead of the class of firm the ICP describes. Anchors belong in
        the scoring prompt, and in an exclusion list where one exists.
        """
        return {
            "tech": list(self.required_stack),
            "vertical": list(self.target_industries),
            "role": list(self.target_roles),
            "pain": list(self.trigger_pain_phrases),
        }

    def to_prompt_context(self) -> str:
        """Render explicit ICP context for an account task when requested."""
        return (
            f"IDEAL COMPANY PROFILE: {self.profile_name} (v{self.version})\n"
            f"Product category: {self.product_category or 'Not specified'}\n"
            f"Architectural layer: {self.architectural_layer or 'Not specified'}\n"
            f"Required stack: {', '.join(self.required_stack) or 'Not specified'}\n"
            f"Negative stack exclusions: {', '.join(self.negative_stack_exclusions) or 'None specified'}\n"
            f"Trigger/pain phrases: {', '.join(self.trigger_pain_phrases) or 'Not specified'}\n"
            f"Target roles: {', '.join(self.target_roles) or 'Not specified'}\n"
            f"Anchor logos: {', '.join(self.anchor_logos) or 'None specified'}\n"
            f"Scoring rubric: {json.dumps(self.calibrated_scoring_rubric, ensure_ascii=False, sort_keys=True)}"
        )


class IdealEmployerProfile(BaseModel):
    """The durable IEP contract for career research.

    This is the `career` lane's onboarding document. It was the one live piece of
    the retired ``career_fleet`` distribution: the lane declares
    ``onboarding_profile: ideal_employer`` and the registry resolves this class,
    so the four company-screening lanes and their own store could go while this
    stayed.
    """

    model_config = ConfigDict(extra="forbid")

    #: The store's kind for this document. Without it a saved IEP fell back to
    #: "ideal_company" and filed an employer profile under the account kind,
    #: which is how two different onboardings ended up sharing one active slot.
    profile_kind: ClassVar[str] = "ideal_employer"
    authoring_file: ClassVar[str] = "profile.json"

    profile_name: str = Field(
        default="My Career Fit Profile",
        description="Descriptive name for the candidate or role target.",
    )
    version: str = Field(default="1.0.0", description="Profile specification version.")
    wedge_capabilities: list[str] = Field(
        default_factory=list,
        description="Core high-leverage capabilities the candidate deploys.",
    )
    required_stack: list[str] = Field(
        default_factory=list,
        description="Core technical stack or infrastructure required in the target company.",
    )
    negative_stack: list[str] = Field(
        default_factory=list,
        description="Technologies indicating legacy bloat or misaligned engineering culture.",
    )
    hiring_catalysts: list[str] = Field(
        default_factory=list,
        description="Catalyst events that create urgent leadership budget and mandate.",
    )
    target_leadership: list[str] = Field(
        default_factory=list,
        description="Leadership traits and counterpart profiles.",
    )
    anchor_companies: list[str] = Field(
        default_factory=list,
        description=(
            "Exemplar companies retained as context for calibration and external "
            "evaluation; not a direct deterministic score."
        ),
    )
    #: Firmographics the gates run on. A lane ships no thresholds, so this is
    #: where the operator states theirs; an unset field leaves its gate off
    #: rather than inventing a bound. These are also the only "dealbreakers" the
    #: engine can actually run, which is why the rest are not fields here.
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
        default_factory=list,
        description="Role families to search for (e.g. enterprise sales, sales operations).",
    )

    @classmethod
    def load(cls, path: Path | str) -> IdealEmployerProfile:
        profile_path = Path(path).expanduser()
        if not profile_path.exists():
            raise FileNotFoundError(f"Ideal Employer Profile not found at: {profile_path}")
        return cls.model_validate(json.loads(profile_path.read_text(encoding="utf-8")))

    def save(self, path: Path | str) -> None:
        profile_path = Path(path).expanduser()
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def funnel_profile(self) -> dict[str, Any]:
        """The firmographics the gate engine runs on, in the shape it reads.

        An employer is never asked the partner question — "is this a delivery
        firm?" — because a product company employs people too, so this lane asks
        nothing of the kind. Size, territory and vertical are the whole of it.

        What a candidate wants *beyond* firmographics — no in-office mandate, no
        pure quota-carrying, nothing thinner than a wrapper — is a preference.
        Preferences are values in this document (``negative_stack``,
        ``wedge_capabilities``, ``hiring_catalysts``); they are not fields the
        engine ships to every install, which is what a `dealbreakers` block was.
        """
        return {
            "allows": "any",
            "size_min": self.size_min,
            "size_max": self.size_max,
            "locations": tuple(self.target_territories),
            "verticals": tuple(self.target_industries),
        }

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

"""Typed Ideal Company Profile used by account-fleet onboarding."""
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

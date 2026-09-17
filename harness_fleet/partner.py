"""Typed Ideal Partner Profile used by partner-fleet onboarding and qualification."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .profile import IdealCompanyProfile


def _size_phrase(minimum: int, maximum: int) -> str:
    if minimum and maximum:
        return f"{minimum}-{maximum} people"
    if minimum:
        return f"at least {minimum} people"
    if maximum:
        return f"up to {maximum} people"
    return "Not specified"


class IdealPartnerProfile(BaseModel):
    """The durable Ideal Partner Profile (IPP) contract for partner research.

    The JSON file is the human-editable authoring form (ideal_partner_profile.json).
    It bridges to IdealCompanyProfile so partner campaigns can integrate directly
    with the existing store and run execution engine.
    """

    model_config = ConfigDict(extra="forbid")

    profile_kind: ClassVar[str] = "ideal_partner"

    profile_name: str = Field(default="My Ideal Partner Profile")
    version: str = Field(default="1.0.0")
    #: Firmographics first, because they are what the funnel runs on. The
    #: cheapest gates — what kind of company this is, how big, where, serving
    #: whom — decide most of a candidate set for almost nothing, so a profile
    #: that leads with semantics leaves its own first four gates unanswerable
    #: and pays a page fetch to learn what a size range would have said.
    #:
    #: A partner lane wants delivery firms: a product vendor is never the
    #: partner implementing somebody's platform.
    partner_kind: str = Field(default="services", description="What this lane looks for: 'services' (delivery firms) or 'any'")
    partner_size_min: int = Field(default=0, description="Headcount floor; 0 leaves it unbounded (e.g. 20)")
    partner_size_max: int = Field(default=0, description="Headcount ceiling; 0 leaves it unbounded (e.g. 500)")
    target_territories: list[str] = Field(default_factory=list, description="Where they must be (e.g. United Kingdom, Ireland)")
    target_industries: list[str] = Field(default_factory=list, description="Industries they must serve (e.g. fintech, healthcare)")
    #: Then the semantics.
    target_ecosystem: str = Field(default="", description="Platform or technology to be implemented (e.g. Snowflake, Kafka, Supabase, Datadog)")
    service_models: list[str] = Field(default_factory=list, description="Target partner delivery models (e.g. Systems Integration, Migration, Managed Services)")
    required_adjacent_competencies: list[str] = Field(default_factory=list, description="Pre-requisite or complementary tech stack (e.g. AWS, Terraform, Kubernetes)")
    target_client_segment: list[str] = Field(default_factory=list, description="Target client tier (e.g. Enterprise, Mid-Market, Regulated)")
    target_partner_tier: str = Field(default="", description="Desired partner agency scale (e.g. Boutique 10-50, Regional 50-250, GSI)")
    key_delivery_roles: list[str] = Field(default_factory=list, description="Client-facing roles (e.g. Solutions Architect, Implementation Consultant, Delivery Lead)")
    negative_exclusions: list[str] = Field(default_factory=list, description="Disqualifiers (e.g. Pure SaaS product vendors, staffing agencies, direct rivals)")
    anchor_partners: list[str] = Field(default_factory=list, description="Reference exemplar partner firms or agencies")
    calibrated_scoring_rubric: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_interview_document(cls, value: Any) -> Any:
        """Flatten the nested shape emitted by the IPP interview guide."""
        if not isinstance(value, dict):
            return value
        nested = value.get("ipp_profile") or value.get("partner_profile")
        if isinstance(nested, dict):
            flattened = dict(nested)
            for key in ("profile_name", "version", "calibrated_scoring_rubric"):
                if key in value:
                    flattened[key] = value[key]
            return flattened
        return value

    @classmethod
    def load(cls, path: Path | str) -> IdealPartnerProfile:
        profile_path = Path(path).expanduser()
        if not profile_path.exists():
            raise FileNotFoundError(f"Ideal Partner Profile not found at: {profile_path}")
        return cls.model_validate(json.loads(profile_path.read_text(encoding="utf-8")))

    def save(self, path: Path | str) -> None:
        profile_path = Path(path).expanduser()
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def funnel_profile(self) -> dict[str, Any]:
        """The firmographics the gate engine runs on, in the shape it reads.

        This is the bridge that was missing: the engine existed and nothing
        handed it a profile, because the profile had no size range, no
        territory and no vertical list to hand over.
        """
        return {
            "allows": self.partner_kind or "services",
            "size_min": self.partner_size_min,
            "size_max": self.partner_size_max,
            "locations": tuple(self.target_territories),
            "verticals": tuple(self.target_industries),
        }

    def query_terms(self) -> dict[str, list[str]]:
        """The values this IPP supplies to the lane's query templates.

        The ecosystem leads the tech axis because it is the one thing the
        partner must actually implement; the adjacent competencies follow,
        since a firm that names them is a firm that does the work.

        ``anchor_partners`` is deliberately absent: a named exemplar is a
        calibration example, not a search — querying it returns that firm's own
        marketing rather than the class of integrator this profile describes.
        """
        tech = ([self.target_ecosystem] if self.target_ecosystem else []) + list(
            self.required_adjacent_competencies
        )
        return {
            "tech": list(dict.fromkeys(tech)),
            "vertical": list(self.target_industries),
            "role": list(self.key_delivery_roles),
            "pain": list(self.service_models),
        }

    def to_company_profile(self) -> IdealCompanyProfile:
        """Map to IdealCompanyProfile for persistence in the SQLite store."""
        trigger_phrases = [
            f"service model: {sm}" for sm in self.service_models
        ] + [
            f"target client: {tc}" for tc in self.target_client_segment
        ]
        return IdealCompanyProfile(
            profile_name=self.profile_name,
            version=self.version,
            product_category=f"Implementation Partner: {self.target_ecosystem}".strip(),
            architectural_layer=self.target_partner_tier or "Systems Integration & Consulting Partner",
            required_stack=list(dict.fromkeys(([self.target_ecosystem] if self.target_ecosystem else []) + self.required_adjacent_competencies)),
            negative_stack_exclusions=self.negative_exclusions,
            trigger_pain_phrases=trigger_phrases,
            target_roles=self.key_delivery_roles,
            anchor_logos=self.anchor_partners,
            calibrated_scoring_rubric=self.calibrated_scoring_rubric,
        )

    def to_prompt_context(self) -> str:
        """Render explicit partner context for task runs.

        Firmographics lead, because they are the gates a candidate meets first;
        a reader tuning thresholds sees the fields the funnel actually ran on
        before the ones it only scores with.
        """
        size = _size_phrase(self.partner_size_min, self.partner_size_max)
        return (
            f"IDEAL PARTNER PROFILE: {self.profile_name} (v{self.version})\n"
            f"Firmographics (the funnel's first gates)\n"
            f"  Company kind: {'delivery firms, not product vendors' if (self.partner_kind or 'services') == 'services' else 'any kind'}\n"
            f"  Size: {size}\n"
            f"  Territory: {', '.join(self.target_territories) or 'Not specified'}\n"
            f"  Industries served: {', '.join(self.target_industries) or 'Not specified'}\n"
            f"Target ecosystem / technology: {self.target_ecosystem or 'Not specified'}\n"
            f"Service delivery models: {', '.join(self.service_models) or 'Not specified'}\n"
            f"Required adjacent competencies: {', '.join(self.required_adjacent_competencies) or 'None specified'}\n"
            f"Target client segment: {', '.join(self.target_client_segment) or 'Not specified'}\n"
            f"Target partner tier: {self.target_partner_tier or 'Not specified'}\n"
            f"Key delivery roles: {', '.join(self.key_delivery_roles) or 'Not specified'}\n"
            f"Negative exclusions: {', '.join(self.negative_exclusions) or 'None specified'}\n"
            f"Anchor exemplar partners: {', '.join(self.anchor_partners) or 'None specified'}\n"
            f"Scoring rubric: {json.dumps(self.calibrated_scoring_rubric, ensure_ascii=False, sort_keys=True)}"
        )

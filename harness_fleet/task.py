"""Declarative task contract loading and preset templates."""
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import TIER_BY_SCORE, TaskSpec


def _checklist_property(items: dict[str, int], descriptions: dict[str, str]) -> dict[str, Any]:
    """Closed boolean-map schema for evidence-bound checklist answers."""
    return {
        "type": "object",
        "description": (
            "Evidence-bound checklist: answer every item true or false; each true "
            "item must be supported by a cited quote. The pipeline computes the score."
        ),
        "properties": {
            item_id: {"type": "boolean", "description": descriptions.get(item_id, f"True when the source supports '{item_id}'.")}
            for item_id in items
        },
        "required": sorted(items),
        "additionalProperties": False,
    }


def _computed_score_property(description: str) -> dict[str, Any]:
    # The wording matters: a free model that reads a friendly description here
    # tries to answer the field itself, and a supplied score is rejected when it
    # disagrees with the checklist. Say "never send this" first.
    return {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "description": f"NEVER include this field in your output: the pipeline computes it from the checklist. {description}",
    }


SCORE_CHECKLIST = {
    "initiative_named": 40,
    "criteria_evidence": 35,
    "supporting_signals": 25,
}
SCORE_CHECKLIST_DESCRIPTIONS = {
    "initiative_named": "True only when the source names an active explicit initiative, project, or evaluation.",
    "criteria_evidence": "True only when the source confirms stated qualification criteria with cited facts.",
    "supporting_signals": "True only when the source shows supporting signals such as hiring, funding, or leadership change.",
}
SCORE_EVIDENCE_TERMS = [
    "initiative", "migration", "evaluation", "pilot", "hiring", "headcount",
    "funding", "roadmap", "priority", "moderniz", "platform", "scale",
]
ACCOUNT_CHECKLIST = {
    "explicit_initiative": 40,
    "stack_confirmed": 30,
    "hiring_or_trigger": 20,
    "firmographic_fit": 10,
}
ACCOUNT_CHECKLIST_DESCRIPTIONS = {
    "explicit_initiative": "True only when the source names an active explicit initiative or bottleneck, quoted verbatim.",
    "stack_confirmed": "True only when the source confirms required-stack technology in use.",
    "hiring_or_trigger": "True only when the source shows senior infra hiring or a trigger/pain phrase.",
    "firmographic_fit": "True only when the source confirms firmographic fit such as size, sector, or model.",
}
ACCOUNT_EVIDENCE_TERMS = [
    "initiative", "migration", "moderniz", "hiring", "headcount", "platform",
    "bottleneck", "scale", "latency", "outage", "roadmap", "kubernetes",
]
# Initial recency calibration in days: hiring urgency goes stale in weeks
# (a live posting can close any day) while stack and firmographic facts
# last months. Refit against labeled outcomes; these are starting points.
ACCOUNT_HALF_LIVES = {
    "explicit_initiative": 90.0,
    "stack_confirmed": 180.0,
    "hiring_or_trigger": 21.0,
    "firmographic_fit": 365.0,
}
# Revenue-weighted partner checklist. Every question is answerable from the
# sources the sourcing plan actually reaches — the partner's own site, their ATS
# board, vendor registries, review directories, and communities — and each one
# bears on whether this partner can generate revenue with us.
PARTNER_CHECKLIST = {
    "q1_billable_delivery": 15,
    "q2_stack_delivery": 15,
    "q3_delivery_hiring": 10,
    "q4_client_outcome": 15,
    "q5_commercial_scale": 5,
    "q6_vendor_alliance": 10,
    "q7_vertical_focus": 5,
    "q8_independent_validation": 15,
    "q9_published_engineering": 5,
    "q10_growth_signal": 5,
}
PARTNER_CHECKLIST_DESCRIPTIONS = {
    "q1_billable_delivery": "True only when the source shows they sell project-based delivery (engagement, statement of work, implementation, managed service). False for licence resellers and pure staff augmentation.",
    "q2_stack_delivery": "True only when the source evidences delivery of the target technology for named clients.",
    "q3_delivery_hiring": "True only when an open requisition on their own ATS board is for a delivery or engineering role in the target stack.",
    "q4_client_outcome": "True only when the source names a client and a concrete outcome or metric, not a capability claim.",
    "q5_commercial_scale": "True only when published commercial terms appear: minimum project size, hourly rate, or employee count.",
    "q6_vendor_alliance": "True only when a vendor partner tier, certification, or marketplace listing is stated.",
    "q7_vertical_focus": "True only when one industry vertical has repeat delivery proof, not a list of every sector served.",
    "q8_independent_validation": "True only when a source that is not the partner's own marketing vouches for delivery: a review, a vendor case study, or a community mention.",
    "q9_published_engineering": "True only when they publish technical work of their own (engineering blog, conference talk, open source) showing the practice is real.",
    "q10_growth_signal": "True only when a dated growth event appears in the last twelve months: funding, acquisition, new office, new practice, or an award.",
}
PARTNER_EVIDENCE_TERMS = [
    "partner", "consulting", "implementation", "client", "integrat", "migration",
    "solutions architect", "practice", "deployment", "certified", "managed service",
    "case study", "delivery", "professional services", "enterprise", "vertical",
    "engagement", "statement of work", "retainer", "minimum project", "hourly rate",
    "review", "recommend", "hiring", "requisition", "award", "funding", "acquisition",
]
# Recency calibration in days: a live requisition or a growth announcement goes
# stale fast; stack and commercial facts last a year or more.
PARTNER_HALF_LIVES = {
    "q1_billable_delivery": 365.0,
    "q2_stack_delivery": 180.0,
    "q3_delivery_hiring": 21.0,
    "q4_client_outcome": 180.0,
    "q5_commercial_scale": 365.0,
    "q6_vendor_alliance": 180.0,
    "q7_vertical_focus": 365.0,
    "q8_independent_validation": 180.0,
    "q9_published_engineering": 180.0,
    "q10_growth_signal": 90.0,
}

PRESETS: dict[str, dict[str, Any]] = {
    "summarize": {
        "instructions": "Summarize each item using only supported source facts.",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One or two sentences supported only by the cited source quotes, no outside knowledge.",
            },
        },
        "required": ["summary"],
    },
    "classify": {
        "instructions": "Classify each item and give a short supported summary.",
        "properties": {
            "label": {
                "type": "string",
                "description": "The single best-fitting category label for the item, supported by the cited quotes.",
            },
            "summary": {
                "type": "string",
                "description": "One or two sentences supported only by the cited source quotes, no outside knowledge.",
            },
        },
        "required": ["label", "summary"],
    },
    "extract": {
        "instructions": "Extract a short supported summary and the named entities present in the source.",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One or two sentences supported only by the cited source quotes, no outside knowledge.",
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Named entities stated verbatim in the source (products, companies, systems), never inferred.",
            },
        },
        "required": ["summary", "entities"],
    },
    "triage": {
        "instructions": "Assign a supported triage priority and explain why.",
        "properties": {
            "priority": {
                "enum": ["high", "medium", "low", "unknown"],
                "description": "Urgency grounded in the source: high only for explicit blocking impact, unknown when the source is silent.",
            },
            "reason": {
                "type": "string",
                "description": "Short explanation naming the source evidence behind the priority.",
            },
        },
        "required": ["priority", "reason"],
    },
    "score": {
        "instructions": "Evaluate each item against qualification criteria by answering the evidence checklist; the pipeline computes the 0-100 score.",
        "checklist": SCORE_CHECKLIST,
        "evidence_terms": SCORE_EVIDENCE_TERMS,
        "properties": {
            "checklist": _checklist_property(SCORE_CHECKLIST, SCORE_CHECKLIST_DESCRIPTIONS),
            "score": _computed_score_property(
                "Qualification score: 85-100 explicit named initiative, 70-84 confirmed criteria plus supporting signals, 50-69 partial fit only, 0-49 incompatible or silent."
            ),
            "reason": {
                "type": "string",
                "description": "Short explanation naming the source evidence behind the checklist answers.",
            },
        },
        "required": ["checklist", "reason"],
    },
    "filter": {
        "instructions": "Screen each item against qualifying criteria, providing a boolean passed status and supported explanation.",
        "properties": {
            "passed": {
                "type": "boolean",
                "description": "True only when the source meets every qualifying criterion with cited evidence; false otherwise.",
            },
            "reason": {
                "type": "string",
                "description": "Short explanation naming the source evidence behind the decision.",
            },
        },
        "required": ["passed", "reason"],
    },
    "account-research": {
        "instructions": "Evaluate target account technical fit by answering the evidence checklist, identify key technical bottlenecks or gaps, and cite verbatim evidence. The pipeline computes the ICP fit score and tier.",
        "checklist": ACCOUNT_CHECKLIST,
        "evidence_terms": ACCOUNT_EVIDENCE_TERMS,
        "recency_half_lives": ACCOUNT_HALF_LIVES,
        "properties": {
            "checklist": _checklist_property(ACCOUNT_CHECKLIST, ACCOUNT_CHECKLIST_DESCRIPTIONS),
            "score": _computed_score_property(
                "ICP fit: 85-100 active explicit initiative quoted verbatim, 70-84 confirmed stack plus senior infra hiring, 50-69 firmographic fit only, 0-49 incompatible stack or wrong model."
            ),
            "identified_gap": {
                "type": "string",
                "description": "The verified technical initiative or bottleneck, named exactly as the source names it.",
            },
            "fit_tier": {
                "type": "string",
                "enum": ["tier_1", "tier_2", "tier_3", "unfit"],
                "description": "NEVER include this field in your output: the pipeline derives the band from the score. tier_1 85-100, tier_2 70-84, tier_3 50-69, unfit below 50.",
            },
            "reasoning": {
                "type": "string",
                "description": "Short explanation grounded in the cited quotes, naming the evidence behind the checklist and gap.",
            },
        },
        "required": ["checklist", "identified_gap", "reasoning"],
    },
    "partner-research": {
        "instructions": "Decide whether this implementation partner can generate revenue with us: answer the 10-question revenue checklist, name the verified practice, state how we would make money together, and cite verbatim evidence from the multi-source dossier. Return only item_id, claims and quotes: never send score, fit_tier or passed, because the pipeline derives all three and rejects any answer that carries them.",
        "checklist": PARTNER_CHECKLIST,
        "evidence_terms": PARTNER_EVIDENCE_TERMS,
        "recency_half_lives": PARTNER_HALF_LIVES,
        "properties": {
            "checklist": _checklist_property(PARTNER_CHECKLIST, PARTNER_CHECKLIST_DESCRIPTIONS),
            "score": _computed_score_property(
                "Partner revenue fit: 85-100 delivery proven, independently validated and growing (Tier 1), 70-84 strong delivery with some gaps (Tier 2), 50-69 plausible but thin or unproven (Tier 3), 0-49 licence reseller, staff-aug only, or no delivery evidence."
            ),
            "identified_practice": {
                "type": "string",
                "description": "The verified partner practice, capability, or core service offering, named exactly as the source names it.",
            },
            "revenue_hypothesis": {
                "type": "string",
                "description": "One sentence on how we would make money with them (co-sell, referral, reseller, or subcontract), tied only to cited evidence.",
            },
            "answers": {
                "type": "object",
                "description": "Structured attributes. Use an empty list or empty string when the sources do not state it — never guess.",
                "properties": {
                    "target_stack": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Technologies and platforms they have implemented for clients.",
                    },
                    "service_model": {
                        "type": "string",
                        "enum": ["turnkey_si", "migration_modernization", "managed_services", "staff_aug_only", "saas_vendor", "unknown"],
                        "description": "Primary client delivery model.",
                    },
                    "industry_verticals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Industry verticals with delivery proof.",
                    },
                    "delivery_coverage": {
                        "type": "string",
                        "description": "Headquarters and delivery locations or regions they can serve.",
                    },
                    "vendor_alliances": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Certified partner tiers and vendor badges, named as the source names them.",
                    },
                    "case_study_outcome": {
                        "type": "string",
                        "description": "A specific project outcome or metric achieved for a named client.",
                    },
                    "client_logos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Clients named in the sources. Named clients are the strongest revenue proof.",
                    },
                    "hiring_signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Open delivery or engineering roles with seniority and location, from their ATS board.",
                    },
                    "commercial_terms": {
                        "type": "object",
                        "description": "Published commercial terms. Empty strings when the sources do not state them.",
                        "properties": {
                            "min_project_size": {"type": "string", "description": "Minimum engagement size as published (for example '$50,000+')."},
                            "hourly_rate": {"type": "string", "description": "Published hourly or day rate range."},
                            "employees": {"type": "string", "description": "Employee or delivery-team count as published."},
                        },
                        "required": ["min_project_size", "hourly_rate", "employees"],
                        "additionalProperties": False,
                    },
                    "revenue_motion": {
                        "type": "string",
                        "enum": ["co_sell", "referral", "reseller", "subcontract", "unknown"],
                        "description": "How revenue would most plausibly flow between us and them.",
                    },
                    "third_party_mentions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Independent mentions that vouch for delivery: a review, a vendor case study, or a community thread. Name the source.",
                    },
                    "engineering_output": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Their own technical output: talks, blog posts, open-source repositories.",
                    },
                    "growth_signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Dated growth events from the last twelve months: funding, acquisition, new office, new practice, award.",
                    },
                    "evidence_categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The source categories actually cited, copied from the dossier metadata.",
                    },
                },
                "required": ["target_stack", "service_model", "industry_verticals", "delivery_coverage", "vendor_alliances", "case_study_outcome", "client_logos", "hiring_signals", "commercial_terms", "revenue_motion", "third_party_mentions", "engineering_output", "growth_signals", "evidence_categories"],
                "additionalProperties": False,
            },
            "source_diversity_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of distinct source categories (First-Party, Vendor Registry, Review Audit, Community, ATS) cited in the supporting quotes.",
            },
            "fit_tier": {
                "type": "string",
                "enum": ["tier_1", "tier_2", "tier_3", "unfit"],
                "description": "NEVER include this field in your output: the pipeline derives the band from the score. tier_1 85-100, tier_2 70-84, tier_3 50-69, unfit below 50.",
            },
            "reasoning": {
                "type": "string",
                "description": "Short explanation grounded in the cited quotes, naming the evidence behind the checklist and the revenue hypothesis.",
            },
        },
        "required": ["checklist", "identified_practice", "revenue_hypothesis", "reasoning", "answers"],
    },
}


def parse_source_weight(spec: str) -> tuple[str, float]:
    """Parse a 'substring=weight' source-weight rule (weight in 0-1)."""
    if "=" not in spec:
        raise ValueError(f"source weight must look like 'example.com=0.5', got {spec!r}")
    match, _, raw = spec.partition("=")
    match = match.strip().casefold()
    if not match:
        raise ValueError(f"source weight needs a non-empty match, got {spec!r}")
    try:
        weight = float(raw)
    except ValueError as exc:
        raise ValueError(f"source weight for '{match}' is not numeric: {raw!r}") from exc
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"source weight for '{match}' must be between 0 and 1")
    return match, weight


def parse_half_life(spec: str) -> tuple[str, float]:
    """Parse an 'item=days' recency half-life rule (days must be positive)."""
    if "=" not in spec:
        raise ValueError(f"half-life must look like 'hiring_or_trigger=21', got {spec!r}")
    item_id, _, raw = spec.partition("=")
    item_id = item_id.strip()
    if not item_id:
        raise ValueError(f"half-life needs a non-empty item id, got {spec!r}")
    try:
        days = float(raw)
    except ValueError as exc:
        raise ValueError(f"half-life for '{item_id}' is not numeric: {raw!r}") from exc
    if not math.isfinite(days) or days <= 0:
        raise ValueError(f"half-life for '{item_id}' must be a positive number of days")
    return item_id, days


def create_task_from_preset(
    name: str,
    preset_name: str = "score",
    instructions: str | None = None,
    batch_size: int = 5,
    source_weights: dict[str, float] | None = None,
    default_source_weight: float = 1.0,
    recency_half_lives: dict[str, float] | None = None,
) -> TaskSpec:
    if preset_name not in PRESETS:
        raise ValueError(f"unknown preset '{preset_name}'; available presets: {', '.join(sorted(PRESETS))}")
    preset = PRESETS[preset_name]
    return TaskSpec(
        name=name,
        instructions=instructions or preset["instructions"],
        batch_size=batch_size,
        claims_schema={
            "type": "object",
            "properties": preset["properties"],
            "required": preset["required"],
            "additionalProperties": False,
        },
        checklist=dict(preset["checklist"]) if preset.get("checklist") else None,
        pass_score=preset.get("pass_score"),
        evidence_terms=list(preset.get("evidence_terms") or []),
        source_weights=dict(source_weights or {}),
        default_source_weight=default_source_weight,
        recency_half_lives={
            **(preset.get("recency_half_lives") or {}),
            **(recency_half_lives or {}),
        },
    )


# Fields a scoring editor may change. Everything else in a TaskSpec —
# especially score, fit_tier, and passed — is derived by the pipeline from
# checklist answers, so exposing them would reintroduce free-form grading.
SCORING_EDITABLE_FIELDS = frozenset({
    "instructions",
    "checklist",
    "source_weights",
    "default_source_weight",
    "pass_score",
    "evidence_terms",
    "half_lives",
    "min_quote_chars",
    "batch_size",
    "max_slice_chars",
    "candidate_top_n",
})
SCORING_LOCKED_FIELDS = frozenset({"score", "fit_tier", "passed", "tier_bands", "claims_schema"})
_CHECKLIST_ITEM_FIELDS = frozenset({"item_id", "points", "description", "half_life_days"})


def tier_bands() -> list[dict[str, Any]]:
    """Read-only 0-100 score bands, in descending order of minimum score."""
    ordered = sorted(TIER_BY_SCORE, key=lambda pair: pair[0], reverse=True)
    bands: list[dict[str, Any]] = []
    for index, (threshold, tier) in enumerate(ordered):
        upper = 100 if index == 0 else ordered[index - 1][0] - 1
        bands.append({"tier": tier, "min_score": threshold, "max_score": upper})
    return bands


def _checklist_descriptions(spec: TaskSpec) -> dict[str, str]:
    """Item descriptions carried in the claims schema's checklist property."""
    properties = spec.claims_schema.get("properties", {}) if isinstance(spec.claims_schema, dict) else {}
    checklist_prop = properties.get("checklist") if isinstance(properties, dict) else None
    item_props = checklist_prop.get("properties", {}) if isinstance(checklist_prop, dict) else {}
    descriptions: dict[str, str] = {}
    for item_id, described in (item_props or {}).items():
        if isinstance(described, dict):
            descriptions[str(item_id)] = str(described.get("description") or "")
    return descriptions


def scoring_view(spec: TaskSpec) -> dict[str, Any]:
    """Plain-language read model of a task's scoring contract.

    This is a view over the typed TaskSpec, not a second scoring engine:
    checklist items and points are editable calibration, while score,
    fit_tier, and passed stay pipeline-derived and are reported as fixed.
    """
    properties = spec.claims_schema.get("properties", {}) if isinstance(spec.claims_schema, dict) else {}
    descriptions = _checklist_descriptions(spec)
    items = [
        {
            "item_id": item_id,
            "points": int(points),
            "description": descriptions.get(item_id, ""),
            "half_life_days": spec.recency_half_lives.get(item_id),
        }
        for item_id, points in (spec.checklist or {}).items()
    ]
    return {
        "task": spec.name,
        "scorable": spec.checklist is not None,
        "derived": True,
        "instructions": spec.instructions,
        "checklist": items,
        "total_points": sum(int(points) for points in (spec.checklist or {}).values()),
        "score_cap": 100,
        "tier_bands": tier_bands(),
        "has_fit_tier": isinstance(properties, dict) and "fit_tier" in properties,
        "has_pass": isinstance(properties, dict) and "passed" in properties,
        "pass_score": spec.pass_score,
        "source_weights": dict(spec.source_weights),
        "default_source_weight": float(spec.default_source_weight),
        "evidence_terms": list(spec.evidence_terms),
        "min_quote_chars": spec.min_quote_chars,
        "batch_size": spec.batch_size,
        "max_slice_chars": spec.max_slice_chars,
        "candidate_top_n": spec.candidate_top_n,
    }


def _positive_days(item_id: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"half-life for '{item_id}' must be a number of days")
    try:
        days = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"half-life for '{item_id}' must be a number of days") from exc
    if not math.isfinite(days) or days <= 0:
        raise ValueError(f"half-life for '{item_id}' must be a positive number of days")
    return days


def _integer_field(name: str, value: Any, low: int, high: int) -> int:
    """Validate an integer field, rejecting bools, floats, and out-of-range values."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{name} must be an integer")
    result = int(number)
    if not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def _edited_checklist(
    patch: Any,
    descriptions: dict[str, str],
    existing_half_lives: dict[str, float],
) -> tuple[dict[str, int], dict[str, str], dict[str, float]]:
    """Validate an edited checklist into items, descriptions, and half-lives.

    A checklist entry is a full replacement for that item. An omitted
    ``half_life_days`` key preserves the stored value; an explicit null or
    empty string clears it, so a client that posts partial rows cannot
    silently drop recency calibration.
    """
    if not isinstance(patch, list) or not patch:
        raise ValueError("checklist must be a non-empty list of items")
    items: dict[str, int] = {}
    edited_descriptions: dict[str, str] = {}
    half_lives: dict[str, float] = {}
    for entry in patch:
        if not isinstance(entry, dict):
            raise ValueError("each checklist item must be an object")
        unknown = sorted(set(entry) - _CHECKLIST_ITEM_FIELDS)
        if unknown:
            raise ValueError(f"unsupported checklist item fields: {unknown}")
        item_id = str(entry.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("checklist item ids must be non-empty strings")
        if item_id in items:
            raise ValueError(f"duplicate checklist item id: {item_id}")
        points = _integer_field(f"checklist points for '{item_id}'", entry.get("points"), 1, 100)
        items[item_id] = points
        # Blank descriptions fall through to _checklist_property's generic text.
        text = str(entry.get("description") or descriptions.get(item_id, "")).strip()
        if text:
            edited_descriptions[item_id] = text
        if "half_life_days" in entry:
            half_life = entry.get("half_life_days")
            if half_life not in (None, ""):
                half_lives[item_id] = _positive_days(item_id, half_life)
        elif item_id in existing_half_lives:
            half_lives[item_id] = float(existing_half_lives[item_id])
    return items, edited_descriptions, half_lives


def apply_scoring_edit(spec: TaskSpec, patch: Any) -> TaskSpec:
    """Return a new TaskSpec with structured scoring edits applied.

    Only evidence-level calibration is accepted: checklist items and points,
    source weights, recency half-lives, the pass threshold, and the
    instruction text. Score, fit_tier, and passed are pipeline-derived, and
    a replacement claims_schema is rejected, so a UI can tune the rubric
    without ever grading a record by hand.
    """
    if not isinstance(patch, dict):
        raise ValueError("scoring patch must be a JSON object")
    unknown = sorted(set(patch) - SCORING_EDITABLE_FIELDS)
    if unknown:
        locked = sorted(set(unknown) & SCORING_LOCKED_FIELDS)
        if locked:
            raise ValueError(
                f"{', '.join(locked)} is derived by the pipeline and cannot be edited; "
                "edit the evidence checklist and weights instead"
            )
        raise ValueError(f"unsupported scoring fields: {unknown}")
    if spec.checklist is None:
        raise ValueError(f"task '{spec.name}' has no scoring checklist to edit")

    payload = spec.model_dump(mode="json", by_alias=True)
    half_lives = dict(spec.recency_half_lives)
    effective_items = dict(spec.checklist)

    if "instructions" in patch:
        text = str(patch["instructions"]).strip()
        if not text:
            raise ValueError("instructions must not be empty")
        payload["instructions"] = text

    if "checklist" in patch:
        items, descriptions, half_lives = _edited_checklist(
            patch["checklist"], _checklist_descriptions(spec), spec.recency_half_lives
        )
        effective_items = items
        payload["checklist"] = items
        schema = deepcopy(spec.claims_schema)
        schema.setdefault("properties", {})["checklist"] = _checklist_property(items, descriptions)
        payload["claims_schema"] = schema

    if "half_lives" in patch:
        raw_halves = patch["half_lives"]
        if not isinstance(raw_halves, dict):
            raise ValueError("half_lives must be an object of checklist item id to days")
        half_lives = {}
        for item_id, value in raw_halves.items():
            if item_id not in effective_items:
                raise ValueError(f"half-life for '{item_id}' names no checklist item")
            half_lives[item_id] = _positive_days(item_id, value)
    payload["recency_half_lives"] = half_lives

    if "source_weights" in patch:
        raw_weights = patch["source_weights"]
        if not isinstance(raw_weights, dict):
            raise ValueError("source_weights must be an object of URI substring to weight")
        weights: dict[str, float] = {}
        for match, value in raw_weights.items():
            key = str(match).strip().casefold()
            if not key:
                raise ValueError("source weight keys must be non-empty strings")
            if isinstance(value, bool):
                raise ValueError(f"source weight for '{key}' must be a number 0-1")
            try:
                weight = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"source weight for '{key}' must be a number 0-1") from exc
            if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
                raise ValueError(f"source weight for '{key}' must be between 0 and 1")
            weights[key] = weight
        payload["source_weights"] = weights

    if "default_source_weight" in patch:
        if isinstance(patch["default_source_weight"], bool):
            raise ValueError("default_source_weight must be a number 0-1")
        try:
            default_weight = float(patch["default_source_weight"])
        except (TypeError, ValueError) as exc:
            raise ValueError("default_source_weight must be a number 0-1") from exc
        if not math.isfinite(default_weight) or not 0.0 <= default_weight <= 1.0:
            raise ValueError("default_source_weight must be between 0 and 1")
        payload["default_source_weight"] = default_weight

    if "pass_score" in patch:
        value = patch["pass_score"]
        if value in (None, ""):
            payload["pass_score"] = None
        else:
            payload["pass_score"] = _integer_field("pass_score", value, 0, 100)

    if "evidence_terms" in patch:
        raw_terms = patch["evidence_terms"]
        if not isinstance(raw_terms, list):
            raise ValueError("evidence_terms must be a list of non-empty strings")
        terms = []
        for term in raw_terms:
            text = str(term).strip()
            if not text:
                raise ValueError("evidence_terms must be non-empty strings")
            terms.append(text)
        payload["evidence_terms"] = terms

    # Bounds mirror the TaskSpec field constraints, so invalid values fail here
    # with a readable message instead of a pydantic ValidationError dump.
    for field, low, high in (
        ("min_quote_chars", 1, 10_000),
        ("batch_size", 1, 100),
        ("max_slice_chars", 300, 100_000),
        ("candidate_top_n", 1, 32),
    ):
        if field in patch:
            payload[field] = _integer_field(field, patch[field], low, high)

    return TaskSpec.model_validate(payload)


def duplicate_spec(spec: TaskSpec, new_name: str) -> TaskSpec:
    """Copy a task contract under a new name.

    Round-trips through model_validate so the copy is re-checked, and clears
    nothing else: the duplicate starts as an exact, separately-revisioned
    clone that can be tuned without touching the original.
    """
    payload = spec.model_dump(mode="json", by_alias=True)
    payload["name"] = new_name
    return TaskSpec.model_validate(payload)


def load_task_spec(task_path: str | Path) -> TaskSpec:
    path = Path(task_path)
    if path.suffix.lower() != ".json":
        raise ValueError("task specs must be JSON files")
    if not path.is_file():
        raise FileNotFoundError(f"task file not found: {path}")
    return TaskSpec.model_validate_json(path.read_text(encoding="utf-8"))

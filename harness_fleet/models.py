"""Closed data contracts used at trust boundaries."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from . import contracts, evidence

SCHEMA_BASE = "https://raw.githubusercontent.com/NatesVibeCode/harness-fleet/master/schemas"
ID_PATTERN = r"^[A-Za-z0-9_.-]+$"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class InputItem(ClosedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE}/input-item-v1.schema.json",
        }
    )
    schema_uri: Literal[f"{SCHEMA_BASE}/input-item-v1.schema.json"] = Field(  # type: ignore[valid-type]
        default=f"{SCHEMA_BASE}/input-item-v1.schema.json",
        alias="$schema",
    )
    item_id: str = Field(description="Stable input identity", min_length=1, max_length=128, pattern=ID_PATTERN)
    text: str = Field(description="Complete source text used for evidence verification", min_length=1)
    title: str | None = Field(default=None, description="Optional source label")
    source_uri: str | None = Field(default=None, description="Optional source locator retained in output")
    content_type: str = Field(
        default="text/plain",
        description="Media type describing text serialization",
        pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$",
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict, description="Source metadata retained with the item")


class SourceSlice(ClosedModel):
    slice_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str
    partial: bool

    @model_validator(mode="after")
    def exact_length(self) -> SourceSlice:
        if self.end - self.start != len(self.text):
            raise ValueError("slice offsets must match text length")
        return self


class PackedItem(ClosedModel):
    item_id: str
    title: str | None = None
    source_uri: str | None = None
    content_type: str
    metadata: dict[str, JsonValue]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    slices: list[SourceSlice] = Field(min_length=1)
    full_char_length: int = Field(ge=1)


class PackedBatch(ClosedModel):
    batch_id: str = Field(pattern=r"^batch_[0-9a-f]{16}$")
    items: list[PackedItem] = Field(min_length=1)


class QuoteRef(ClosedModel):
    slice_id: str = Field(description="Exact source slice containing the quote", min_length=1)
    start: int = Field(description="Inclusive absolute character offset", ge=0)
    end: int = Field(description="Exclusive absolute character offset", gt=0)
    text: str = Field(description="Exact source substring at start:end", min_length=1)
    supports: list[str] = Field(
        default_factory=list,
        description="Checklist item ids this quote backs",
    )

    @model_validator(mode="after")
    def valid_range(self) -> QuoteRef:
        if self.end <= self.start:
            raise ValueError("quote end must be greater than start")
        return self


class QuoteCandidate(ClosedModel):
    slice_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    candidate_id: int | None = Field(
        default=None,
        ge=0,
        description="Optional evidence-candidate span id from the prompt; verification recomputes the span table",
    )
    supports: list[str] = Field(
        default_factory=list,
        description="Checklist item ids this quote backs; every true answer needs at least one supporting quote",
    )

    @model_validator(mode="after")
    def complete_optional_range(self) -> QuoteCandidate:
        if (self.start is None) != (self.end is None):
            raise ValueError("quote start and end must be supplied together")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("quote end must be greater than start")
        return self


class ExtractedItem(ClosedModel):
    item_id: str = Field(min_length=1)
    source_uri: str | None = None
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str
    claims: dict[str, JsonValue]
    quotes: list[QuoteRef] = Field(min_length=1)
    captured_at: str | None = Field(
        default=None,
        description="ISO-8601 evidence capture time from input metadata; None means ageless",
    )
    scored_at: str | None = Field(
        default=None,
        description="ISO-8601 scoring time; fixed at verification so revalidation is stable",
    )


class ModelOutput(ClosedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE}/output-v2.schema.json",
        }
    )
    items: list[ExtractedItem]


class CandidateExtractedItem(ClosedModel):
    """One extracted item. Closed on purpose: a stray key is a failed attempt,
    never a silently dropped field."""

    item_id: str = Field(min_length=1)
    claims: dict[str, JsonValue]
    quotes: list[QuoteCandidate] = Field(min_length=1)


class CandidateModelOutput(ClosedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE}/candidate-output-v1.schema.json",
        }
    )
    items: list[CandidateExtractedItem]


DEFAULT_CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "category": {"type": "string"},
    },
    "required": ["summary", "category"],
    "additionalProperties": False,
}


TIER_BY_SCORE: tuple[tuple[int, str], ...] = (
    (85, "tier_1"),
    (70, "tier_2"),
    (50, "tier_3"),
    (0, "unfit"),
)
TIER_SET = frozenset({"tier_1", "tier_2", "tier_3", "unfit"})


def score_to_fit_tier(score: Any) -> str:
    """Derive the deterministic fit tier for a numeric 0-100 score.

    Boundaries follow the scoring-rubric guide: tier_1 85-100, tier_2 70-84,
    tier_3 50-69, unfit 0-49. Raises ValueError for non-numeric input.
    """
    try:
        value = float(score)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"score {score!r} is not numeric") from exc
    if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124  # NaN check is intentional
        raise ValueError(f"score {score!r} is not finite")
    for threshold, tier in TIER_BY_SCORE:
        if value >= threshold:
            return tier
    return "unfit"


INSTRUCTION_TEMPLATE = (
    "Form-fill contract: return exactly one entry per input item, keyed by that item's own "
    "item_id (never a slice_id), filling every required claim from the supplied source "
    "sections only. Attach at least one exact quote per item, copied from a slice that "
    "belongs to the same item. Never use outside knowledge. Return JSON only."
)
DEFAULT_INSTRUCTIONS = "Extract only facts supported by the supplied source slices."


class TaskSpec(ClosedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE}/task-v1.schema.json",
        }
    )
    schema_uri: Literal[f"{SCHEMA_BASE}/task-v1.schema.json"] = Field(  # type: ignore[valid-type]
        default=f"{SCHEMA_BASE}/task-v1.schema.json",
        alias="$schema",
    )
    format_version: Literal["harness_fleet_task_v1"] = "harness_fleet_task_v1"
    name: str = Field(description="Stable task name", min_length=1, max_length=128, pattern=ID_PATTERN)
    instructions: str = Field(
        default=DEFAULT_INSTRUCTIONS,
        description="Outcome-specific directions; field structure belongs in claims_schema",
    )
    batch_size: int = Field(default=6, description="Maximum input items per model request", ge=1, le=100)
    max_slice_chars: int = Field(default=6000, description="Maximum source characters exposed per item", ge=300, le=100_000)
    min_quote_chars: int = Field(default=15, description="Minimum admitted evidence-quote length", ge=1, le=10_000)
    claims_schema: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_CLAIMS_SCHEMA),
        description="Draft 2020-12 object schema; type=object and additionalProperties=false are required",
    )
    checklist: dict[str, int] | None = Field(
        default=None,
        description="Checklist item id to score points; the pipeline derives score from true answers",
    )
    pass_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Minimum derived score that yields passed=true; the pipeline derives the boolean",
    )
    evidence_terms: list[str] = Field(
        default_factory=list,
        description="Terms used to rank deterministic evidence-candidate spans per section",
    )
    candidate_top_n: int = Field(
        default=6,
        ge=1,
        le=32,
        description="Maximum evidence-candidate spans exposed per section",
    )
    source_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Lowercased URI substring to evidentiary weight 0-1; longest match wins, 0 blocks support",
    )
    default_source_weight: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Weight for sources matching no rule; 1 preserves legacy unweighted scoring",
    )
    recency_half_lives: dict[str, float] = Field(
        default_factory=dict,
        description="Checklist item id to evidence half-life in days; support decays by half each period",
    )

    @model_validator(mode="after")
    def closed_claims_schema(self) -> TaskSpec:
        Draft202012Validator.check_schema(self.claims_schema)
        if self.claims_schema.get("type") != "object":
            raise ValueError("claims_schema must describe an object")
        if self.claims_schema.get("additionalProperties") is not False:
            raise ValueError("claims_schema must set additionalProperties to false")
        if self.checklist is not None:
            if not self.checklist:
                raise ValueError("checklist must not be empty")
            for item_id, points in self.checklist.items():
                if not isinstance(item_id, str) or not item_id.strip():
                    raise ValueError("checklist item ids must be non-empty strings")
                if not isinstance(points, int) or isinstance(points, bool) or points < 1:
                    raise ValueError(f"checklist points for '{item_id}' must be a positive integer")
            props = self.claims_schema.get("properties", {})
            checklist_prop = props.get("checklist") if isinstance(props, dict) else None
            if not isinstance(checklist_prop, dict) or checklist_prop.get("type") != "object":
                raise ValueError("a task checklist requires a 'checklist' object property in claims_schema")
        for term in self.evidence_terms:
            if not isinstance(term, str) or not term.strip():
                raise ValueError("evidence_terms must be non-empty strings")
        for match, weight in self.source_weights.items():
            if not isinstance(match, str) or not match.strip():
                raise ValueError("source_weights keys must be non-empty strings")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                raise ValueError(f"source weight for '{match}' must be a number 0-1")
            if not 0.0 <= float(weight) <= 1.0:
                raise ValueError(f"source weight for '{match}' must be between 0 and 1")
        for item_id, half_life in self.recency_half_lives.items():
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError("recency_half_lives keys must be non-empty strings")
            if not isinstance(half_life, (int, float)) or isinstance(half_life, bool):
                raise ValueError(f"half-life for '{item_id}' must be a positive number of days")
            if not math.isfinite(float(half_life)) or float(half_life) <= 0:
                raise ValueError(f"half-life for '{item_id}' must be a positive number of days")
            if self.checklist is not None and item_id not in self.checklist:
                raise ValueError(f"half-life for '{item_id}' names no checklist item")
        return self

    @staticmethod
    def _parse_captured_at(value: Any) -> datetime | None:
        """Parse an ISO-8601 capture timestamp; None when missing or malformed.

        Naive timestamps are read as UTC. Malformed values decay nothing:
        staleness must be proven by a date, never assumed from its absence.
        """
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def recency_decay(
        self,
        item_id: str,
        captured_at: str | None,
        scored_at: str | None,
    ) -> float:
        """Exponential decay for evidence age: half the support each half-life.

        Hiring signals go stale in weeks while company fundamentals last
        months; per-item half-lives price that difference. Items without a
        configured half-life, and evidence without usable dates on both
        ends, decay nothing. Future-dated captures clamp to full strength.
        """
        half_life = self.recency_half_lives.get(item_id)
        if half_life is None:
            return 1.0
        captured = self._parse_captured_at(captured_at)
        scored = self._parse_captured_at(scored_at)
        if captured is None or scored is None:
            return 1.0
        age_days = max(0.0, (scored - captured).total_seconds() / 86400.0)
        if age_days <= 0:
            return 1.0
        return 0.5 ** (age_days / float(half_life))

    def source_weight(self, source_uri: str | None) -> float:
        """Evidentiary weight for a source URI: longest matching rule wins.

        Matching is case-insensitive substring on the full URI, so
        "boards.greenhouse.io" beats "greenhouse.io" by length. Sources
        with no rule — including a missing URI — take default_source_weight.
        """
        if not source_uri or not self.source_weights:
            return self.default_source_weight
        lowered = source_uri.casefold()
        best: str | None = None
        for match in self.source_weights:
            if match.casefold() in lowered and (best is None or len(match) > len(best)):
                best = match
        return float(self.source_weights[best]) if best is not None else self.default_source_weight

    def support_strengths(
        self,
        quotes: list[Any],
        source_uri: str | None,
        captured_at: str | None = None,
        scored_at: str | None = None,
        text: str | None = None,
    ) -> dict[str, float]:
        """Per-item support strength: source weight times recency decay.

        Quotes name the checklist items they support via `supports`; each
        item's strength is its strongest backing source times its recency
        decay at scoring time. Items with no backing quote score 0, so
        untagged truth cannot inflate. Both timestamps ride in the stored
        record, so revalidation recomputes identical strengths forever.

        A quote must also *address* the claim it is tagged for: the central
        requirements in ``evidence.quote_addresses_claim`` are checked here, so
        a press release that names no technology cannot carry a stack claim and
        a forum remark about culture cannot carry independent validation.
        Products only supply their own vocabulary through ``evidence_terms``.
        """
        categories: dict[str, set[str]] = {}
        for quote in quotes:
            backed = quote.get("supports", []) if isinstance(quote, dict) else getattr(quote, "supports", [])
            if not isinstance(backed, list):
                continue
            quote_text = quote.get("text", "") if isinstance(quote, dict) else getattr(quote, "text", "")
            quote_start = quote.get("start", 0) if isinstance(quote, dict) else getattr(quote, "start", 0)
            category = ""
            if text:
                category = evidence.section_category_at(text, int(quote_start or 0))
            if not category and source_uri:
                category = evidence.classify_source_category(source_uri).upper()
            for item_id in backed:
                if not isinstance(item_id, str):
                    continue
                # The quote must state the claim, and the source must be one that
                # may carry it. Nothing else is averaged in at any weight: a
                # source that cannot carry a claim is a lead, not evidence.
                addressed, _reason = contracts.quote_addresses_claim(
                    item_id, str(quote_text or ""), category, self.evidence_terms
                )
                if not addressed:
                    continue
                categories.setdefault(item_id, set()).add(category)
        strengths: dict[str, float] = {}
        for item_id, seen in categories.items():
            strength = contracts.support_strength(item_id, seen)
            if strength > 0:
                strengths[item_id] = strength * self.source_weight(source_uri) * self.recency_decay(
                    item_id, captured_at, scored_at
                )
        return strengths

    def support_notes(
        self, quotes: list[Any], text: str | None = None, source_uri: str | None = None
    ) -> dict[str, str]:
        """Why a tagged quote was refused, per item, for the evidence readout."""
        notes: dict[str, str] = {}
        for quote in quotes:
            backed = quote.get("supports", []) if isinstance(quote, dict) else getattr(quote, "supports", [])
            if not isinstance(backed, list):
                continue
            quote_text = quote.get("text", "") if isinstance(quote, dict) else getattr(quote, "text", "")
            quote_start = quote.get("start", 0) if isinstance(quote, dict) else getattr(quote, "start", 0)
            category = evidence.section_category_at(text or "", int(quote_start or 0))
            if not category and source_uri:
                category = evidence.classify_source_category(source_uri).upper()
            for item_id in backed:
                if not isinstance(item_id, str):
                    continue
                addressed, reason = evidence.quote_addresses_claim(
                    item_id, str(quote_text or ""), category, self.evidence_terms
                )
                if not addressed:
                    notes[item_id] = reason
        return notes

    def derive_checklist_score(
        self,
        answers: dict[str, Any],
        strengths: dict[str, float] | None = None,
    ) -> int:
        """Compute the 0-100 score for evidence-bound checklist answers.

        Each true answer contributes its configured points scaled by its
        support strength (source weight of its strongest backing quote).
        strengths=None preserves legacy unweighted scoring; an explicit
        mapping (even empty) prices untagged truth at zero, so omitting
        supports tags cannot inflate. Missing answers count as false,
        halves round up, and the total caps at 100. Answers must be strict
        booleans and keys must be configured items: anything else is a
        worker error, not a judgment call.
        """
        if self.checklist is None:
            raise ValueError("task defines no checklist")
        if not isinstance(answers, dict):
            raise ValueError("checklist answers must be an object")
        unknown = [key for key in answers if key not in self.checklist]
        if unknown:
            raise ValueError(f"unknown checklist items: {sorted(str(key) for key in unknown)}")
        total = 0.0
        for item_id, value in answers.items():
            if value is True:
                strength = 1.0 if strengths is None else float(strengths.get(item_id, 0.0))
                total += self.checklist[item_id] * strength
            elif value is not False:
                raise ValueError(f"checklist item '{item_id}' must be true or false")
        return min(100, math.floor(total + 0.5))

    def revision_payload(self) -> dict[str, Any]:
        """Canonical digest input: full spec minus defaulted calibration.

        The revision id is behavior identity: default calibration behaves
        exactly like a task created before calibration existed, so legacy
        stored revisions keep validating after upgrade.
        """
        """Canonical digest input: full spec minus defaulted calibration."""
        payload = self.model_dump(mode="json", by_alias=True)
        if self.checklist is None:
            payload.pop("checklist", None)
        if self.pass_score is None:
            payload.pop("pass_score", None)
        if not self.evidence_terms:
            payload.pop("evidence_terms", None)
        if self.candidate_top_n == TaskSpec.model_fields["candidate_top_n"].default:
            payload.pop("candidate_top_n", None)
        if not self.source_weights:
            payload.pop("source_weights", None)
        if self.default_source_weight == TaskSpec.model_fields["default_source_weight"].default:
            payload.pop("default_source_weight", None)
        if not self.recency_half_lives:
            payload.pop("recency_half_lives", None)
        return payload

    def derive_passed(self, score: Any) -> bool:
        """Derive the pass boolean for a numeric score and pass_score."""
        if self.pass_score is None:
            raise ValueError("task defines no pass_score")
        try:
            value = float(score)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"score {score!r} is not numeric") from exc
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124  # NaN check is intentional
            raise ValueError(f"score {score!r} is not finite")
        return value >= self.pass_score

    def with_derived_claims(
        self,
        claims: dict[str, JsonValue],
        strengths: dict[str, float] | None = None,
    ) -> dict[str, JsonValue]:
        """Fill computed score, fit_tier, and passed claims the worker omitted.

        The worker answers the checklist; calibration stays in code. Fields
        the worker did supply are never overwritten (validate_claims rejects
        mismatches instead). Raises ValueError for non-numeric supplied
        scores that derivation depends on.
        """
        if not isinstance(claims, dict):
            raise ValueError("claims must be an object")
        props = self.claims_schema.get("properties", {}) if isinstance(self.claims_schema, dict) else {}
        derived = dict(claims)
        if (
            self.checklist is not None
            and isinstance(derived.get("checklist"), dict)
            and "score" in props
            and "score" not in derived
        ):
            derived["score"] = self.derive_checklist_score(derived["checklist"], strengths)  # type: ignore[arg-type]
        if "score" in derived and "fit_tier" in props and "fit_tier" not in derived:
            derived["fit_tier"] = score_to_fit_tier(derived["score"])
        if (
            self.pass_score is not None
            and "score" in derived
            and "passed" in props
            and "passed" not in derived
        ):
            derived["passed"] = self.derive_passed(derived["score"])
        return derived

    @staticmethod
    def _backed_items(quotes: list[Any] | None) -> set[str]:
        """Checklist item ids named in any quote's supports list."""
        backed: set[str] = set()
        for quote in quotes or []:
            if isinstance(quote, dict):
                names = quote.get("supports", [])
            else:
                names = getattr(quote, "supports", [])
            if isinstance(names, list):
                backed.update(name for name in names if isinstance(name, str))
        return backed

    def validate_claims(
        self,
        claims: dict[str, JsonValue],
        quotes: list[Any] | None = None,
        strengths: dict[str, float] | None = None,
    ) -> None:
        errors = sorted(
            Draft202012Validator(self.claims_schema).iter_errors(claims),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ValueError(errors[0].message)
        if not isinstance(claims, dict):
            return
        props = self.claims_schema.get("properties", {}) if isinstance(self.claims_schema, dict) else {}
        # Deterministic checklist/score consistency: when a task configures a
        # checklist, a supplied score must equal the derived total. The score
        # is computed, not judged; with_derived_claims fills it when omitted.
        if self.checklist is not None and "checklist" in claims:
            answers = claims.get("checklist")
            if isinstance(answers, dict):
                if any(key not in self.checklist for key in answers):
                    raise ValueError("claims contain unknown checklist items")
                # Linkage checks apply once any quote names support. Records
                # written before supports existed carry empty tags and keep
                # validating unweighted; untagged true answers score nothing
                # through strengths, so dodging the tags cannot inflate.
                backed = self._backed_items(quotes) if quotes else set()
                if backed:
                    unknown_links = [name for name in backed if name not in self.checklist]
                    if unknown_links:
                        raise ValueError(f"quotes support unknown checklist items: {sorted(unknown_links)}")
                    unsupported = sorted(
                        item_id
                        for item_id, value in answers.items()
                        if value is True and item_id not in backed
                    )
                    if unsupported:
                        raise ValueError(
                            f"true checklist items lack a supporting quote: {unsupported}"
                        )
                if "score" in claims and "score" in props:
                    try:
                        expected_score: Any = self.derive_checklist_score(
                            answers, strengths if backed else None
                        )
                    except ValueError:
                        expected_score = None
                    if expected_score is not None and claims.get("score") != expected_score:
                        raise ValueError(
                            f"score {claims.get('score')!r} is inconsistent with the checklist "
                            f"(expected {expected_score})"
                        )
        # Deterministic score/passed consistency: a supplied boolean must
        # equal the pass_score derivation.
        if (
            self.pass_score is not None
            and "passed" in claims
            and "passed" in props
            and "score" in claims
            and isinstance(claims.get("passed"), bool)
        ):
            try:
                expected_passed: Any = self.derive_passed(claims.get("score"))
            except ValueError:
                expected_passed = None
            if expected_passed is not None and claims.get("passed") is not expected_passed:
                raise ValueError(
                    f"passed {claims.get('passed')!r} is inconsistent with score "
                    f"{claims.get('score')!r} (expected {expected_passed})"
                )
        # Deterministic score/tier consistency: when a task collects both a
        # numeric score and a fit_tier, the tier must equal
        # score_to_fit_tier(score). The tier is derived, not judged twice.
        if isinstance(claims, dict) and "score" in claims and "fit_tier" in claims:
            tier = claims.get("fit_tier")
            if isinstance(tier, str) and tier in TIER_SET:
                try:
                    expected = score_to_fit_tier(claims.get("score"))
                except ValueError:
                    expected = None
                if expected is not None and tier != expected:
                    raise ValueError(
                        f"fit_tier '{tier}' is inconsistent with score "
                        f"{claims.get('score')!r} (expected '{expected}')"
                    )

    def validate_extracted_item(self, item: Any) -> None:
        """Re-validate a stored ``ExtractedItem`` with the strengths it was derived from.

        The score-consistency branch recomputes the expected total; without the
        per-quote source weight and recency factors it would reject every
        legitimately weighted record. Callers that only hold the stored item
        (batch completion, export, DAG) must route through here rather than
        calling ``validate_claims`` bare.
        """
        if isinstance(item, dict):
            quotes = item.get("quotes") or []
            source_uri = item.get("source_uri")
            captured_at = item.get("captured_at")
            scored_at = item.get("scored_at")
            claims = item.get("claims")
        else:
            quotes = getattr(item, "quotes", None) or []
            source_uri = getattr(item, "source_uri", None)
            captured_at = getattr(item, "captured_at", None)
            scored_at = getattr(item, "scored_at", None)
            claims = getattr(item, "claims", None)
        strengths = self.support_strengths(quotes, source_uri, captured_at, scored_at)
        self.validate_claims(claims, quotes=quotes, strengths=strengths)  # type: ignore[arg-type]

    def render_instructions(self) -> str:
        """Fixed form-fill template plus the task-specific direction.

        Workers never interpret free-form rule prose: the template carries
        the invariant contract and instructions names only the outcome.
        """
        note = (self.instructions or "").strip()
        if note and note != DEFAULT_INSTRUCTIONS:
            return f"{INSTRUCTION_TEMPLATE}\nTask direction: {note}"
        return INSTRUCTION_TEMPLATE

    def render_worker_guide(self) -> str:
        """Deterministic field-filling rules for worker models, derived from this spec.

        Mechanical checks (quote length, offset requirements, score/tier
        consistency) live in code; this renders the exact words the worker
        sees so it does not have to guess numbers the verifier already knows.
        Keep it brace-free prose: providers locate the task payload by
        scanning for JSON objects.
        """
        lines = [
            "FIELD RULES - verified mechanically; violations rotate to the next route:",
            f"- Quote exactly: copy character-exact text from ONE named slice, at least {self.min_quote_chars} characters.",
            "- Offsets: if the quote text occurs exactly once in that slice, start/end may be omitted. "
            "If it occurs more than once, exact start/end are REQUIRED.",
        ]
        props = self.claims_schema.get("properties", {}) if isinstance(self.claims_schema, dict) else {}
        if self.checklist:
            points = ", ".join(f"{item_id} {pts}pts" for item_id, pts in sorted(self.checklist.items()))
            lines.append(
                "- Checklist decides the score: answer every item true or false. "
                "Tag each quote with supports naming the items it backs; every true item "
                "needs at least one supporting quote. Points: "
                f"{points}. The pipeline sums true-item points for the 0-100 score, "
                "scaled by source weight."
            )
            if self.recency_half_lives:
                decaying = ", ".join(
                    f"{item_id} {hl:g}d" for item_id, hl in sorted(self.recency_half_lives.items())
                )
                lines.append(
                    "- Evidence decays: support halves every half-life, so cite the freshest "
                    f"quotes. Half-lives in days: {decaying}."
                )
            computed = [name for name in ("score", "fit_tier", "passed") if name in props]
            if computed:
                lines.append(
                    f"Computed in the pipeline, never judged - omit: {', '.join(computed)}."
                )
        if self.evidence_terms:
            lines.append(
                "- Evidence candidates: sections list numbered candidate spans ranked by term overlap. "
                "Cite candidate_id and copy the span text exactly; offsets may then be omitted. "
                "Sections with no candidates need free quotes with offsets only when repeated."
            )
        if "score" in props and "fit_tier" in props:
            lines.append(
                "- Tiers are derived from score in the pipeline, never judged: tier_1 for "
                "85-100, tier_2 for 70-84, tier_3 for 50-69, unfit below 50. Do not send "
                "fit_tier; the pipeline fills it from the score."
            )
        field_notes = [
            f"{name} - {spec['description']}"
            for name, spec in props.items()
            if isinstance(spec, dict) and spec.get("description")
        ]
        if field_notes:
            lines.append("FIELDS: " + " | ".join(field_notes))
        lines.append("Return JSON only.")
        return "\n".join(lines)

    def render_prompt(self, items: list[dict[str, Any]]) -> str:
        import json

        from .candidates import attach_candidates

        prompt_items = (
            attach_candidates(items, self.evidence_terms, self.candidate_top_n)
            if self.evidence_terms
            else items
        )
        contract = {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["item_id", "claims", "quotes"],
                        "properties": {
                            "item_id": {"type": "string"},
                            "claims": self.claims_schema,
                            "quotes": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["slice_id", "text"],
                                    "properties": {
                                        "slice_id": {"type": "string"},
                                        "start": {"type": "integer", "minimum": 0},
                                        "end": {"type": "integer", "minimum": 1},
                                        "candidate_id": {"type": "integer", "minimum": 0},
                                        "supports": {"type": "array", "items": {"type": "string"}},
                                        "text": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        payload = {"output_schema": contract, "input_items": prompt_items}
        return f"{self.render_instructions()}\n{self.render_worker_guide()}\n{json.dumps(payload, ensure_ascii=False)}"


class RoutePolicy(ClosedModel):
    allowed_transports: list[str] | None = Field(default=None, description="Optional allowlist of harness-fleet transport names")
    excluded_transports: list[str] = Field(default_factory=list, description="Blocklist of harness-fleet transport names")
    allowed_providers: list[str] | None = Field(default=None, description="Deprecated alias for allowed_transports")
    excluded_providers: list[str] = Field(default_factory=list, description="Deprecated alias for excluded_transports")
    allowed_routes: list[str] | None = Field(default=None, description="Optional allowlist of route IDs")
    excluded_routes: list[str] = Field(default_factory=list, description="Blocklist of route IDs")
    zdr: bool = Field(default=False, description="Enforce Zero Data Retention on upstream providers")
    allow_data_collection: bool = Field(default=True, description="Whether providers may collect request data")
    max_cost_per_1k_input: float = Field(default=0.0, ge=0, description="Max allowed cost per 1k input tokens")
    max_cost_per_1k_output: float = Field(default=0.0, ge=0, description="Max allowed cost per 1k output tokens")
    max_request_cost: float | None = Field(default=None, ge=0, description="Max allowed spend per single request")
    free_only: bool = Field(default=False, description="Explicit flag to restrict to observed-zero routes only")
    note: str | None = Field(default=None, description="Operator-recorded reason for a routing choice (e.g. trust basis for an approved paid route)")
    openrouter_providers: list[str] | None = Field(default=None, description="Upstream OpenRouter providers to prioritize")
    openrouter_ignore: list[str] = Field(default_factory=list, description="Upstream OpenRouter providers to ignore")
    openrouter_order: list[str] | None = Field(default=None, description="Upstream OpenRouter provider ordering")
    openrouter_allow_fallbacks: bool = Field(default=True, description="Whether OpenRouter may fall back to other providers")

    @model_validator(mode="after")
    def sync_transports_and_providers(self) -> RoutePolicy:
        if self.allowed_providers and not self.allowed_transports:
            self.allowed_transports = list(self.allowed_providers)
        elif self.allowed_transports and not self.allowed_providers:
            self.allowed_providers = list(self.allowed_transports)
        if self.excluded_providers and not self.excluded_transports:
            self.excluded_transports = list(self.excluded_providers)
        elif self.excluded_transports and not self.excluded_providers:
            self.excluded_providers = list(self.excluded_transports)
        return self


class ProviderReceipt(ClosedModel):
    id: str
    session_id: str | None = None
    provider: str
    requested_route: str
    status: Literal["complete", "failed"]
    cost: float | None = Field(default=None, ge=0)
    cost_status: Literal["reported_zero", "billed", "unknown"] = "unknown"
    usage: dict[str, JsonValue] | None = None
    error: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    error_type: Literal["rate_limit", "transient_http", "inference_error", "auth_error", "timeout", "unsupported"] | None = None
    retry_after: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent_cost(self) -> ProviderReceipt:
        expected = "unknown" if self.cost is None else ("reported_zero" if self.cost == 0 else "billed")
        if self.cost_status != expected:
            raise ValueError(f"cost_status must be '{expected}' for cost={self.cost}")
        return self


class MalformedRouteError(ValueError):
    """A harness route id failed structural validation."""


class RouteId(ClosedModel):
    """Parsed harness route identity: provider plus optional model.

    Both legacy spellings (``provider/model`` and ``provider:model``) parse
    to the same value for simple ids; when both separators appear, ``/``
    wins (historical rule, preserved). Bare provider ids carry
    ``model=None`` — adapters whose spec ignores the model accept them,
    others reject them in ``derive_model``. Storage and comparison keep
    using raw strings; this type validates at parse boundaries only.
    """

    provider: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    @classmethod
    def parse(cls, route_id: str) -> RouteId:
        """Split a route id with validation.

        Blank ids and ids with an empty provider or model part raise
        ``MalformedRouteError``; callers fail closed. This is the single
        place harness model derivation parses route ids, except OpenCode,
        which keeps its tested native-prefix rule as an override of
        ``derive_model``.
        """
        if not isinstance(route_id, str) or not route_id.strip():
            raise MalformedRouteError(f"malformed harness route id: {route_id!r}")
        text = route_id.strip()
        for separator in ("/", ":"):
            if separator in text:
                head, _, remainder = text.partition(separator)
                if head and remainder:
                    return cls(provider=head, model=remainder)
                raise MalformedRouteError(f"malformed harness route id: {route_id!r}")
        return cls(provider=text, model=None)


def coerce_receipt(value: ProviderReceipt | dict[str, Any]) -> ProviderReceipt:
    """Carrier coercion for the live provider path.

    Parser-mutated models round-trip through JSON because pydantic does not
    revalidate model instances by default; foreign plain dicts (third-party
    providers) validate directly. Raises ValidationError on invalid
    receipts; callers fail closed.
    """
    if isinstance(value, ProviderReceipt):
        return ProviderReceipt.model_validate(value.model_dump(mode="json"))
    return ProviderReceipt.model_validate(value)


class PacketAudit(ClosedModel):
    total_batches_processed: int = Field(ge=0)
    total_tokens_consumed: int = Field(ge=0)
    total_cost_reported: float = Field(ge=0)
    batches_verified: int = Field(ge=0)
    batches_failed: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    receipts_recorded: int = Field(ge=0)
    unknown_cost_attempts: int = Field(ge=0)


class CleanPacket(ClosedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE}/packet-v2.schema.json",
        }
    )
    schema_uri: Literal[f"{SCHEMA_BASE}/packet-v2.schema.json"] = Field(  # type: ignore[valid-type]
        default=f"{SCHEMA_BASE}/packet-v2.schema.json",
        alias="$schema",
    )
    format_version: Literal["harness_fleet_v2"] = "harness_fleet_v2"
    exported_at: str
    run_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    task: TaskSpec
    task_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_verified_records: int = Field(ge=0)
    audit: PacketAudit
    records: list[ExtractedItem]
    receipts: list[ProviderReceipt]
    policy: RoutePolicy | None = None

    @model_validator(mode="after")
    def consistent_record_count(self) -> CleanPacket:
        if self.total_verified_records != len(self.records):
            raise ValueError("total_verified_records does not match records")
        task_payload = self.task.revision_payload()
        task_digest = hashlib.sha256(
            json.dumps(task_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        if self.task_revision != task_digest:
            raise ValueError("task_revision does not match the embedded task")
        item_ids = [record.item_id for record in self.records]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("records contain duplicate item_id values")
        for record in self.records:
            self.task.validate_claims(
                record.claims,
                quotes=record.quotes,
                strengths=self.task.support_strengths(
                    record.quotes,
                    record.source_uri,
                    record.captured_at,
                    record.scored_at,
                ),
            )
        return self


class RouteInfo(ClosedModel):
    id: str
    provider: str | None = None
    enabled: bool
    price_state: Literal["candidate", "price_observed_zero", "unknown", "disabled"]
    auth: str | None = None
    cost_per_1k_input: float | None = Field(default=None, ge=0)
    cost_per_1k_output: float | None = Field(default=None, ge=0)
    last_verified: str | None = None
    verification_source: str | None = None
    last_price_observation: float | None = None
    disabled_reason: str | None = None
    disabled_at: float | None = None


class RoutesResult(ClosedModel):
    routes: list[RouteInfo]
    refresh: dict[str, int | str] | None = None


class WorkerSessionRecord(ClosedModel):
    session_id: str
    worker_idx: int = Field(ge=1)
    route_id: str
    provider: str
    status: Literal["active", "completed", "failed"]
    started_at: str
    last_active: str
    batches_completed: int = Field(ge=0)
    items_completed: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    reported_cost: float = Field(ge=0)
    errors: list[str]
    error_count: int = Field(default=0, ge=0)
    route_history: list[str] = Field(default_factory=list)


class BatchTestResult(ClosedModel):
    ok: bool
    results: list[ExtractedItem] | None = None
    receipt: ProviderReceipt | None = None
    error: str | None = None


class ValidationReport(ClosedModel):
    valid: bool
    task: str
    input_items: int
    batches: int
    # Non-fatal findings the caller must understand before trusting grounding
    # (for example items sliced into partial windows). ``valid`` stays true
    # because the run can proceed; a non-empty list means "read this first".
    errors: list[str] = Field(default_factory=list)


class TaskRegistrationResult(ClosedModel):
    task: str
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProfileResult(ClosedModel):
    profile_kind: Literal["ideal_company", "ideal_employer"]
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: dict[str, JsonValue]


class TaskSummary(ClosedModel):
    task_name: str
    revision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    #: 1 when the revision carries a checklist, so its score is derivable.
    #: The store returns the SQLite flag as an int, and this model is strict.
    scorable: int = Field(default=0, ge=0, le=1)


class TasksResult(ClosedModel):
    tasks: list[TaskSummary]
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent_count(self) -> TasksResult:
        if self.count != len(self.tasks):
            raise ValueError("task count does not match tasks")
        return self


class DoctorCheck(ClosedModel):
    name: str
    ok: bool
    detail: str


class DoctorReport(ClosedModel):
    ready: bool
    database: str
    checks: list[DoctorCheck]


class ScoreHistoryRound(ClosedModel):
    run_id: str
    item_id: str
    entity: str
    score: float
    created_at: str
    parent_run_id: str | None = None


class EntityHistoryReport(ClosedModel):
    entity: str
    rounds: list[ScoreHistoryRound] = Field(default_factory=list)


class ErrorSummary(ClosedModel):
    mse: float
    mae: float


class CalibrationReport(ClosedModel):
    task: str
    route: str
    scored_at: str
    params: list[str] = Field(default_factory=list)
    sweeps: int = Field(ge=0)
    n_train: int = Field(ge=0)
    n_holdout: int = Field(ge=0)
    n_skipped: int = Field(ge=0)
    skipped: dict[str, int] = Field(default_factory=dict)
    baseline: ErrorSummary
    fitted_train: ErrorSummary
    fitted_holdout: ErrorSummary | None = None
    points: dict[str, int] = Field(default_factory=dict)
    points_float: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    default_source_weight: float = 1.0
    halves: dict[str, float] = Field(default_factory=dict)
    applied: bool = False
    new_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SchemaResult(ClosedModel):
    kind: Literal["task", "input", "candidate-output", "output", "packet", "profile", "database"]
    schema_document: dict[str, JsonValue]


class SetupAction(ClosedModel):
    kind: Literal["skill", "database", "routes"]
    status: Literal["planned", "created", "updated", "unchanged", "skipped"]
    path: str | None = None
    detail: str | None = None


class StdioServerConfig(ClosedModel):
    command: str
    args: list[str]


class SetupReport(ClosedModel):
    ready: bool
    scope: Literal["user", "project"]
    workspace_root: str
    database: str
    skill_path: str
    actions: list[SetupAction]
    stdio_server: StdioServerConfig
    route_refresh: dict[str, int | str] | None = None
    next_commands: list[list[str]]


class BatchStatusCounts(ClosedModel):
    total: int = Field(default=0, ge=0)
    verified: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    leased: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class RouteStatusSummary(ClosedModel):
    route_id: str
    provider: str
    attempts: int = Field(default=0, ge=0)
    verified: int = Field(default=0, ge=0)
    rate_limits: int = Field(default=0, ge=0)
    success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_latency_seconds: float = Field(default=0.0, ge=0.0)
    reported_cost: float = Field(default=0.0, ge=0.0)


class RunStatusReport(ClosedModel):
    run_id: str
    status: str
    task_name: str
    profile_revision_id: str | None = None
    total_items: int = Field(default=0, ge=0)
    verified_items: int = Field(default=0, ge=0)
    batches: BatchStatusCounts
    attempts_used: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=0, ge=0)
    rate_limits_encountered: int = Field(default=0, ge=0)
    routes: list[RouteStatusSummary] = Field(default_factory=list)
    active_workers: int = Field(default=0, ge=0)
    recent_errors: list[str] = Field(default_factory=list)


class RouteEvalResult(ClosedModel):
    route_id: str
    provider: str
    total_samples: int = Field(ge=0)
    schema_pass_count: int = Field(ge=0)
    grounding_pass_count: int = Field(ge=0)
    correct_count: int | None = Field(default=None, ge=0)
    rate_limit_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    schema_pass_rate: float = Field(ge=0.0, le=1.0)
    grounding_pass_rate: float = Field(ge=0.0, le=1.0)
    accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    avg_latency_seconds: float = Field(ge=0.0)
    composite_score: float = Field(ge=0.0, le=1.0)


class RouteEvalReport(ClosedModel):
    task: str
    samples: int = Field(ge=0)
    evaluated_at: str
    routes: list[RouteEvalResult] = Field(default_factory=list)


class CooldownDetail(ClosedModel):
    route_id: str
    cooldown_until: str
    reason: str | None = None
    seconds_remaining: float = Field(default=0.0, ge=0.0)


class CooldownsReport(ClosedModel):
    action: str
    count: int = Field(default=0, ge=0)
    cooldowns: list[CooldownDetail] = Field(default_factory=list)
    cleared: int | None = None
    route_id: str | None = None


CLAIM_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]*$"

FilterScalar = str | int | float | bool | None


class FilterOp(str, Enum):
    """Typed comparison operators. Serializes as its symbol (``==``, ``in``)."""

    EQ = "=="
    NE = "!="
    GTE = ">="
    LTE = "<="
    GT = ">"
    LT = "<"
    IN = "in"
    NOT_IN = "not_in"


class FilterClause(ClosedModel):
    """One typed predicate on a claim field. ``IN``/``NOT_IN`` take a list value."""

    field: str = Field(pattern=CLAIM_KEY_PATTERN)
    op: FilterOp = FilterOp.EQ
    value: FilterScalar | list[FilterScalar] = None

    @model_validator(mode="after")
    def check_value_shape(self) -> FilterClause:
        if self.op in (FilterOp.IN, FilterOp.NOT_IN):
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"filter op '{self.op.value}' needs a non-empty list value")
        elif isinstance(self.value, list):
            raise ValueError(f"filter op '{self.op.value}' needs a scalar value, not a list")
        return self


class ClaimFilter(ClosedModel):
    """Typed replacement for the ``filter_expr`` mini-language.

    Disjunctive normal form with an optional top-level AND-group: a record
    matches when every clause in ``all`` passes AND (``any`` is empty OR at
    least one branch passes). The string parser maps each ``||`` branch to
    one nested ``ClaimFilter(all=[...])`` entry in ``any``, so parsed and
    hand-built filters share identical semantics.
    """

    all: list[FilterClause] = Field(default_factory=list)
    any: list[ClaimFilter] = Field(default_factory=list)


class SortSpec(ClosedModel):
    """Typed replacement for ``sort_by`` + ``descending``."""

    field: str = Field(pattern=CLAIM_KEY_PATTERN)
    descending: bool = True

class RegistryDomain(ClosedModel):
    """One domain the taxonomy has promoted, and why."""

    domain: str
    category: str
    reason: str = ""


class SourceProposal(ClosedModel):
    """A candidate the growth pass suggests a category for."""

    domain: str
    category: str
    confidence: float
    sightings: int


class SourceChannel(ClosedModel):
    """A channel this workspace defines in its sources directory."""

    name: str
    category: str
    path: str = ""


class SourcesReport(ClosedModel):
    """The source taxonomy as it stands: known, proposed, installed."""

    registry: str
    domains: list[RegistryDomain] = []
    candidate_count: int = 0
    proposals: list[SourceProposal] = []
    channels: list[SourceChannel] = []



class LaneYield(ClosedModel):
    """One source's contribution to a run: what it returned, what it cost."""

    source: str
    kind: str = "backend"
    captured: int = 0
    attempted: int | None = None
    skipped: int = 0


class LaneCoverage(ClosedModel):
    """One entity's evidence kinds, and what its bar is still missing."""

    item_id: str
    kinds: list[str] = []
    missing: list[str] = []
    claimed_tier: str | None = None


class LaneSupport(ClosedModel):
    """One entity's scored claims: carried by a qualifying source, or refused."""

    item_id: str
    supported: list[str] = []
    refused: list[str] = []
    reasons: list[str] = []


class LaneTruthQuote(ClosedModel):
    """One sampled quote, re-checked against its offsets and its live page."""

    item_id: str
    uri: str = ""
    category: str = ""
    exact: bool = False
    live: str = "not_checked"
    addressed: bool = False
    reason: str = ""


class LaneCost(ClosedModel):
    """What the run spent: routes, attempts, cost and wall time."""

    routes: list[str] = []
    attempts: int = 0
    cost: float = 0.0
    wall_seconds: float | None = None
    batches_verified: int = 0
    batches_failed: int = 0


class LaneReport(ClosedModel):
    """The five measurements a lane is tuned by, for one finished run."""

    run_id: str
    lane: str = ""
    tier_bar: list[str] = []
    generated_at: str = ""
    records: int = 0
    yield_by_source: list[LaneYield] = []
    coverage: list[LaneCoverage] = []
    coverage_meeting_bar: float = 0.0
    support: list[LaneSupport] = []
    claims_supported: int = 0
    claims_refused: int = 0
    truth: list[LaneTruthQuote] = []
    truth_sampled: int = 0
    cost: LaneCost = LaneCost()
    frozen: dict[str, str] = {}
    notes: list[str] = []

class LaneSummary(ClosedModel):
    """One lane a person or an assistant can pick."""

    name: str
    description: str = ""
    preset: str = ""
    #: Empty when the lane is not judged by the tier ladder.
    tier: str = ""
    top: int = 0
    title_include: list[str] = []
    remote: bool = False
    source: str = "shipped"


class LaneAvailability(ClosedModel):
    """One lane this workspace can run, with where it came from."""

    name: str
    description: str = ""
    preset: str = ""
    #: The ladder floor, or empty when the lane gates on ``require_kinds``.
    tier: str = ""
    require_kinds: list[str] = []
    queries: int = 0
    backends: list[str] = []
    top: int = 0
    source: str = "shipped"
    #: The bar one line, as the CLI prints it: a floor or a kind list.
    bar: str = ""


class LanesReport(ClosedModel):
    """What this install can be asked to do, without installing anything else."""

    default: str = ""
    lanes: list[LaneSummary] = []
    workspace_lanes_dir: str = ""


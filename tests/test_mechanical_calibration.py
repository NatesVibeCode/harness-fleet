"""Checklist calibration, evidence candidates, and instruction templating.

The worker answers evidence-bound booleans and cites numbered candidate
spans; the pipeline computes score/tier/passed and resolves offsets. These
tests pin that division: judgment stays small and checkable, arithmetic
stays in code.
"""
import json

import pytest

from harness_fleet.candidates import (
    extract_candidates,
    merge_terms,
    normalize_terms,
    profile_evidence_terms,
)
from harness_fleet.grounding import normalize_grounding
from harness_fleet.models import QuoteCandidate, TaskSpec
from harness_fleet.task import (
    create_task_from_preset,
    parse_half_life,
    parse_source_weight,
)


def _checklist_task(**overrides):
    kwargs = {
        "name": "check",
        "claims_schema": {
            "type": "object",
            "properties": {
                "checklist": {
                    "type": "object",
                    "properties": {
                        "alpha": {"type": "boolean"},
                        "beta": {"type": "boolean"},
                    },
                    "required": ["alpha", "beta"],
                    "additionalProperties": False,
                },
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "fit_tier": {"enum": ["tier_1", "tier_2", "tier_3", "unfit"]},
                "passed": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["checklist", "reason"],
            "additionalProperties": False,
        },
        "checklist": {"alpha": 60, "beta": 50},
        "pass_score": 70,
    }
    kwargs.update(overrides)
    return TaskSpec(**kwargs)


# --- checklist derivation ----------------------------------------------------

def test_derive_checklist_score_sums_true_points_and_caps():
    task = _checklist_task()
    assert task.derive_checklist_score({"alpha": True, "beta": False}) == 60
    assert task.derive_checklist_score({"alpha": False, "beta": False}) == 0
    # 60 + 50 = 110 caps at 100
    assert task.derive_checklist_score({"alpha": True, "beta": True}) == 100
    # Missing answers count as false
    assert task.derive_checklist_score({}) == 0


def test_derive_checklist_score_rejects_unknown_and_non_bool():
    task = _checklist_task()
    with pytest.raises(ValueError, match="unknown checklist items"):
        task.derive_checklist_score({"alpha": True, "gamma": True})
    with pytest.raises(ValueError, match="must be true or false"):
        task.derive_checklist_score({"alpha": 1, "beta": False})
    with pytest.raises(ValueError, match="must be an object"):
        task.derive_checklist_score(["alpha"])
    with pytest.raises(ValueError, match="no checklist"):
        TaskSpec(name="plain").derive_checklist_score({"alpha": True})


def test_derive_passed_uses_threshold():
    task = _checklist_task()
    assert task.derive_passed(70) is True
    assert task.derive_passed(69.9) is False
    assert task.derive_passed(100) is True
    with pytest.raises(ValueError, match="not numeric"):
        task.derive_passed("high")
    with pytest.raises(ValueError, match="no pass_score"):
        TaskSpec(name="plain").derive_passed(90)


def test_with_derived_claims_fills_score_tier_and_passed():
    task = _checklist_task()
    derived = task.with_derived_claims({
        "checklist": {"alpha": True, "beta": False},
        "reason": "r",
    })
    assert derived["score"] == 60
    assert derived["fit_tier"] == "tier_3"
    assert derived["passed"] is False
    # Worker-supplied fields are never overwritten
    supplied = {"checklist": {"alpha": True, "beta": True}, "score": 100,
                "fit_tier": "tier_1", "passed": True, "reason": "r"}
    assert task.with_derived_claims(supplied) == supplied
    # Tasks without calibration config pass claims through untouched
    plain = TaskSpec(name="plain")
    claims = {"summary": "s"}
    assert plain.with_derived_claims(claims) == claims


def test_validate_claims_rejects_mismatched_derived_fields():
    task = _checklist_task()
    good = {"checklist": {"alpha": True, "beta": False}, "score": 60,
            "fit_tier": "tier_3", "passed": False, "reason": "r"}
    task.validate_claims(good)
    bad_score = dict(good, score=61)
    with pytest.raises(ValueError, match="inconsistent with the checklist"):
        task.validate_claims(bad_score)
    bad_passed = dict(good, passed=True)
    with pytest.raises(ValueError, match="inconsistent with score"):
        task.validate_claims(bad_passed)
    # A loose checklist schema lets unknown keys reach the explicit guard
    # (closed schemas reject them first, which is also correct).
    loose_props = dict(task.claims_schema["properties"])
    loose_props["checklist"] = {"type": "object"}
    loose = _checklist_task(claims_schema={**task.claims_schema, "properties": loose_props})
    with pytest.raises(ValueError, match="unknown checklist items"):
        loose.validate_claims({"checklist": {"alpha": True, "zzz": True}, "reason": "r"})


def test_task_spec_rejects_dangling_checklist_config():
    with pytest.raises(ValueError, match="requires a 'checklist' object property"):
        TaskSpec(name="bad", checklist={"alpha": 10})
    with pytest.raises(ValueError, match="must not be empty"):
        _checklist_task(checklist={})
    with pytest.raises(ValueError, match="positive integer"):
        _checklist_task(checklist={"alpha": 0})
    with pytest.raises(ValueError, match="non-empty strings"):
        _checklist_task(evidence_terms=["ok", "  "])


# --- instruction template ----------------------------------------------------

def test_render_instructions_template_and_direction():
    task = TaskSpec(name="t", instructions="Do it.")
    rendered = task.render_instructions()
    assert rendered.startswith("Form-fill contract:")
    assert "Task direction: Do it." in rendered
    default_task = TaskSpec(name="t")
    assert default_task.render_instructions() == default_task.render_instructions()
    assert "Task direction" not in default_task.render_instructions()
    assert "Return JSON only." in default_task.render_instructions()


# --- worker guide ------------------------------------------------------------

def test_guide_names_checklist_points_and_computed_fields():
    task = _checklist_task(evidence_terms=["alpha"])
    guide = task.render_worker_guide()
    assert "alpha 60pts" in guide and "beta 50pts" in guide
    assert "omit: score, fit_tier, passed" in guide
    assert "candidate_id" in guide


def test_score_preset_is_checklist_shaped():
    task = create_task_from_preset("s", preset_name="score")
    assert task.checklist == {"initiative_named": 40, "criteria_evidence": 35, "supporting_signals": 25}
    assert "score" not in task.claims_schema["required"]
    assert task.evidence_terms, "score preset must expose candidate terms"
    research = create_task_from_preset("r", preset_name="account-research")
    assert "fit_tier" not in research.claims_schema["required"]
    assert research.derive_checklist_score(
        {"explicit_initiative": True, "stack_confirmed": True,
         "hiring_or_trigger": True, "firmographic_fit": True}) == 100


# --- candidates --------------------------------------------------------------

TEXT = (
    "Acme is migrating its platform to Kubernetes this quarter. "
    "The team opened senior roles in Berlin after the funding round. "
    "Quiet steady operations continue with nothing else to report here."
)


def test_extract_candidates_ranking_offsets_and_determinism():
    first = extract_candidates(TEXT, ["migrating", "kubernetes", "funding", "platform"], top_n=6)
    second = extract_candidates(TEXT, ["MIGRATING", " Kubernetes ", "funding", "funding"], top_n=6)
    assert first == second, "term normalization must be deterministic"
    assert [s["candidate_id"] for s in first] == list(range(len(first)))
    for span in first:
        assert TEXT[span["start"]:span["end"]] == span["text"]
    # Multi-hit sentence wins and ids follow position order
    assert first[0]["text"].startswith("Acme is migrating")
    assert len(first) == 2


def test_extract_candidates_edge_cases():
    assert extract_candidates(TEXT, []) == []
    assert extract_candidates(TEXT, None) == []
    assert extract_candidates("", ["x"]) == []
    assert extract_candidates(TEXT, ["berlin"], top_n=1)[0]["text"].startswith("The team opened")
    long_sentence = "word " * 200 + "migrating."
    assert extract_candidates(long_sentence, ["migrating"]) == [], "overlong spans are dropped"
    assert normalize_terms([" A ", "a", "", 7, "B"]) == ["a", "b"]
    assert merge_terms(["a", "b"], ["b", "c"], None) == ["a", "b", "c"]


def test_profile_evidence_terms_duck_typed():
    from harness_fleet.profile import IdealCompanyProfile

    profile = IdealCompanyProfile(
        required_stack=["Kubernetes", "Postgres"],
        trigger_pain_phrases=["slow deploys"],
        target_roles=["platform engineer"],
    )
    terms = profile_evidence_terms(profile)
    assert "kubernetes" in terms and "slow deploys" in terms and "platform engineer" in terms
    assert profile_evidence_terms(object()) == []
    assert profile_evidence_terms(None) == []


def test_attach_candidates_does_not_mutate_and_render_prompt_embeds():
    task = create_task_from_preset("s", preset_name="score")
    items = [{
        "item_id": "i1", "title": "T",
        "sections": [{"slice_id": "full", "start": 0, "end": len(TEXT), "text": TEXT}],
    }]
    prompt = task.render_prompt(items)
    assert items[0]["sections"][0].get("candidates") is None, "prompt items must be copies"
    from harness_fleet.providers.base import extract_task_payload

    payload = extract_task_payload(prompt)
    sections = payload["input_items"][0]["sections"]
    assert sections[0]["candidates"], "sections must carry candidate spans"
    assert task.render_instructions() in prompt


# --- candidate grounding -----------------------------------------------------

def _card(text):
    digest = "0" * 64
    return {
        "item_id": "i1",
        "source_digest": digest,
        "source_uri": None,
        "content_type": "text/plain",
        "slices": [{"slice_id": "full", "start": 0, "end": len(text), "text": text}],
    }


def _candidate(item_id="i1", **quote):
    base = {"slice_id": "full", "text": quote.pop("text"), **quote}
    return {"item_id": item_id, "claims": {"summary": "s"}, "quotes": [base]}


def test_candidate_id_resolves_offsets_without_model_offsets():
    terms = ["migrating", "kubernetes"]
    spans = extract_candidates(TEXT, terms, 6)
    target = spans[0]
    items, err = normalize_grounding(
        [_candidate(text=target["text"], candidate_id=target["candidate_id"])],
        [_card(TEXT)], evidence_terms=terms, candidate_top_n=6,
    )
    assert err is None, err
    quote = items[0].quotes[0]
    assert (quote.start, quote.end, quote.text) == (target["start"], target["end"], target["text"])


def test_candidate_id_accepts_normalized_copy_as_verbatim_span():
    terms = ["funding"]
    spans = extract_candidates(TEXT, terms, 6)
    assert spans, "funding sentence must be a candidate"
    target = spans[0]
    curly = target["text"].replace("'", "\u2019") if "'" in target["text"] else target["text"]
    items, err = normalize_grounding(
        [_candidate(text=curly, candidate_id=target["candidate_id"])],
        [_card(TEXT)], evidence_terms=terms, candidate_top_n=6,
    )
    assert err is None, err
    assert items[0].quotes[0].text == target["text"]


def test_candidate_id_failures_are_closed():
    terms = ["migrating", "kubernetes"]
    spans = extract_candidates(TEXT, terms, 6)
    # Unknown id
    _, err = normalize_grounding(
        [_candidate(text=spans[0]["text"], candidate_id=99)],
        [_card(TEXT)], evidence_terms=terms, candidate_top_n=6,
    )
    assert err and "unknown candidate" in err
    # Text that does not match the cited span
    _, err = normalize_grounding(
        [_candidate(text="Quiet steady operations continue with nothing else to report here.",
                     candidate_id=spans[0]["candidate_id"])],
        [_card(TEXT)], evidence_terms=terms, candidate_top_n=6,
    )
    assert err and "does not match cited candidate" in err
    # Cited id but the task exposes no terms
    _, err = normalize_grounding(
        [_candidate(text=spans[0]["text"], candidate_id=0)], [_card(TEXT)],
    )
    assert err and "no evidence candidates" in err
    # Legacy path without candidate_id still searches the whole slice
    items, err = normalize_grounding(
        [_candidate(text="Quiet steady operations continue with nothing else to report here.")],
        [_card(TEXT)], evidence_terms=terms, candidate_top_n=6,
    )
    assert err is None, err


def test_candidate_id_with_verbatim_offsets_still_accepted():
    terms = ["migrating", "kubernetes"]
    spans = extract_candidates(TEXT, terms, 6)
    target = spans[0]
    quote = QuoteCandidate(
        slice_id="full", text=target["text"],
        start=target["start"], end=target["end"],
        candidate_id=target["candidate_id"],
    )
    items, err = normalize_grounding(
        [{"item_id": "i1", "claims": {"summary": "s"},
          "quotes": [quote.model_dump(mode="json")]}],
        [_card(TEXT)], evidence_terms=terms, candidate_top_n=6,
    )
    assert err is None, err


# --- revision stability ------------------------------------------------------

def test_revision_payload_ignores_default_calibration():
    from harness_fleet.store import digest_json

    task = TaskSpec(name="t")
    legacy_shape = task.model_dump(mode="json", by_alias=True)
    for key in ("checklist", "pass_score", "evidence_terms", "candidate_top_n",
                "source_weights", "default_source_weight", "recency_half_lives"):
        legacy_shape.pop(key, None)
    assert digest_json(task.revision_payload()) == digest_json(legacy_shape)
    calibrated = _checklist_task()
    assert digest_json(calibrated.revision_payload()) != digest_json(legacy_shape)


def test_store_round_trips_calibration_and_keeps_legacy_digest(tmp_path):
    from harness_fleet.store import HarnessStore, digest_json

    store = HarnessStore(tmp_path / "s.db")
    calibrated = _checklist_task(evidence_terms=["alpha"])
    revision = store.register_task(calibrated)
    reloaded = store.get_task("check")
    assert reloaded.checklist == {"alpha": 60, "beta": 50}
    assert reloaded.pass_score == 70
    assert reloaded.evidence_terms == ["alpha"]
    assert digest_json(reloaded.revision_payload()) == revision
    # A pre-upgrade row (NULL calibration columns) rebuilds as defaults and
    # keeps the legacy digest, so old runs still resume and export.
    plain = TaskSpec(name="legacy")
    legacy_digest = digest_json({
        k: v for k, v in plain.model_dump(mode="json", by_alias=True).items()
        if k not in ("checklist", "pass_score", "evidence_terms", "candidate_top_n",
                     "source_weights", "default_source_weight", "recency_half_lives")
    })
    with store.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO task_revisions(
                revision_id,task_name,format_version,instructions,batch_size,
                max_slice_chars,min_quote_chars,claims_schema_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (legacy_digest, "legacy", plain.format_version, plain.instructions,
             plain.batch_size, plain.max_slice_chars, plain.min_quote_chars,
             json.dumps(plain.claims_schema, separators=(",", ":")), "2026-01-01T00:00:00+00:00"),
        )
    reloaded_legacy = store.get_task_revision(legacy_digest)
    assert reloaded_legacy.checklist is None
    assert reloaded_legacy.evidence_terms == []
    assert reloaded_legacy.candidate_top_n == 6
    assert digest_json(reloaded_legacy.revision_payload()) == legacy_digest


# --- source weights ----------------------------------------------------------

WEIGHTED_TASK_KWARGS = {
    "source_weights": {"boards.greenhouse.io": 1.0, "greenhouse.io": 0.5, "aggregator.example": 0.25},
    "default_source_weight": 1.0,
}


def test_source_weight_longest_match_and_default():
    task = _checklist_task(**WEIGHTED_TASK_KWARGS)
    assert task.source_weight("https://boards.greenhouse.io/acme/jobs/1") == 1.0
    assert task.source_weight("https://greenhouse.io/embed/2") == 0.5
    assert task.source_weight("https://AGGREGATOR.example/x") == 0.25
    assert task.source_weight("https://unknown.example/y") == 1.0
    assert task.source_weight(None) == 1.0
    assert task.source_weight("") == 1.0


def test_source_weight_validation():
    with pytest.raises(ValueError, match="between 0 and 1"):
        _checklist_task(source_weights={"a.example": 1.5})
    with pytest.raises(ValueError, match="non-empty strings"):
        _checklist_task(source_weights={"  ": 0.5})
    with pytest.raises(ValueError, match="must look like"):
        parse_source_weight("no-equals")
    with pytest.raises(ValueError, match="between 0 and 1"):
        parse_source_weight("a.example=2")
    assert parse_source_weight("Boards.Greenhouse.io=0.75") == ("boards.greenhouse.io", 0.75)


def test_weighted_derivation_prices_weak_sources():
    task = _checklist_task(**WEIGHTED_TASK_KWARGS)
    answers = {"alpha": True, "beta": True}
    assert task.derive_checklist_score(answers, None) == 100
    assert task.derive_checklist_score(answers, {}) == 0, "untagged truth scores nothing"
    assert task.derive_checklist_score(answers, {"alpha": 0.5, "beta": 0.5}) == 55
    assert task.derive_checklist_score(answers, {"alpha": 1.0}) == 60, "unbacked beta prices zero"
    assert task.derive_checklist_score({"alpha": True, "beta": False}, {"alpha": 0.25}) == 15


def test_support_strengths_aggregate_sources_by_trust():
    """Strength is the aggregate of a claim's sources, priced by the contract.

    This replaced "take the strongest backing weight": several sources agreeing
    is evidence (so a second one raises the strength), a low-weight source is
    worth less, and none of it is a magic constant invented here — the trust,
    specificity and corroboration rules live in contracts.py.
    """
    task = _checklist_task(**WEIGHTED_TASK_KWARGS)
    one = task.support_strengths([{"supports": ["alpha"], "text": "x" * 20}],
                                 "https://boards.greenhouse.io/a")
    two = task.support_strengths(
        [{"supports": ["alpha"], "text": "x" * 20}, {"supports": ["alpha"], "text": "y" * 20}],
        "https://boards.greenhouse.io/a",
    )
    assert one["alpha"] == 1.0, "one qualifying source is enough; the bar is binary"
    assert two["alpha"] == one["alpha"], "a second source adds nothing to a met bar"

    # A generic page is not evidence at all now, whatever weight it carries:
    # the bar is about what a source *is*, the weight is about how much of an
    # allowed source's word to take.
    assert task.support_strengths([{"supports": ["alpha"], "text": "x" * 20}],
                                  "https://aggregator.example/a") == {}

    # Structural invariants are unchanged.
    assert task.support_strengths([{"text": "untagged"}], "https://boards.greenhouse.io/a") == {}
    assert task.support_strengths("not-a-list", "https://boards.greenhouse.io/a") == {}


def test_coverage_true_answers_need_supporting_quotes():
    task = _checklist_task()
    claims = {"checklist": {"alpha": True, "beta": False}, "reason": "r"}
    quote = {"slice_id": "full", "text": "a" * 20, "supports": ["alpha"]}
    task.validate_claims(claims, quotes=[quote])
    # Once any quote tags support, every true item needs backing.
    with pytest.raises(ValueError, match="lack a supporting quote"):
        task.validate_claims(claims, quotes=[{"slice_id": "full", "text": "b" * 20, "supports": ["beta"]}])
    with pytest.raises(ValueError, match="unknown checklist items"):
        task.validate_claims(claims, quotes=[{"slice_id": "full", "text": "c" * 20, "supports": ["zzz"]}])
    # False answers need no support.
    task.validate_claims({"checklist": {"alpha": False, "beta": False}, "reason": "r"}, quotes=[])
    # Legacy untagged records validate unweighted but derive zero: dodging
    # the tags cannot inflate, it can only zero out.
    legacy = dict(claims, score=60)
    task.validate_claims(legacy, quotes=[{"slice_id": "full", "text": "a" * 20}])
    assert task.with_derived_claims(
        {"checklist": {"alpha": True, "beta": False}, "reason": "r"}, {}
    )["score"] == 0


def test_revision_payload_drops_default_weights():
    from harness_fleet.store import digest_json

    task = TaskSpec(name="t")
    assert "source_weights" not in task.revision_payload()
    assert "default_source_weight" not in task.revision_payload()
    weighted = _checklist_task(**WEIGHTED_TASK_KWARGS)
    assert weighted.revision_payload()["source_weights"] == WEIGHTED_TASK_KWARGS["source_weights"]
    assert digest_json(weighted.revision_payload()) != digest_json(task.revision_payload())


def test_weighted_engine_end_to_end(tmp_path):
    import json as _json

    from harness_fleet.catalog import RouteCatalog
    from harness_fleet.engine import Engine
    from harness_fleet.packer import pack_items
    from harness_fleet.store import HarnessStore

    task = create_task_from_preset("w-run", preset_name="score")
    # The weight must apply to a source the bar allows, since a disallowed one
    # is not evidence at any weight.
    task.source_weights = {"boards.greenhouse.io": 0.25}

    class Stub:
        def run_prompt(self, route_id, prompt, system_prompt=None, timeout_sec=120, session_id=None, policy=None):
            payload = {
                "items": [{
                    "item_id": "i1",
                    "claims": {
                        "checklist": {"initiative_named": True, "criteria_evidence": True,
                                      "supporting_signals": False},
                        "reason": "weakly sourced but tagged",
                    },
                    "quotes": [{"slice_id": "full",
                                "supports": ["initiative_named", "criteria_evidence"],
                                "text": "Acme is migrating its platform to Kubernetes this quarter."}],
                }]
            }
            receipt = {"id": "r1", "session_id": session_id, "provider": "stub",
                       "requested_route": route_id, "status": "complete", "cost": 0.0,
                       "cost_status": "reported_zero", "usage": {"total_tokens": 1},
                       "error": None, "duration_seconds": 0.01}
            return True, _json.dumps(payload), receipt

    catalog = RouteCatalog(config_path=tmp_path / "routes.json")
    catalog.data = {"revision": 2, "routes": [
        {"id": "stub/zero", "provider": "stub", "enabled": True,
         "price_state": "price_observed_zero"}]}
    store = HarnessStore(tmp_path / "s.db")
    engine = Engine(task, catalog=catalog, store=store)
    engine.registry.register("stub", Stub())
    card = {"item_id": "i1", "text": TEXT, "source_uri": "https://boards.greenhouse.io/acme"}
    ok, results, _, err = engine.execute_batch(pack_items([card])[0], run_id="weight-run")
    assert ok is True, err
    # The engine's score is the derivation from its own strengths; a weighted
    # aggregator source prices well below the same answers on a trusted one.
    quotes = [{"slice_id": "full", "supports": ["initiative_named", "criteria_evidence"],
               "text": "Acme is migrating its platform to Kubernetes this quarter."}]
    expected = task.derive_checklist_score(
        {"initiative_named": True, "criteria_evidence": True, "supporting_signals": False},
        task.support_strengths(quotes, "https://boards.greenhouse.io/acme"),
    )
    assert results[0]["claims"]["score"] == expected
    assert expected == 19, "the source weight prices 75 points at 0.25"
    history = store.get_entity_history("i1")
    assert len(history) == 1 and history[0]["score"] == expected


# --- recency ---------------------------------------------------------------

HALF_LIFE_KWARGS = {"recency_half_lives": {"alpha": 20.0, "beta": 200.0}}


def test_recency_decay_math():
    task = _checklist_task(**HALF_LIFE_KWARGS)
    assert task.recency_decay("alpha", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00") == 1.0
    assert task.recency_decay("alpha", "2026-01-01T00:00:00+00:00", "2026-01-21T00:00:00+00:00") == 0.5
    assert task.recency_decay("alpha", "2026-01-01T00:00:00+00:00", "2026-02-10T00:00:00+00:00") == 0.25
    # No configured half-life, missing or malformed dates decay nothing.
    assert task.recency_decay("zzz", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00") == 1.0
    assert task.recency_decay("alpha", None, "2026-01-21T00:00:00+00:00") == 1.0
    assert task.recency_decay("alpha", "not-a-date", "2026-01-21T00:00:00+00:00") == 1.0
    # Naive timestamps read as UTC; future captures clamp to full strength.
    assert task.recency_decay("alpha", "2026-01-01T00:00:00", "2026-01-21T00:00:00+00:00") == 0.5
    assert task.recency_decay("alpha", "2026-02-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00") == 1.0


def test_recency_half_life_validation():
    with pytest.raises(ValueError, match="positive number of days"):
        _checklist_task(recency_half_lives={"alpha": 0})
    with pytest.raises(ValueError, match="positive number of days"):
        _checklist_task(recency_half_lives={"alpha": float("inf")})
    with pytest.raises(ValueError, match="names no checklist item"):
        _checklist_task(recency_half_lives={"zzz": 10})
    with pytest.raises(ValueError, match="positive number of days"):
        parse_half_life("alpha=-3")
    with pytest.raises(ValueError, match="must look like"):
        parse_half_life("alpha")
    assert parse_half_life("hiring_or_trigger=21") == ("hiring_or_trigger", 21.0)


def test_account_preset_half_lives_price_hiring_fast():
    research = create_task_from_preset("r", preset_name="account-research")
    assert research.recency_half_lives["hiring_or_trigger"] == 21.0
    assert research.recency_half_lives["stack_confirmed"] > research.recency_half_lives["hiring_or_trigger"]
    guide = research.render_worker_guide()
    assert "hiring_or_trigger 21d" in guide


def test_aged_evidence_scores_less_end_to_end(tmp_path):
    import json as _json

    from harness_fleet.catalog import RouteCatalog
    from harness_fleet.engine import Engine
    from harness_fleet.packer import pack_items
    from harness_fleet.store import HarnessStore

    task = create_task_from_preset("aged", preset_name="score")
    task.recency_half_lives = {"supporting_signals": 20.0}

    class Stub:
        def run_prompt(self, route_id, prompt, system_prompt=None, timeout_sec=120, session_id=None, policy=None):
            payload = {
                "items": [{
                    "item_id": "i1",
                    "claims": {
                        "checklist": {"initiative_named": False, "criteria_evidence": False,
                                      "supporting_signals": True},
                        "reason": "old signal",
                    },
                    "quotes": [{"slice_id": "full", "supports": ["supporting_signals"],
                                "text": "The team opened senior roles in Berlin after the funding round."}],
                }]
            }
            receipt = {"id": "r1", "session_id": session_id, "provider": "stub",
                       "requested_route": route_id, "status": "complete", "cost": 0.0,
                       "cost_status": "reported_zero", "usage": {"total_tokens": 1},
                       "error": None, "duration_seconds": 0.01}
            return True, _json.dumps(payload), receipt

    catalog = RouteCatalog(config_path=tmp_path / "routes.json")
    catalog.data = {"revision": 2, "routes": [
        {"id": "stub/zero", "provider": "stub", "enabled": True,
         "price_state": "price_observed_zero"}]}
    store = HarnessStore(tmp_path / "s.db")
    engine = Engine(task, catalog=catalog, store=store)
    engine.registry.register("stub", Stub())
    card = {"item_id": "i1", "text": TEXT,
            "metadata": {"captured_at": "2020-01-01T00:00:00+00:00"}}
    ok, results, _, err = engine.execute_batch(pack_items([card])[0], run_id="aged-run")
    assert ok is True, err
    # 25 points decayed over ~6 years at a 20-day half-life prices to zero.
    assert results[0]["claims"]["score"] == 0
    assert results[0]["captured_at"] == "2020-01-01T00:00:00+00:00"
    assert results[0]["scored_at"] is not None
    # Revalidation with the fixed record timestamps reproduces the verdict.
    task.validate_claims(
        results[0]["claims"],
        quotes=results[0]["quotes"],
        strengths=task.support_strengths(
            results[0]["quotes"], None,
            results[0]["captured_at"], results[0]["scored_at"],
        ),
    )


# --- rescore lineage ---------------------------------------------------------

def test_score_history_trajectory_and_parent_linkage(tmp_path):
    from harness_fleet.store import HarnessStore

    store = HarnessStore(tmp_path / "s.db")
    store.record_score_history(run_id="r1", item_id="acme-1", entity="acme", score=40.0)
    store.record_score_history(run_id="r2", item_id="acme-2", entity="acme", score=85.0)
    rows = store.get_entity_history("acme")
    assert [row["score"] for row in rows] == [40.0, 85.0]
    assert rows[0]["run_id"] == "r1" and rows[1]["item_id"] == "acme-2"
    assert store.get_entity_history("unknown") == []


def test_runs_carry_parent_lineage(tmp_path):
    from harness_fleet.store import HarnessStore

    store = HarnessStore(tmp_path / "s.db")
    task = create_task_from_preset("lineage", preset_name="summarize")
    revision = store.register_task(task)
    with store.streaming_run(
        run_id="child", task_revision_id=revision, input_path="in.jsonl",
        input_digest="a" * 64, total_items=0, max_attempts=3, batch_size=2,
        output_path="out.json", parent_run_id="parent",
    ):
        pass
    assert store.run_snapshot("child")["parent_run_id"] == "parent"
    with pytest.raises(ValueError, match="parent_run_id"):
        with store.streaming_run(
            run_id="bad", task_revision_id=revision, input_path="in.jsonl",
            input_digest="b" * 64, total_items=0, max_attempts=3, batch_size=2,
            output_path="out.json", parent_run_id="has space",
        ):
            pass


def test_init_source_weight_flag_registers_weighted_task(tmp_path, monkeypatch):
    from argparse import Namespace

    from harness_fleet import cli
    from harness_fleet.store import HarnessStore

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(
        name="weighted", preset="score", batch_size=4,
        source_weight=["aggregator.example=0.25", "boards.greenhouse.io=1"],
        sample=None, db=str(db), json=True,
    ))
    task = HarnessStore(db).get_task("weighted")
    assert task.source_weights == {"aggregator.example": 0.25, "boards.greenhouse.io": 1.0}
    args = cli.build_parser().parse_args(["init", "x", "--source-weight", "a.example=0.5"])
    assert args.source_weight == ["a.example=0.5"]
    rescore_args = cli.build_parser().parse_args(["rescore", "parent-1", "--input", "new.jsonl"])
    assert rescore_args.parent_run == "parent-1" and rescore_args.task is None


# --- engine end to end --------------------------------------------------------

def test_engine_derives_score_from_checklist_claims(tmp_path):
    import json as _json

    from harness_fleet.catalog import RouteCatalog
    from harness_fleet.engine import Engine
    from harness_fleet.packer import pack_items
    from harness_fleet.store import HarnessStore

    task = create_task_from_preset("check-run", preset_name="score")

    class Stub:
        def run_prompt(self, route_id, prompt, system_prompt=None, timeout_sec=120, session_id=None, policy=None):
            payload = {
                "items": [{
                    "item_id": "i1",
                    "claims": {
                        "checklist": {"initiative_named": True, "criteria_evidence": True,
                                      "supporting_signals": False},
                        "reason": "migration named with stack facts",
                    },
                    "quotes": [{"slice_id": "full",
                                "supports": ["initiative_named", "criteria_evidence"],
                                "text": "Acme is migrating its platform to Kubernetes this quarter."}],
                }]
            }
            receipt = {"id": "r1", "session_id": session_id, "provider": "stub",
                       "requested_route": route_id, "status": "complete", "cost": 0.0,
                       "cost_status": "reported_zero", "usage": {"total_tokens": 1},
                       "error": None, "duration_seconds": 0.01}
            return True, _json.dumps(payload), receipt

    catalog = RouteCatalog(config_path=tmp_path / "routes.json")
    catalog.data = {"revision": 2, "routes": [
        {"id": "stub/zero", "provider": "stub", "enabled": True,
         "price_state": "price_observed_zero"}]}
    engine = Engine(task, catalog=catalog, store=HarnessStore(tmp_path / "s.db"))
    engine.registry.register("stub", Stub())
    batch = pack_items([{"item_id": "i1", "text": TEXT}])[0]
    ok, results, _, err = engine.execute_batch(batch, run_id="check-run")
    assert ok is True, err
    # The score the engine stores is the derivation from its own strengths —
    # which the central contract prices by source trust and specificity, not by
    # checklist points alone. Deriving it here keeps this test about the wiring.
    expected = task.derive_checklist_score(
        {"initiative_named": True, "criteria_evidence": True, "supporting_signals": False},
        task.support_strengths([{"slice_id": "full", "supports": ["initiative_named", "criteria_evidence"],
  "text": "Acme is migrating its platform to Kubernetes this quarter."}], None),
    )
    assert results[0]["claims"]["score"] == expected
    assert expected == 75, "an unsourced document can carry its own first-party claims"

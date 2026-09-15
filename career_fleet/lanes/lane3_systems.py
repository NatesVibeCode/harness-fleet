"""Lane 3: Technical Systems Wedge & Defensibility Evaluation.
Evaluates surviving companies for proprietary technical moats, stateful architectures,
and mission-critical infrastructure vs. fragile commodity wrappers.
"""
from __future__ import annotations

import re
from typing import Any

from career_fleet.profile import IdealEmployerProfile
from career_fleet.store import CareerStore

SYSTEM_WEDGE_SIGNALS = [
    (re.compile(r"\b(?:database|distributed systems|storage engine|raft|consensus|kafka)\b", re.I), "Infrastructure & Data Systems"),
    (re.compile(r"\b(?:compiler|runtime|ebpf|kernel|parser|ast|bytecode)\b", re.I), "Systems & Compiler Architecture"),
    (re.compile(r"\b(?:workflow engine|state machine|durable execution|event-driven)\b", re.I), "Durable Orchestration"),
    (re.compile(r"\b(?:fintech|banking rails|hipaa|pci-dss|soc2|compliance engine)\b", re.I), "Regulated Core Rails"),
]


def _phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    terms = [term for term in re.split(r"[\s/_-]+", str(phrase or "").strip()) if term]
    if not terms:
        return None
    return re.compile(
        r"(?<!\w)" + r"[\W_]+".join(re.escape(term) for term in terms) + r"(?!\w)",
        re.I,
    )


def _stack_options(requirement: str) -> list[str]:
    return [option.strip() for option in str(requirement or "").split("/") if option.strip()]


def _configured_matches(text: str, phrases: list[str]) -> list[str]:
    matches = []
    for phrase in phrases:
        pattern = _phrase_pattern(phrase)
        if pattern and pattern.search(text):
            matches.append(phrase)
    return matches


def score_technical_wedge(
    text: str,
    profile: IdealEmployerProfile,
) -> dict[str, Any]:
    """Score technical wedge depth and extract supporting quotes."""
    if not text or not text.strip():
        return {
            "score": 0.0,
            "verdict": "UNKNOWN",
            "matched_wedges": [],
            "matched_stack": [],
            "missing_required_stack": list(profile.required_stack),
            "required_stack_satisfied": not profile.required_stack,
            "matched_capabilities": [],
            "matched_catalysts": [],
            "matched_negative_stack": [],
            "quotes": [],
            "rationale": "No captured source text available to evaluate technical wedge.",
        }

    matched_wedges = []
    required_quotes = []
    signal_quotes = []

    for pattern, label in SYSTEM_WEDGE_SIGNALS:
        match = pattern.search(text)
        if match:
            matched_wedges.append(label)
            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 30)
            signal_quotes.append(text[start:end].strip())

    # Stack alignment check (handles slash-separated technologies like 'Modern Cloud / Kubernetes')
    matched_stack = []
    missing_required_stack = []
    for requirement in profile.required_stack:
        matched_option = None
        for option in _stack_options(requirement):
            stack_pattern = _phrase_pattern(option)
            if stack_pattern and stack_pattern.search(text):
                matched_option = option
                break
        if matched_option:
            if matched_option not in matched_stack:
                matched_stack.append(matched_option)
            stack_pattern = _phrase_pattern(matched_option)
            stack_match = stack_pattern.search(text) if stack_pattern else None
            if stack_match:
                start = max(0, stack_match.start() - 30)
                end = min(len(text), stack_match.end() + 30)
                required_quotes.append(text[start:end].strip())
        else:
            missing_required_stack.append(requirement)

    matched_capabilities = _configured_matches(text, profile.wedge_capabilities)
    matched_catalysts = _configured_matches(text, profile.hiring_catalysts)
    matched_negative_stack = _configured_matches(text, profile.negative_stack)

    for phrase in matched_capabilities + matched_catalysts + matched_negative_stack:
        sig_pattern = _phrase_pattern(phrase)
        sig_match = sig_pattern.search(text) if sig_pattern else None
        if sig_match:
            start = max(0, sig_match.start() - 30)
            end = min(len(text), sig_match.end() + 30)
            signal_quotes.append(text[start:end].strip())

    score = (
        (len(matched_wedges) * 0.3)
        + (len(matched_stack) * 0.15)
        + (len(matched_capabilities) * 0.1)
        + (len(matched_catalysts) * 0.1)
        - (len(matched_negative_stack) * 0.3)
    )
    if missing_required_stack:
        # A configured required stack is a real gate, not merely a bonus.
        score = min(score, 0.59)
    score = max(0.0, min(1.0, score))
    verdict = "HIGH FIT" if score >= 0.8 else ("STRONG FIT" if score >= 0.6 else "MARGINAL")
    quotes = (required_quotes + signal_quotes)[: max(5, len(required_quotes))]

    return {
        "score": round(score, 2),
        "verdict": verdict,
        "matched_wedges": matched_wedges,
        "matched_stack": matched_stack,
        "missing_required_stack": missing_required_stack,
        "required_stack_satisfied": not missing_required_stack,
        "matched_capabilities": matched_capabilities,
        "matched_catalysts": matched_catalysts,
        "matched_negative_stack": matched_negative_stack,
        "quotes": quotes[:5],
        "rationale": (
            f"Identified technical wedges: {', '.join(matched_wedges) or 'None'}. "
            f"Stack alignment: {', '.join(matched_stack) or 'None'}. "
            f"Missing required stack: {', '.join(missing_required_stack) or 'None'}. "
            f"Profile capability matches: {', '.join(matched_capabilities) or 'None'}. "
            f"Hiring catalyst matches: {', '.join(matched_catalysts) or 'None'}. "
            f"Negative stack signals: {', '.join(matched_negative_stack) or 'None'}."
        ),
    }


def run_lane3_systems(
    store: CareerStore,
    profile: IdealEmployerProfile,
) -> dict[str, Any]:
    """Evaluate every current Lane 2 survivor, including prior results.

    A profile change must be able to invalidate a previously qualified
    company, so the company status alone is not used as a skip signal.
    """
    profile_revision_id = store.save_profile(profile)
    survivors = []
    for company in store.list_companies():
        if company["status"] not in ("triaged", "qualified"):
            continue
        dossier = store.get_company_dossier(company["id"])
        evaluations = dossier.get("evaluations", []) if dossier else []
        lane2_results = [e for e in evaluations if e["lane"] == "lane2_triage"]
        if lane2_results and lane2_results[-1]["status"] != "triaged":
            continue
        if company["status"] == "qualified" and not lane2_results:
            continue
        survivors.append(company)
    evaluated = 0

    for comp in survivors:
        cid = comp["id"]
        dossier = store.get_company_dossier(cid)
        postings = dossier.get("jobs", []) if dossier else []
        combined_text = "\n".join(p.get("raw_text", "") for p in postings)

        res = score_technical_wedge(combined_text, profile)
        status = "qualified" if res["score"] >= 0.6 and res["required_stack_satisfied"] else "marginal"

        store.record_evaluation(
            eval_id=f"eval-systems-{cid}",
            company_id=cid,
            lane="lane3_systems",
            status=status,
            score=res["score"],
            verdict=res["verdict"],
            rationale=res["rationale"],
            quotes=res["quotes"],
            model_used="deterministic-wedge-scorer",
            profile_revision_id=profile_revision_id,
        )
        evaluated += 1

    return {
        "status": "success",
        "evaluated": evaluated,
    }

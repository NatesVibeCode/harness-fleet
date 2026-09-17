"""Exact source-offset grounding with deterministic unicode normalization and fuzzy repair."""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from .candidates import extract_candidates
from .models import CandidateExtractedItem, ExtractedItem, QuoteRef


class GroundingError(ValueError):
    pass

# Common unicode quirks that LLMs normalize away
_UNICODE_REPLACEMENTS = {
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u00a0": " ", "\u201a": "'", "\u201e": '"',
    "\u2014": "--", "\u2013": "-", "\u2026": "...",
    "\u2022": "-", "\u00ab": '"', "\u00bb": '"',
}

def _simple_normalize(text: str) -> str:
    for k, v in _UNICODE_REPLACEMENTS.items():
        text = text.replace(k, v)
    # NFKC for remaining compatibility mappings (ligatures, etc.)
    text = unicodedata.normalize("NFKC", text)
    # Collapse whitespace to single spaces for matching purposes
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _build_normalized_map(text: str):
    """Return (normalized_text, map_from_normalized_index -> original_char_index).
    Built by per-char replacement + NFKC, tracking expansions.
    """
    norm_parts: list[str] = []
    index_map: list[int] = []  # for each char in normalized text, which original index it came from
    for orig_idx, ch in enumerate(text):
        replaced = _UNICODE_REPLACEMENTS.get(ch, ch)
        # NFKC may expand single char
        nfkc = unicodedata.normalize("NFKC", replaced)
        # Collapse? We handle whitespace collapse after building per-char map, but we need post-collapse map simplification:
        # Use raw nfkc chars for now; whitespace collapse will be second pass with approximate mapping.
        for nc in nfkc:
            norm_parts.append(nc)
            index_map.append(orig_idx)
    raw_norm = "".join(norm_parts)
    # Now collapse whitespace sequences (e.g. "  " -> " ") and keep map via scanning
    collapsed_chars: list[str] = []
    collapsed_map: list[int] = []
    in_space = False
    for i, c in enumerate(raw_norm):
        if c.isspace():
            if not in_space:
                collapsed_chars.append(" ")
                collapsed_map.append(index_map[i])
                in_space = True
            # else skip duplicate spaces
        else:
            collapsed_chars.append(c)
            collapsed_map.append(index_map[i])
            in_space = False
    # Strip leading/trailing collapsed space without losing map correctness
    # Rebuild exact trimmed map by re-scanning collapsed with strip positions
    # For simplicity, rebuild by stripping from both ends using the collapsed representation.
    start_trim = 0
    while start_trim < len(collapsed_chars) and collapsed_chars[start_trim].isspace():
        start_trim += 1
    end_trim = len(collapsed_chars)
    while end_trim > start_trim and collapsed_chars[end_trim - 1].isspace():
        end_trim -= 1
    return "".join(collapsed_chars[start_trim:end_trim]), collapsed_map[start_trim:end_trim]

def _fuzzy_ratios(candidate_text: str, slice_text: str) -> tuple[str, str, list[int], list[tuple[int, float]]]:
    """Normalized candidate/slice, index map, and (start, ratio) per window.

    Computed once per fuzzy resolution and shared by the ambiguity guard
    and the best-match search so long slices are scanned a single time.
    """
    cand_norm = _simple_normalize(candidate_text)
    slice_norm, norm_map = _build_normalized_map(slice_text)
    if not cand_norm or not slice_norm:
        return cand_norm, slice_norm, norm_map, []
    cand_len = len(cand_norm)
    step = max(1, cand_len // 8)
    scored = []
    for start in range(0, max(1, len(slice_norm) - cand_len + 1), step):
        window = slice_norm[start:start + cand_len + 10][:cand_len]
        scored.append((start, difflib.SequenceMatcher(None, cand_norm, window).ratio()))
    return cand_norm, slice_norm, norm_map, scored


def _fuzzy_ambiguous(candidate_text: str, slice_text: str, min_ratio: float = 0.85) -> bool:
    """Whether two non-overlapping windows both resemble the candidate.

    Mirrors the exact/normalized ambiguity guards: a fuzzy match that could
    plausibly sit in two places must not silently ground to one of them.
    """
    cand_norm, _, _, scored = _fuzzy_ratios(candidate_text, slice_text)
    if not cand_norm:
        return False
    hits = [start for start, ratio in scored if ratio >= min_ratio]
    for i, first in enumerate(hits):
        for second in hits[i + 1:]:
            if abs(second - first) >= len(cand_norm):
                return True
    return False


def _fuzzy_find_in_slice(candidate_text: str, slice_text: str, min_ratio: float = 0.85) -> tuple[int, int] | None:
    """Use difflib to find best near-verbatim substring of similar length when exact normalized fails."""
    cand_norm, slice_norm, norm_map, scored = _fuzzy_ratios(candidate_text, slice_text)
    if not cand_norm or not slice_norm:
        return None
    cand_len = len(cand_norm)
    best_ratio = 0.0
    best_pos = -1
    for start, ratio in scored:
        if ratio > best_ratio:
            best_ratio = ratio
            best_pos = start
            if ratio >= 0.98:
                break
    if best_ratio >= min_ratio and best_pos != -1:
        # Expand match to exact candidate length in normalized space
        norm_start = best_pos
        norm_end = norm_start + cand_len
        # Refine via SequenceMatcher to find actual matching block
        # Use opcodes to get precise boundaries? For now return mapped original offsets via norm_map
        try:
            orig_start = norm_map[norm_start]
            # Map end: last normalized char's original index +1
            last_norm_idx = min(norm_end - 1, len(norm_map) - 1)
            orig_end = norm_map[last_norm_idx] + 1
            # Extend to include rest of original token if we cut inside a word due to normalization slop
            # Ensure the extracted original substring normalizes close to candidate
            # Validate by extracting and renormalizing
            extracted = slice_text[orig_start:orig_end]
            if _simple_normalize(extracted) == cand_norm:
                return orig_start, orig_end
            # Check ratio of extracted slice text vs candidate
            curr_ratio = difflib.SequenceMatcher(None, _simple_normalize(extracted), cand_norm).ratio()
            best_s, best_e = orig_start, orig_end
            best_r = curr_ratio
            for delta in (1, 2, 3, 5, 8):
                for s in (max(0, orig_start - delta), orig_start):
                    for e in (min(len(slice_text), orig_end + delta), orig_end):
                        if s >= e:
                            continue
                        r = difflib.SequenceMatcher(None, _simple_normalize(slice_text[s:e]), cand_norm).ratio()
                        if r > best_r:
                            best_r = r
                            best_s, best_e = s, e
            if best_r >= min_ratio:
                return best_s, best_e
        except Exception:
            pass
    return None


def _resolve_candidate_span(
    candidate_text: str,
    span: dict[str, Any],
    slice_text: str,
) -> tuple[int, int, str] | None:
    """Match a cited candidate's text inside its own code-owned span.

    Exact copy first, then unicode-normalized match constrained to the span
    (models often normalize curly quotes when copying). Anything else fails
    closed: a candidate_id is a precision citation, and silently grounding it
    to different text would launder the worker's evidence choice. Returns
    (relative_start, relative_end, verbatim_span_text).
    """
    span_text = span["text"]
    if candidate_text == span_text:
        return span["start"], span["end"], span_text
    cand_norm = _simple_normalize(candidate_text)
    span_norm, span_map = _build_normalized_map(span_text)
    if not cand_norm or not span_norm:
        return None
    first = span_norm.find(cand_norm)
    if first < 0 or span_norm.find(cand_norm, first + 1) >= 0:
        return None
    try:
        rel_start = span_map[first]
        rel_end = span_map[min(first + len(cand_norm) - 1, len(span_map) - 1)] + 1
    except IndexError:
        return None
    if _simple_normalize(span_text[rel_start:rel_end]) != cand_norm:
        return None
    return span["start"] + rel_start, span["start"] + rel_end, slice_text[
        span["start"] + rel_start : span["start"] + rel_end
    ]


def normalize_grounding(
    extracted_items: Sequence[CandidateExtractedItem | dict[str, Any]],
    raw_cards: list[dict[str, Any]],
    min_quote_chars: int = 15,
    evidence_terms: list[str] | tuple[str, ...] | None = None,
    candidate_top_n: int = 6,
    scored_at: str | None = None,
) -> tuple[list[ExtractedItem] | None, str | None]:
    """Resolve unique quote text to canonical absolute offsets, then verify it.
    Supports exact, unicode-normalized, and fuzzy fallback matching for LLM quirks
    (curly quotes, nbsp, em-dash, whitespace collapse, minor typos) before failing.
    """
    card_map = {str(card["item_id"]): card for card in raw_cards}
    normalized: list[ExtractedItem] = []

    for raw_item in extracted_items:
        try:
            item = raw_item if isinstance(raw_item, CandidateExtractedItem) else CandidateExtractedItem.model_validate(raw_item)
        except ValidationError as exc:
            return None, f"Invalid extracted item: {exc.errors(include_url=False)}"
        card = card_map.get(item.item_id)
        if card is None:
            return None, f"Unknown item_id '{item.item_id}' returned by model."
        meta = card.get("metadata") or {}
        captured_at = meta.get("captured_at") if isinstance(meta, dict) else None
        if not isinstance(captured_at, str):
            captured_at = None
        slices = {str(part["slice_id"]): part for part in card.get("slices", [])}
        quotes: list[QuoteRef] = []
        for candidate in item.quotes:
            if len(candidate.text) < min_quote_chars:
                return None, f"Item '{item.item_id}' quote too short ({len(candidate.text)} < {min_quote_chars} chars): '{candidate.text}'"
            source_slice = slices.get(candidate.slice_id)
            if source_slice is None:
                return None, f"Item '{item.item_id}' references unknown slice '{candidate.slice_id}'."

            slice_text = source_slice.get("text", "")
            slice_start = int(source_slice["start"])
            _slice_end = int(source_slice["end"])

            # Check if model provided offsets that are ALREADY verbatim accurate
            offsets_valid = False
            if candidate.start is not None and candidate.end is not None:
                rel_s = candidate.start - slice_start
                rel_e = candidate.end - slice_start
                if 0 <= rel_s < rel_e <= len(slice_text) and slice_text[rel_s:rel_e] == candidate.text:
                    start, end = candidate.start, candidate.end
                    offsets_valid = True

            if not offsets_valid and candidate.candidate_id is not None:
                # 0) Cited evidence candidate: recompute the code-owned span
                # table and resolve inside it. Offsets stay optional because
                # the span already fixes them; anything that does not match
                # the cited span fails instead of searching the whole slice.
                # The span table is an *optimization*, and `extract_candidates`
                # says so: it returns [] when no evidence term appears, and "the
                # caller then falls back to the legacy free-search quote path".
                # It did not. A model that followed the contract — "cite
                # candidate_id and copy the span text exactly" — was refused
                # outright whenever that table came back empty, which is the
                # common case for a short dossier, so *every* candidate failed
                # grounding and no run could ever score above zero. An empty
                # table is not evidence against the model; it is a reason to
                # search the slice for the quote instead of indexing into it.
                spans = (
                    extract_candidates(slice_text, evidence_terms, candidate_top_n)
                    if evidence_terms
                    else []
                )
                if 0 <= candidate.candidate_id < len(spans):
                    resolved = _resolve_candidate_span(candidate.text, spans[candidate.candidate_id], slice_text)
                    if resolved is None:
                        return None, (
                            f"Item '{item.item_id}' quote does not match cited candidate "
                            f"#{candidate.candidate_id} in slice '{candidate.slice_id}'; copy the span exactly."
                        )
                    rel_start, rel_end, verbatim = resolved
                    quotes.append(QuoteRef(
                        slice_id=candidate.slice_id,
                        start=slice_start + rel_start,
                        end=slice_start + rel_end,
                        text=verbatim,
                        supports=list(candidate.supports),
                    ))
                    continue
                # Otherwise fall through to the free-search path below, which
                # resolves the quote by exact match, normalized match, or reports
                # a real ambiguity — the promise this branch used to break.

            if not offsets_valid:
                # 1) Exact match
                first = slice_text.find(candidate.text)
                if first >= 0:
                    if slice_text.find(candidate.text, first + 1) >= 0:
                        return None, f"Item '{item.item_id}' quote is ambiguous in slice '{candidate.slice_id}'; provide exact offsets."
                    start = slice_start + first
                    end = start + len(candidate.text)
                else:
                    # 2) Unicode-normalized match via index map
                    slice_norm, norm_map = _build_normalized_map(slice_text)
                    cand_norm = _simple_normalize(candidate.text)
                    norm_first = slice_norm.find(cand_norm)
                    if norm_first >= 0:
                        # Check ambiguity in normalized space as well
                        if slice_norm.find(cand_norm, norm_first + 1) >= 0:
                            return None, f"Item '{item.item_id}' quote is ambiguous in slice '{candidate.slice_id}' (normalized); provide exact offsets."
                        # Map normalized offset back to original
                        try:
                            orig_start_rel = norm_map[norm_first]
                            last_idx = min(norm_first + len(cand_norm) - 1, len(norm_map) - 1)
                            orig_end_rel = norm_map[last_idx] + 1
                            # Adjust end to cover full token if normalized collapsed chars
                            # Verify round-trip
                            extracted = slice_text[orig_start_rel:orig_end_rel]
                            # If simple extraction doesn't re-normalize exactly, try expand by 1-2 chars
                            if _simple_normalize(extracted) != cand_norm:
                                # brute expand search around mapped region
                                found = False
                                for d in range(1, 6):
                                    for s in (max(0, orig_start_rel - d), orig_start_rel):
                                        for e in (min(len(slice_text), orig_end_rel + d), orig_end_rel):
                                            if _simple_normalize(slice_text[s:e]) == cand_norm:
                                                orig_start_rel, orig_end_rel = s, e
                                                found = True
                                                break
                                        if found:
                                            break
                                    if found:
                                        break
                                if _simple_normalize(slice_text[orig_start_rel:orig_end_rel]) != cand_norm:
                                    return None, f"Item '{item.item_id}' quote not found in slice '{candidate.slice_id}' (normalized map failed)."
                            # Use the actual original substring (preserves verbatim verification) but report candidate.text as normalized?
                            # We must emit a quote whose text exactly matches the slice substring for verify_grounding to pass.
                            # So use the original slice substring, not the model's curly-quote variant.
                            candidate_text_for_ref = slice_text[orig_start_rel:orig_end_rel]
                            start = int(source_slice["start"]) + orig_start_rel
                            end = start + len(candidate_text_for_ref)
                            # Store the verbatim slice text (so verify_grounding's exact check passes)
                            quotes.append(QuoteRef(slice_id=candidate.slice_id, start=start, end=end, text=candidate_text_for_ref, supports=list(candidate.supports)))
                            continue
                        except Exception:
                            return None, f"Item '{item.item_id}' quote not found in slice '{candidate.slice_id}'."
                    else:
                        # 3) Fuzzy fallback via difflib (minor typos, missing character)
                        if _fuzzy_ambiguous(candidate.text, slice_text):
                            return None, f"Item '{item.item_id}' quote is ambiguous in slice '{candidate.slice_id}'; provide exact offsets."
                        fuzzy = _fuzzy_find_in_slice(candidate.text, slice_text)
                        if fuzzy is not None:
                            rel_start, rel_end = fuzzy
                            candidate_text_for_ref = slice_text[rel_start:rel_end]
                            start = int(source_slice["start"]) + rel_start
                            end = start + len(candidate_text_for_ref)
                            quotes.append(QuoteRef(slice_id=candidate.slice_id, start=start, end=end, text=candidate_text_for_ref, supports=list(candidate.supports)))
                            continue
                        return None, f"Item '{item.item_id}' quote not found in slice '{candidate.slice_id}'."

            quotes.append(QuoteRef(slice_id=candidate.slice_id, start=start, end=end, text=candidate.text, supports=list(candidate.supports)))
        normalized.append(ExtractedItem(
            item_id=item.item_id,
            source_uri=card.get("source_uri"),
            source_digest=card["source_digest"],
            content_type=card["content_type"],
            claims=item.claims,
            quotes=quotes,
            captured_at=captured_at,
            scored_at=scored_at,
        ))

    # Stable packet order regardless of model output order.
    card_order = {iid: index for index, iid in enumerate(card_map)}
    normalized.sort(key=lambda item: card_order.get(item.item_id, len(card_order)))
    ok, error = verify_grounding(normalized, raw_cards, min_quote_chars=min_quote_chars)
    return (normalized, None) if ok else (None, error)

def verify_grounding(
    extracted_items: Sequence[ExtractedItem | dict[str, Any]],
    raw_cards: list[dict[str, Any]],
    quote_field: str = "quotes",
    min_quote_chars: int = 15,
) -> tuple[bool, str | None]:
    """Require each quote to match one supplied slice at its absolute offsets."""
    if quote_field != "quotes":
        return False, "typed output requires the canonical 'quotes' field"

    card_map = {str(card["item_id"]): card for card in raw_cards}
    # Item order is clerical, not evidentiary: accept any order and verify
    # deterministically in card order. Unknown ids sort last so they still
    # raise the unknown-id error below.
    order_index = {iid: index for index, iid in enumerate(card_map)}

    def _sort_key(raw_item: ExtractedItem | dict[str, Any]) -> tuple[int, str]:
        if isinstance(raw_item, dict):
            iid = str(raw_item.get("item_id", ""))
        else:
            iid = str(getattr(raw_item, "item_id", ""))
        return (order_index.get(iid, len(order_index)), iid)

    seen_ids: set[str] = set()

    for raw_item in sorted(extracted_items, key=_sort_key):
        try:
            item = raw_item if isinstance(raw_item, ExtractedItem) else ExtractedItem.model_validate(raw_item)
        except ValidationError as exc:
            return False, f"Invalid extracted item: {exc.errors(include_url=False)}"

        if item.item_id not in card_map:
            return False, f"Unknown item_id '{item.item_id}' returned by model."
        if item.item_id in seen_ids:
            return False, f"Duplicate item_id '{item.item_id}' returned by model."
        seen_ids.add(item.item_id)

        card = card_map[item.item_id]
        if item.source_digest != card.get("source_digest"):
            return False, f"Item '{item.item_id}' source digest does not match the supplied source."
        if item.source_uri != card.get("source_uri") or item.content_type != card.get("content_type"):
            return False, f"Item '{item.item_id}' source metadata does not match the supplied source."
        slices = {str(part["slice_id"]): part for part in card.get("slices", [])}
        for quote in item.quotes:
            if len(quote.text) < min_quote_chars:
                return False, f"Item '{item.item_id}' quote too short ({len(quote.text)} < {min_quote_chars} chars): '{quote.text}'"
            source_slice = slices.get(quote.slice_id)
            if source_slice is None:
                return False, f"Item '{item.item_id}' references unknown slice '{quote.slice_id}'."

            slice_start = source_slice.get("start")
            slice_end = source_slice.get("end")
            if not isinstance(slice_start, int) or not isinstance(slice_end, int):
                return False, f"Item '{item.item_id}' source slice has invalid bounds."
            if quote.start < slice_start or quote.end > slice_end:
                return False, f"Item '{item.item_id}' quote bounds fall outside slice '{quote.slice_id}'."

            relative_start = quote.start - slice_start
            relative_end = quote.end - slice_start
            if source_slice.get("text", "")[relative_start:relative_end] != quote.text:
                return False, f"Item '{item.item_id}' quote does not exactly match its source offsets."

    missing = set(card_map) - seen_ids
    if missing:
        return False, f"Model missed items from batch: {sorted(missing)}"
    return True, None

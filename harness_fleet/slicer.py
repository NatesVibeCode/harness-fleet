"""Structure-aware document slicing with exact offsets and complete coverage.

Long documents (partner dossiers run 20k-43k characters) are read by a model
in one prompt built from the item's slices, so slicing must never drop or
duplicate evidence: every character lands in exactly one slice, no slice
exceeds the budget, and every slice carries the exact offsets grounding
verifies quotes against.
"""
from __future__ import annotations

import re
from typing import Any

# Preferred break points, coarsest first, as (pattern, cut after the match?).
# A dossier section marker or a markdown heading starts the unit that follows
# it, so the cut belongs before the match; paragraph and sentence boundaries
# end the unit, so the cut belongs after the match. Either way the units tile
# the source exactly.
_SECTION_MARKER = re.compile(r"(?m)^={3,}[ \t]*SECTION:.*$")
_HEADING_MARKER = re.compile(r"(?m)^#{1,6}[ \t].*$")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t\r]*\n")
# Sentence ends: terminal punctuation plus trailing quotes and whitespace, or
# a bare newline. Punctuation must be followed by whitespace so decimals and
# abbreviations are never split.
_SENTENCE_BREAK = re.compile(r"(?:[.!?]+[\"')\]]*\s+|\n+)")

_BREAK_LEVELS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (_SECTION_MARKER, False),
    (_HEADING_MARKER, False),
    (_PARAGRAPH_BREAK, True),
    (_SENTENCE_BREAK, True),
)


def _full_slice(text: str, length: int) -> dict[str, Any]:
    """The whole document as one slice: nothing is withheld, so not partial."""
    return {"slice_id": "full", "start": 0, "end": length, "text": text, "partial": False}


def _split_at(
    text: str,
    start: int,
    end: int,
    boundary: re.Pattern[str],
    cut_after: bool,
) -> list[tuple[int, int]]:
    """Tile ``[start, end)`` at every match of ``boundary``, cutting on one side."""
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in boundary.finditer(text, start, end):
        cut = match.end() if cut_after else match.start()
        if cut <= cursor or cut >= end:
            continue
        spans.append((cursor, cut))
        cursor = cut
    if cursor < end:
        spans.append((cursor, end))
    return spans or [(start, end)]


def _hard_windows(start: int, end: int, budget: int) -> list[tuple[int, int]]:
    """Last resort for a unit with no break point at all: cut at exact offsets."""
    return [(offset, min(end, offset + budget)) for offset in range(start, end, budget)]


def _structure_slices(
    text: str,
    start: int,
    end: int,
    budget: int,
    level: int,
) -> list[tuple[int, int]]:
    """Tile ``[start, end)`` into slices of at most ``budget`` characters.

    Units from the coarsest structural level that offers a break are packed
    greedily; a unit that cannot fit under the ceiling is re-split at the next
    finer level, so sections and paragraphs survive whole whenever they fit and
    only an unsplittable run is windowed at arbitrary offsets.
    """
    if end - start <= budget:
        return [(start, end)]
    if level >= len(_BREAK_LEVELS):
        return _hard_windows(start, end, budget)

    boundary, cut_after = _BREAK_LEVELS[level]
    units = _split_at(text, start, end, boundary, cut_after)
    if len(units) <= 1:
        # No break at this level inside the span, so every unit is the span
        # itself: window it at the next finer level.
        return _structure_slices(text, start, end, budget, level + 1)

    slices: list[tuple[int, int]] = []
    index = 0
    while index < len(units):
        unit_start, unit_end = units[index]
        if unit_end - unit_start > budget:
            # The unit alone exceeds the ceiling, so it cannot be packed with
            # its neighbours: window it at the next finer level.
            slices.extend(_structure_slices(text, unit_start, unit_end, budget, level + 1))
            index += 1
            continue
        chunk_start, chunk_end = unit_start, unit_end
        index += 1
        while index < len(units) and units[index][1] - chunk_start <= budget:
            chunk_end = units[index][1]
            index += 1
        slices.append((chunk_start, chunk_end))
    return slices


def slice_document(
    text: str,
    max_chars: int | None = 6000,
    overlap_chars: int = 600,
) -> list[dict[str, Any]]:
    """Slices a document into structure-aligned, verifiable sections.

    Slices are a disjoint partition of the source: ``slices[0]["start"] == 0``,
    ``slices[-1]["end"] == len(text)``, consecutive ranges touch without a gap
    or overlap, and ``sum(end - start)`` equals the document length. Every
    character therefore lands in exactly one slice, and
    ``text[start:end] == slice["text"]`` holds for every slice, which is what
    ``grounding.verify_grounding`` checks quotes against.

    ``max_chars`` is both the soft budget slices are filled to and the hard
    ceiling no slice exceeds. Break points come from document structure,
    coarsest first -- dossier ``=== SECTION: ... ===`` markers, markdown
    headings, blank-line paragraph boundaries, then sentence ends -- so a
    section or paragraph is split only when it cannot fit under the ceiling by
    itself, and text is never cut mid-sentence unless a single unit has no
    break point at all, in which case it is windowed at exact offsets.

    ``max_chars`` of 0 (or ``None``) means "no budget": the whole document is
    returned as one ``"full"`` slice with ``partial: False``, so a single model
    attempt reads every character.

    A document that already fits ``max_chars`` returns one ``"full"`` slice,
    unchanged. Every slice of a multi-slice document carries ``partial: True``:
    it is one window of a larger document, and a quote must lie inside a single
    window.

    ``overlap_chars`` is accepted for call-site compatibility and is unused:
    slices no longer overlap, so a prompt carries each character once.
    """
    text = text or ""
    length = len(text)
    budget = None if max_chars is None else int(max_chars)
    if budget is None or budget <= 0 or length <= budget:
        return [_full_slice(text, length)]

    spans = _structure_slices(text, 0, length, budget, 0)
    return [
        {
            "slice_id": f"s{index}",
            "start": start,
            "end": end,
            "text": text[start:end],
            "partial": True,
        }
        for index, (start, end) in enumerate(spans)
    ]

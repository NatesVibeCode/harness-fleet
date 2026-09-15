"""Whole-document reading: complete coverage, structure-aligned cuts, exact offsets."""
import hashlib

from harness_fleet.grounding import normalize_grounding, verify_grounding
from harness_fleet.packer import pack_items
from harness_fleet.slicer import slice_document

CATEGORIES = ["FIRST-PARTY", "VENDOR REGISTRY", "REVIEW AUDIT", "COMMUNITY",
              "ATS", "CASE STUDY", "ENGINEERING BLOG", "GROWTH SIGNAL"]
TAIL = "The final line records the Krakatoa rollout as the closing evidence."


def dossier(sections=8, section_chars=5300):
    parts = ["# Multi-Source Evidence Dossier: acme.example",
             f"# source_count: {sections}", ""]
    for index in range(sections):
        category = CATEGORIES[index % len(CATEGORIES)]
        parts.append(f"=== SECTION: {category} (URI: https://s{index}.example/acme) ===")
        written, paragraph = 0, 0
        while written < section_chars:
            line = (f"{category} paragraph {paragraph} reports that the practice delivered a "
                    f"Kafka migration for a named enterprise client and the practice grew.")
            parts.append(line)
            parts.append("")
            written += len(line) + 1
            paragraph += 1
    parts.append(TAIL)
    return "\n".join(parts)


def assert_partition(text, slices, budget):
    assert slices[0]["start"] == 0
    assert slices[-1]["end"] == len(text)
    assert all(text[s["start"]:s["end"]] == s["text"] for s in slices)
    assert all(a["end"] == b["start"] for a, b in zip(slices, slices[1:], strict=False))
    assert sum(s["end"] - s["start"] for s in slices) == len(text)
    assert all(s["end"] - s["start"] <= budget for s in slices)
    assert all(set(s) == {"slice_id", "start", "end", "text", "partial"} for s in slices)


def test_dossier_is_fully_exposed_in_section_aligned_slices():
    text = dossier()
    slices = slice_document(text, max_chars=6000)
    assert_partition(text, slices, 6000)
    assert len(slices) > 1 and all(s["partial"] for s in slices)
    # No slice starts inside a section marker line.
    marker_starts = [m for m in range(len(text)) if text.startswith("=== SECTION:", m)]
    for start in marker_starts:
        assert not any(start < s["start"] < start + 40 for s in slices)


def test_oversized_single_section_is_windowed_at_break_points():
    text = dossier(sections=1, section_chars=40000)
    slices = slice_document(text, max_chars=6000)
    assert_partition(text, slices, 6000)
    assert all(s["partial"] for s in slices)
    for s in slices[:-1]:
        assert s["text"].endswith("\n")


def test_unbreakable_run_falls_back_to_exact_budget_windows():
    text = "x" * 40000
    slices = slice_document(text, max_chars=6000)
    assert [s["end"] - s["start"] for s in slices] == [6000] * 6 + [4000]
    assert_partition(text, slices, 6000)


def test_zero_or_none_budget_reads_the_whole_document_in_one_slice():
    text = dossier(sections=2)
    for budget in (0, None):
        slices = slice_document(text, max_chars=budget)
        assert len(slices) == 1
        assert slices[0]["slice_id"] == "full" and slices[0]["partial"] is False
        assert slices[0]["text"] == text


def test_packer_exposes_every_character_and_the_tail_quote_still_grounds():
    text = dossier()
    card = pack_items([{"item_id": "acme.example", "text": text}], batch_size=1,
                      max_slice_chars=6000)[0]["items"][0]
    slices = card["slices"]
    assert card["source_digest"] == hashlib.sha256(text.encode()).hexdigest()
    assert_partition(text, slices, 6000)
    host = next(s for s in slices if TAIL in s["text"])
    items, error = normalize_grounding(
        [{"item_id": card["item_id"], "claims": {},
          "quotes": [{"slice_id": host["slice_id"], "text": TAIL}]}],
        [card], min_quote_chars=15,
    )
    assert error is None, error
    quote = items[0].quotes[0]
    assert (quote.start, quote.end) == (text.index(TAIL), text.index(TAIL) + len(TAIL))
    ok, error = verify_grounding(items, [card], min_quote_chars=15)
    assert ok is True, error

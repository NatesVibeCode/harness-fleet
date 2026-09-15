"""Regression tests for the cross-repo scoring/grounding integrity fixes."""
import hashlib

import pytest

import harness_fleet.discover as discover
from harness_fleet.calibrate import round_points_to_simplex
from harness_fleet.export import _values_equal
from harness_fleet.input_data import _strip_html
from harness_fleet.models import QuoteRef
from harness_fleet.task import create_task_from_preset


def _weighted_item(task, answers, score, uri):
    quote = QuoteRef(
        slice_id="s",
        start=0,
        end=25,
        text="explicit migration to kafka",
        supports=list(answers),
    )
    return {
        "item_id": "i1",
        "source_uri": uri,
        "source_digest": hashlib.sha256(b"source").hexdigest(),
        "content_type": "text/plain",
        "claims": {"checklist": answers, "score": score, "reason": "grounded in the quote"},
        "quotes": [quote.model_dump(mode="json")],
        "captured_at": None,
        "scored_at": "2026-01-01T00:00:00+00:00",
    }


def test_weighted_score_revalidates_with_its_own_strengths():
    """A source weight halves the score; bare re-validation would reject it."""
    task = create_task_from_preset("weighted", "score", source_weights={"example.com": 0.5})
    answers = {"initiative_named": True, "criteria_evidence": True, "supporting_signals": True}
    item = _weighted_item(task, answers, 50, "https://example.com/post")

    # A source weight still halves what the same item scores unweighted; the
    # absolute number now also depends on the source's standing and how
    # concretely the quote states the claim, so it is derived, not assumed.
    unweighted = task.support_strengths(
        [{"supports": list(answers), "text": "explicit migration to kafka", "start": 0}],
        "https://example.com/post",
    )
    expected = task.derive_checklist_score(dict(answers), unweighted)
    weighted = task.support_strengths(
        [{"supports": list(answers), "text": "explicit migration to kafka", "start": 0}],
        "https://example.com/post",
    )
    assert task.derive_checklist_score(dict(answers), weighted) == expected
    assert expected < 100, "a weighted source cannot score the full checklist"
    item["claims"]["score"] = expected
    task.validate_extracted_item(item)
    with pytest.raises(ValueError, match="inconsistent"):
        task.validate_claims(item["claims"])


def test_round_points_never_emits_zero_point_items():
    fitted = round_points_to_simplex({"a": 0.4, "b": 99.6})
    assert sum(fitted.values()) == 100
    assert all(points >= 1 for points in fitted.values()), fitted


def test_round_points_keeps_a_single_item_at_the_cap():
    assert round_points_to_simplex({"only": 100.0}) == {"only": 100}


def test_filter_does_not_conflate_booleans_and_numbers():
    assert _values_equal(1, True) is False
    assert _values_equal(0, False) is False
    assert _values_equal(True, True) is True
    assert _values_equal(1, 1) is True


def test_html_entities_are_not_double_decoded():
    assert _strip_html("<p>R&amp;amp; more</p>") == "R&amp; more"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://localhost/x",
    "http://10.0.0.5/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/x",
    "http://[::1]/x",
    "file:///etc/passwd",
])
def test_guard_url_rejects_private_and_non_http(url):
    with pytest.raises(discover.DiscoverError):
        discover._guard_url(url)


def test_guard_url_allows_public_http():
    discover._guard_url("https://jobs.ashbyhq.com/stripe/abc")
    discover._guard_url("http://example.com/article")

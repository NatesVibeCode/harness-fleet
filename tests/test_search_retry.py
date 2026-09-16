"""A search surface's weather is not a verdict on a query.

Free metasearch rate-limits, drops TLS connections and answers "no results" for
queries that have results. Retrying the same query a couple of times is the
difference between "this query found nothing" and "this run found nothing" —
which is the difference a person actually sees. What a retry must never do is
turn a dead source into a live one or hide why a query produced nothing.
"""
from __future__ import annotations

import pytest

from harness_fleet import discover


def test_a_transient_failure_is_retried_and_reported(monkeypatch):
    calls = {"n": 0}

    def flaky(backend, query, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise discover.DiscoverError(f"RequestError: connection reset ({calls['n']})")
        return ["hit"]

    monkeypatch.setattr(discover, "_run_backend", flaky)
    hits, attempts = discover.run_backend_retrying("ddgs", "q", sleep=lambda _s: None, max_results=5)
    assert hits == ["hit"]
    assert attempts == 3


def test_a_persistent_failure_still_raises_with_its_own_reason(monkeypatch):
    def dead(backend, query, **kwargs):
        raise discover.DiscoverError("ddgs search failed for 'q': No results found.")

    monkeypatch.setattr(discover, "_run_backend", dead)
    with pytest.raises(discover.DiscoverError, match="No results found"):
        discover.run_backend_retrying("ddgs", "q", attempts=2, sleep=lambda _s: None, max_results=5)


def test_a_backend_that_answers_first_time_is_not_delayed(monkeypatch):
    slept: list[float] = []

    def fine(backend, query, **kwargs):
        return ["hit"]

    monkeypatch.setattr(discover, "_run_backend", fine)
    hits, attempts = discover.run_backend_retrying("ddgs", "q", sleep=slept.append, max_results=5)
    assert (hits, attempts, slept) == (["hit"], 1, [])


def test_a_non_discover_error_is_also_retried(monkeypatch):
    """A surface failure is a surface failure whatever class it arrives as."""
    calls = {"n": 0}

    def weird(backend, query, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("peer closed connection without sending TLS close_notify")
        return []

    monkeypatch.setattr(discover, "_run_backend", weird)
    hits, attempts = discover.run_backend_retrying("ddgs", "q", sleep=lambda _s: None, max_results=5)
    assert (hits, attempts) == ([], 2)


def test_no_results_is_an_answer_not_a_failure():
    """ddgs raises 'No results found'; that is a zero, not a dead source."""
    assert discover._is_empty_result(Exception("No results found."))
    assert discover._is_empty_result(Exception("no results found"))
    assert not discover._is_empty_result(Exception("RequestError: peer closed connection"))
    assert not discover._is_empty_result(Exception("HTTP 429 rate limited"))


def test_an_empty_result_backend_is_not_retried(monkeypatch):
    """A query that genuinely matches nothing must not burn three attempts.

    ddgs raises for an empty result; search_ddgs turns that back into ``[]``
    before the retry wrapper ever sees it (see tests/test_ddgs_contract.py).
    """
    calls = {"n": 0}

    def fine(backend, query, **kwargs):
        calls["n"] += 1
        return []

    monkeypatch.setattr(discover, "_run_backend", fine)
    hits, attempts = discover.run_backend_retrying("ddgs", "q", sleep=lambda _s: None, max_results=5)
    assert (hits, attempts, calls["n"]) == ([], 1, 1)

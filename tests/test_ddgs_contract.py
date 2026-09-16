"""The ddgs surface's two ways of saying nothing.

ddgs signals an empty result by raising rather than returning an empty list.
Treated as a fault, a query that genuinely matches nothing was retried three
times and then reported as a failed source — the run blamed the network for the
query's own result, which is exactly the kind of wrong reason this fleet does
not allow. It is also the only shape that tells a person "this query is
exhausted" apart from "this source is down".
"""
from __future__ import annotations

import sys
import types

import pytest

from harness_fleet import discover


class _FakeDDGS:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def __call__(self, *args, **kwargs):  # DDGS(timeout=...)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, query, max_results=10):
        return self._behaviour(query, max_results)


def _install(monkeypatch, behaviour):
    module = types.ModuleType("ddgs")

    class DDGS:
        def __init__(self, *args, **kwargs):
            self._inner = _FakeDDGS(behaviour)

        def __enter__(self):
            return self._inner

        def __exit__(self, *exc):
            return False

    module.DDGS = DDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)


def test_an_empty_result_is_a_zero_not_a_failure(monkeypatch):
    def empty(query, max_results):
        raise RuntimeError("No results found.")

    _install(monkeypatch, empty)
    assert discover.search_ddgs("nothing matches this", max_results=10) == []


def test_a_real_failure_still_raises_with_its_reason(monkeypatch):
    def broken(query, max_results):
        raise RuntimeError("peer closed connection without sending TLS close_notify")

    _install(monkeypatch, broken)
    with pytest.raises(discover.DiscoverError, match="peer closed connection"):
        discover.search_ddgs("q", max_results=10)


def test_hits_come_back_with_their_title_and_snippet(monkeypatch):
    def rows(query, max_results):
        return [{"href": "https://example.com/a", "title": "A", "body": "body text"}]

    _install(monkeypatch, rows)
    hits = discover.search_ddgs("q", max_results=10)
    assert [(h.url, h.title, h.backend) for h in hits] == [("https://example.com/a", "A", "ddgs")]

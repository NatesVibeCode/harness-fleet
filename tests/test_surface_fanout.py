"""Surfaces run wide and often: breadth costs wall-clock, not the run."""
from __future__ import annotations

import time

from harness_fleet import discover


def _record(url: str) -> discover.RawRecord:
    return discover.RawRecord(text="Acme implemented a Kafka migration for a bank.", source_uri=url, title="t")


def test_backends_are_probed_in_parallel(monkeypatch):
    """Two slow surfaces cost one surface's wall-clock, not two."""
    def slow_hits(url: str):
        time.sleep(0.30)
        return [discover.SearchHit(url=url, title="t", snippet="s", backend="slow")]

    monkeypatch.setattr(discover, "search_hn", lambda q, **kw: slow_hits("https://a.example/1"))
    monkeypatch.setattr(discover, "search_devto", lambda q, **kw: slow_hits("https://b.example/1"))
    started = time.monotonic()
    _items, report = discover.run_discovery(["q"], backends=["hn", "devto"], delay=0)
    elapsed = time.monotonic() - started
    assert elapsed < 0.55, f"surfaces were serialized: {elapsed:.2f}s"
    assert report["source_quality"]["backends"]["hn"]["hits"] == 1
    assert report["source_quality"]["backends"]["devto"]["hits"] == 1


def test_one_dead_surface_does_not_take_the_run_down(monkeypatch):
    """No surface owes us an output: the others still produce."""
    def boom(query, **kwargs):
        raise discover.DiscoverError("HTTP 403")

    monkeypatch.setattr(discover, "search_hn", boom)
    monkeypatch.setattr(discover, "search_devto", lambda q, **kw: [
        discover.SearchHit(url="https://b.example/1", title="t", snippet="s", backend="devto")])
    monkeypatch.setattr(discover, "fetch_smart_url", lambda url, **kw: _record(url))

    items, report = discover.run_discovery(["q"], backends=["hn", "devto"], delay=0)
    assert [item.source_uri for item in items] == ["https://b.example/1"]
    assert any("403" in str(entry.get("reason", "")) for entry in report["skipped"])


def test_a_surface_that_raises_unexpectedly_also_survives(monkeypatch):
    def kaboom(query, **kwargs):
        raise RuntimeError("provider bug")

    monkeypatch.setattr(discover, "search_hn", kaboom)
    monkeypatch.setattr(discover, "search_devto", lambda q, **kw: [
        discover.SearchHit(url="https://b.example/1", title="t", snippet="s", backend="devto")])
    monkeypatch.setattr(discover, "fetch_smart_url", lambda url, **kw: _record(url))
    items, _report = discover.run_discovery(["q"], backends=["hn", "devto"], delay=0)
    assert items, "an unexpected failure in one surface must not lose the others"


def test_spacing_is_per_host_not_global(monkeypatch):
    """Two hosts are not made to wait for each other; one host still is."""
    monkeypatch.setattr(discover, "search_hn", lambda q, **kw: [
        discover.SearchHit(url="https://one.example/1", title="t", snippet="s", backend="hn"),
        discover.SearchHit(url="https://two.example/1", title="t", snippet="s", backend="hn"),
    ])
    monkeypatch.setattr(discover, "fetch_smart_url", lambda url, **kw: _record(url))
    started = time.monotonic()
    discover.run_discovery(["q"], backends=["hn"], delay=0.4)
    different_hosts = time.monotonic() - started
    assert different_hosts < 0.4, f"different hosts waited on each other: {different_hosts:.2f}s"

    monkeypatch.setattr(discover, "search_hn", lambda q, **kw: [
        discover.SearchHit(url=f"https://same.example/{n}", title="t", snippet="s", backend="hn")
        for n in (1, 2)
    ])
    started = time.monotonic()
    discover.run_discovery(["q"], backends=["hn"], delay=0.4)
    same_host = time.monotonic() - started
    assert same_host >= 0.4, f"the same host was hit faster than the delay: {same_host:.2f}s"

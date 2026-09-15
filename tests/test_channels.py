"""Adding a source is dropping a file in, not editing the engine."""
import json

import pytest

from harness_fleet import channels, contracts


def test_a_python_channel_is_discovered_and_runs(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "industry_list.py").write_text(
        "CHANNEL = {'name': 'industry', 'category': 'b2b_directory_audit',"
        " 'description': 'a trade directory'}\n"
        "def fetch(query, *, max_results=10, timeout=20.0, client=None):\n"
        "    return [{'source_uri': 'https://directory.example/acme', 'text': 'Acme profile', 'title': 'Acme'}]\n",
        encoding="utf-8",
    )
    loaded = channels.load_channels(tmp_path)
    assert set(loaded) == {"industry"}
    assert loaded["industry"].category == "b2b_directory_audit"

    hits = channels.channel_hits(loaded["industry"], "acme")
    assert [hit.url for hit in hits] == ["https://directory.example/acme"]
    # The declared category is what the evidence bar sees.
    assert contracts.qualifies("q1_billable_delivery", "B2B_DIRECTORY_AUDIT") is True


def test_a_declarative_channel_needs_no_code(tmp_path, monkeypatch):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "manual.json").write_text(json.dumps({
        "name": "manual",
        "category": "vendor_registry",
        "list_url": "https://vendor.example/customers?q={query}",
        "item_pattern": r'href="(/customers/[^"]+)"',
    }), encoding="utf-8")
    loaded = channels.load_channels(tmp_path)

    from harness_fleet import discover

    monkeypatch.setattr(
        "harness_fleet.discover.fetch_text",
        lambda url, **kw: discover.RawRecord(
            text='<a href="/customers/acme">Acme</a><a href="/customers/beta">Beta</a>',
            source_uri=url,
            title="list",
        ),
    )
    hits = channels.channel_hits(loaded["manual"], "acme")
    assert [hit.url for hit in hits] == [
        "https://vendor.example/customers/acme",
        "https://vendor.example/customers/beta",
    ]
    assert all(hit.backend == "manual" for hit in hits)


def test_a_channel_must_declare_a_known_category(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "mystery.json").write_text(json.dumps({
        "name": "mystery", "category": "secret_sauce", "list_url": "https://x.example/",
    }), encoding="utf-8")
    with pytest.raises(channels.ChannelError) as err:
        channels.load_channels(tmp_path)
    assert "unknown category" in str(err.value)
    assert "promote its domain" in str(err.value)


def test_a_channel_cannot_shadow_a_built_in(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "hn.json").write_text(json.dumps({
        "name": "hn", "category": "community_and_social", "list_url": "https://x.example/",
    }), encoding="utf-8")
    with pytest.raises(channels.ChannelError, match="built-in"):
        channels.load_channels(tmp_path)


def test_an_empty_workspace_has_no_channels(tmp_path):
    assert channels.load_channels(tmp_path) == {}


def test_a_channel_is_usable_as_a_discover_backend(tmp_path, monkeypatch):
    """The point of a channel: it works from the CLI with no engine change."""
    from harness_fleet import discover

    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "industry.py").write_text(
        "CHANNEL = {'name': 'industry', 'category': 'b2b_directory_audit'}\n"
        "def fetch(query, *, max_results=10, timeout=20.0, client=None):\n"
        "    return [{'source_uri': 'https://directory.example/acme', 'text': 'Acme profile', 'title': 'Acme'}]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discover, "fetch_smart_url", lambda url, **kw: discover.RawRecord(
        text="Acme delivers Kafka migrations.", source_uri=url, title="Acme"))

    items, report = discover.run_discovery(["acme"], backends=["industry"], delay=0)
    assert [item.source_uri for item in items] == ["https://directory.example/acme"]
    assert report["source_quality"]["captured"] == 1

    # And an unknown backend still fails with the list of what is available.
    with pytest.raises(discover.DiscoverError, match="unknown search backend"):
        discover.run_discovery(["acme"], backends=["nope"], delay=0)

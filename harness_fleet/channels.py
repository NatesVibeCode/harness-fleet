"""Channels: sources a user adds without touching the engine.

The built-in backends are the ones we thought of. A person will have others —
an industry directory, a paid feed, a page they scrape by hand — and adding one
must not mean editing the engine or waiting for a release.

A channel is a file in ``<workspace>/sources/``:

* ``*.json`` — declarative, for anything that is "fetch this list page, follow
  the links that look like items". No code.
* ``*.py`` — a module with ``CHANNEL = {...}`` and ``fetch(query, *, max_results,
  timeout, client)`` for anything the declarative form cannot express.

Both must declare a ``category`` from the central taxonomy: a channel that
cannot say what it is cannot carry claims, and an unlisted category is refused
with the two ways to fix it (use an existing one, or promote the domain as a
source first). That keeps the evidence contract intact however the data arrives.
"""
from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHANNEL_DIRNAME = "sources"
RESERVED_NAMES = (
    "ddgs", "searxng", "hn", "yc", "reddit", "stackexchange", "discourse",
    "lobsters", "lemmy", "devto",
)


class ChannelError(ValueError):
    """A channel file is unusable, with the reason a person needs."""


@dataclass
class Channel:
    """One user-supplied source."""

    name: str
    category: str
    description: str = ""
    fetch: Callable[..., list[Any]] | None = None
    spec: dict[str, Any] = field(default_factory=dict)
    path: str = ""


def _validate(meta: dict[str, Any], path: Path) -> None:
    from .contracts import SOURCE_CATEGORIES

    name = str(meta.get("name") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", name):
        raise ChannelError(f"{path.name}: channel 'name' must be a short slug (got {name!r})")
    if name in RESERVED_NAMES:
        raise ChannelError(f"{path.name}: '{name}' is a built-in backend; choose another name")
    category = str(meta.get("category") or "").strip().lower()
    if not category:
        raise ChannelError(f"{path.name}: channel '{name}' must declare a category")
    if category.upper() not in SOURCE_CATEGORIES:
        known = ", ".join(sorted(name.lower() for name in SOURCE_CATEGORIES))
        raise ChannelError(
            f"{path.name}: unknown category '{category}'. Use one of: {known}. "
            f"If this is a new kind of source, promote its domain first "
            f"(see the source registry) so the taxonomy learns it."
        )


def load_channel(path: Path) -> Channel:
    """Load one channel file, python or declarative."""
    if path.suffix == ".json":
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ChannelError(f"{path.name}: not valid JSON ({exc})") from exc
        _validate(meta, path)
        return Channel(
            name=str(meta["name"]).lower(),
            category=str(meta["category"]),
            description=str(meta.get("description") or ""),
            spec=meta,
            path=str(path),
        )

    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location(f"harness_channel_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ChannelError(f"{path.name}: cannot be imported")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ChannelError(f"{path.name}: import failed ({exc})") from exc
        meta = getattr(module, "CHANNEL", None)
        if not isinstance(meta, dict):
            raise ChannelError(f"{path.name}: define CHANNEL = {{'name': ..., 'category': ...}}")
        _validate(meta, path)
        fetch = getattr(module, "fetch", None)
        if not callable(fetch):
            raise ChannelError(f"{path.name}: define fetch(query, *, max_results, timeout, client)")
        return Channel(
            name=str(meta["name"]).lower(),
            category=str(meta["category"]),
            description=str(meta.get("description") or ""),
            fetch=fetch,
            spec=meta,
            path=str(path),
        )

    raise ChannelError(f"{path.name}: channels are .json or .py files")


def load_channels(root: str | Path = ".") -> dict[str, Channel]:
    """Every channel in a workspace's sources directory, by name."""
    directory = Path(root) / CHANNEL_DIRNAME
    channels: dict[str, Channel] = {}
    if not directory.is_dir():
        return channels
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".json", ".py") or path.name.startswith("_"):
            continue
        channel = load_channel(path)
        if channel.name in channels:
            raise ChannelError(f"duplicate channel '{channel.name}' ({path.name})")
        channels[channel.name] = channel
    return channels


def channel_hits(channel: Channel, query: str, *, max_results: int = 10, timeout: float = 20.0,
                 client: Any = None) -> list[Any]:
    """Run one channel, whichever kind it is, and return discovery hits."""
    from .discover import SearchHit, fetch_text

    if channel.fetch is not None:
        records = channel.fetch(query, max_results=max_results, timeout=timeout, client=client) or []
        hits: list[SearchHit] = []
        for record in records:
            if isinstance(record, SearchHit):
                hits.append(record)
                continue
            uri = getattr(record, "source_uri", None) or (record.get("source_uri") if isinstance(record, dict) else "")
            text = getattr(record, "text", None) or (record.get("text") if isinstance(record, dict) else "")
            title = getattr(record, "title", None) or (record.get("title") if isinstance(record, dict) else "")
            if uri:
                hits.append(SearchHit(url=str(uri), title=str(title or ""), snippet=str(text or "")[:400],
                                      backend=channel.name))
        return hits[:max_results]

    # Declarative: read the list page, follow the item links, fetch each one.
    list_url = str(channel.spec.get("list_url") or "").format(query=query.replace(" ", "+"))
    pattern = re.compile(str(channel.spec.get("item_pattern") or r'href="([^"]+)"'), re.IGNORECASE)
    from urllib.parse import urljoin

    fetched = fetch_text(list_url, timeout=timeout, client=client)
    page = getattr(fetched, "text", "") or ""
    links: list[str] = []
    for match in pattern.finditer(page):
        link = urljoin(list_url, match.group(1))
        if link not in links:
            links.append(link)
    hits = [
        SearchHit(url=link, title="", snippet="", backend=channel.name)
        for link in links[: max_results]
    ]
    return hits

"""Deterministic, editable community-source presets for Career Fleet."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

COMMUNITY_SOURCE_TYPES = (
    "reddit",
    "hn",
    "stackexchange",
    "discourse",
    "lobsters",
    "lemmy",
    "devto",
)

COMMUNITY_CONFIG_FILENAME = "career_sources.json"

# These are intentionally generic. They choose public career-oriented
# communities, not a user's job preferences, location, or workplace policy.
# The file is copied into a workspace so the exact inputs remain visible and
# editable rather than hiding them in an agent prompt.
DEFAULT_COMMUNITY_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "id": "reddit-career",
        "source": "reddit",
        "target": "career",
        "query": "hiring engineer",
        "subreddit": ["forhire", "remotejs", "cscareerquestions", "experienceddevs"],
        "reddit_rss": True,
        "subreddit_sort": "new",
        "max_items": 10,
    },
    {
        "id": "hn-hiring",
        "source": "hn",
        "target": "who is hiring",
        "query": "who is hiring",
        "max_items": 10,
    },
    {
        "id": "stackexchange-workplace",
        "source": "stackexchange",
        "target": "career",
        "query": "career",
        "se_site": "workplace",
        "max_items": 10,
    },
    {
        "id": "discourse-python",
        "source": "discourse",
        "target": "https://discuss.python.org",
        "query": "hiring",
        "max_items": 10,
    },
    {
        "id": "lobsters-career",
        "source": "lobsters",
        "target": "job",
        "max_items": 10,
    },
    {
        "id": "lemmy-programming",
        "source": "lemmy",
        "target": "hiring",
        "query": "hiring",
        "lemmy_instance": "https://programming.dev",
        "max_items": 10,
    },
    {
        "id": "devto-career",
        "source": "devto",
        "target": "career",
        "max_items": 10,
    },
)


def default_community_config() -> dict[str, Any]:
    """Return a fresh copy of the generic career source plan."""
    return {
        "version": 1,
        "focus": "career",
        "sources": deepcopy(list(DEFAULT_COMMUNITY_SOURCES)),
    }


def _validate_config(config: Any, path: Path) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError(f"community source config must be an object: {path}")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"community source config needs a non-empty 'sources' list: {path}")

    seen_ids: set[str] = set()
    for entry in sources:
        if not isinstance(entry, dict):
            raise ValueError(f"each community source entry must be an object: {path}")
        entry_id = str(entry.get("id") or "").strip()
        source = str(entry.get("source") or "").strip()
        target = str(entry.get("target") or "").strip()
        if not entry_id or entry_id in seen_ids:
            raise ValueError(f"community source ids must be non-empty and unique: {path}")
        if source not in COMMUNITY_SOURCE_TYPES:
            choices = ", ".join(COMMUNITY_SOURCE_TYPES)
            raise ValueError(f"unknown community source '{source}' in {path}; choose from {choices}")
        if not target:
            raise ValueError(f"community source '{entry_id}' needs a target: {path}")
        seen_ids.add(entry_id)

        if "max_items" in entry:
            try:
                max_items = int(entry["max_items"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"community source '{entry_id}' max_items must be a positive integer") from exc
            if max_items < 1:
                raise ValueError(f"community source '{entry_id}' max_items must be a positive integer")
        for field in ("subreddit", "se_tagged"):
            if field in entry and not isinstance(entry[field], list):
                raise ValueError(f"community source '{entry_id}' {field} must be a list")
    return config


def load_community_config(path: Path | str) -> dict[str, Any]:
    """Load and validate the visible workspace source plan."""
    config_path = Path(path).expanduser()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"No community source plan at {config_path}. Run 'career-fleet init' or 'career-fleet sources --init'."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in community source config {config_path}: {exc}") from exc
    return _validate_config(config, config_path)


def write_default_community_config(path: Path | str, *, force: bool = False) -> tuple[Path, bool]:
    """Create the visible default plan without overwriting user edits."""
    config_path = Path(path).expanduser()
    if config_path.exists() and not force:
        load_community_config(config_path)
        return config_path, False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(default_community_config(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return config_path, True


def configured_sources(config: dict[str, Any], source: str | None = None, preset: str | None = None) -> list[dict[str, Any]]:
    """Return enabled source entries in file order, optionally narrowed by id/source."""
    entries = [entry for entry in config["sources"] if entry.get("enabled", True)]
    if preset:
        entries = [entry for entry in entries if entry.get("id") == preset]
        if not entries:
            raise ValueError(f"no enabled community source preset named '{preset}'")
    if source and source != "community":
        entries = [entry for entry in entries if entry.get("source") == source]
        if not entries:
            raise ValueError(f"no enabled community source preset for '{source}'")
    return [deepcopy(entry) for entry in entries]

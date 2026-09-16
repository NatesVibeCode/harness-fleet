"""Multi-source entity bundler.

Combines raw evidence hits gathered across web crawls, case studies, vendor partner
registries, B2B review platforms, community/social footprints, and ATS job postings
into consolidated, section-tagged composite dossiers per canonical entity.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .models import InputItem
from .sources import (
    ATS_DOMAINS as ATS_DOMAINS,
)
from .sources import (
    CASE_STUDY_PATH_RE as CASE_STUDY_PATH_RE,
)
from .sources import (
    COMMUNITY_DOMAINS as COMMUNITY_DOMAINS,
)
from .sources import (
    PLATFORM_HOSTS as PLATFORM_HOSTS,
)
from .sources import (
    PRACTICE_PATH_RE as PRACTICE_PATH_RE,
)

# The taxonomy and the classifier live in .sources: every fleet tool labels
# evidence the same way, and this module re-exports the names it always had
# (the redundant alias is what keeps a linter from dropping a re-export).
from .sources import (
    REGISTRY_DOMAINS as REGISTRY_DOMAINS,
)
from .sources import (
    REVIEW_DOMAINS as REVIEW_DOMAINS,
)
from .sources import (
    canonicalize_entity_id as canonicalize_entity_id,
)
from .sources import (
    classify_source_category as classify_source_category,
)


def bundle_records(
    records: Iterable[InputItem | dict[str, Any]],
    *,
    min_sources: int = 1,
    min_categories: int = 1,
) -> list[InputItem]:
    """Group multi-channel evidence hits by canonical entity into composite dossiers."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for item in records:
        if isinstance(item, InputItem):
            raw_id = item.item_id
            text = item.text
            uri = item.source_uri or ""
            metadata = dict(item.metadata or {})
        else:
            raw_id = str(item.get("item_id") or item.get("domain") or item.get("url") or "")
            text = str(item.get("text") or item.get("research") or "")
            uri = str(item.get("source_uri") or item.get("url") or "")
            metadata = dict(item.get("metadata") or {})

        canonical_id = canonicalize_entity_id(raw_id or uri)
        category = metadata.get("source_category") or classify_source_category(uri, canonical_id)

        grouped[canonical_id].append({
            "text": text.strip(),
            "source_uri": uri,
            "source_category": category,
            "metadata": metadata,
        })

    bundled_items: list[InputItem] = []

    for entity_id, hits in sorted(grouped.items()):
        if len(hits) < min_sources:
            continue

        categories: list[str] = sorted(
            {h["source_category"] for h in hits if h["source_category"]}
        )
        if len(categories) < min_categories:
            continue

        # Format composite document. The header carries the provenance the
        # scoring task needs to answer for itself (how many sources, which
        # categories) instead of making the model guess from section names.
        sections: list[str] = [
            f"# Multi-Source Evidence Dossier: {entity_id}",
            f"# source_count: {len(hits)}",
            f"# source_categories: {','.join(categories) if categories else 'unknown'}",
            "",
        ]
        all_uris: list[str] = []
        # Which surface each contributing source came from. A dossier is one row
        # per entity, so without this the yield of a run collapses to "unknown"
        # the moment it is bundled — the one place a person looks to see whether
        # a search surface is pulling its weight.
        source_backends: list[str] = []

        for i, hit in enumerate(hits, 1):
            cat = hit["source_category"].upper()
            uri_text = hit["source_uri"] or f"source_{i}"
            all_uris.append(uri_text)
            backend = str(
                (hit.get("metadata") or {}).get("discovery_backend")
                or (hit.get("metadata") or {}).get("backend")
                or ""
            ).strip()
            if backend and backend not in source_backends:
                source_backends.append(backend)
            sections.append(
                f"=== SECTION: {cat} (URI: {uri_text}) ===\n"
                f"{hit['text']}\n"
            )

        composite_text = "\n".join(sections).strip()

        bundled_items.append(
            InputItem(
                item_id=entity_id,
                text=composite_text,
                source_uri=all_uris[0] if all_uris else "",
                title=f"Partner Dossier: {entity_id}",
                metadata={
                    "entity": entity_id,
                    "source_count": len(hits),
                    "source_categories": [str(c) for c in categories],
                    "category_count": len(categories),
                    "source_uris": [str(u) for u in all_uris],
                    "source_backends": [str(name) for name in source_backends],
                },
            )
        )

    return bundled_items


def _string_list(value: Any) -> list[str]:
    """A metadata value read back as a list of strings, whatever shape it is."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def load_and_bundle(
    input_path: str | Path,
    *,
    id_column: str | None = None,
    text_column: str | None = None,
    uri_column: str | None = None,
    min_sources: int = 1,
    min_categories: int = 1,
) -> list[InputItem]:
    """Load records from CSV or JSONL and bundle by canonical entity."""
    from .input_data import load_input_items

    raw_items = load_input_items(
        input_path,
        id_column=id_column,
        text_column=text_column,
        uri_column=uri_column,
    )
    return bundle_records(raw_items, min_sources=min_sources, min_categories=min_categories)


def export_bundled_csv(
    bundled_items: Sequence[InputItem],
    output_path: str | Path,
) -> Path:
    """Export bundled items to a CSV file ready for harness-fleet run."""
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "item_id", "text", "source_uri", "source_count", "source_categories",
            "source_backends", "source_uris",
        ])
        for item in bundled_items:
            meta = item.metadata or {}
            writer.writerow([
                item.item_id,
                item.text,
                item.source_uri or "",
                meta.get("source_count", 1),
                ",".join(str(c) for c in _string_list(meta.get("source_categories"))),
                ",".join(_string_list(meta.get("source_backends"))),
                " ".join(_string_list(meta.get("source_uris"))),
            ])
    return out

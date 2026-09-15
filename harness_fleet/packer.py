"""Typed item batching and prompt packaging."""
import hashlib
import json
from collections.abc import Iterable, Iterator
from typing import Any

from .models import InputItem, PackedBatch
from .slicer import slice_document


def _pack_cards(cards: list[dict[str, Any]]) -> dict[str, Any]:
    # Sort member ids: batch identity must not depend on input order.
    batch_hash = hashlib.sha256(json.dumps(sorted(c["item_id"] for c in cards), sort_keys=True).encode()).hexdigest()[:16]
    batch = PackedBatch.model_validate({
        "batch_id": f"batch_{batch_hash}",
        "items": cards,
    })
    return batch.model_dump(mode="json")


def iter_packed_batches(
    raw_records: Iterable[InputItem | dict[str, Any]],
    batch_size: int = 6,
    max_slice_chars: int | None = 6000
) -> Iterator[dict[str, Any]]:
    """Yield packed batches while retaining only one batch of cards in memory."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")
    cards: list[dict[str, Any]] = []
    for raw_record in raw_records:
        record = raw_record if isinstance(raw_record, InputItem) else InputItem.model_validate(raw_record)
        iid = record.item_id
        text = record.text
        title = record.title
        
        slices = slice_document(text, max_chars=max_slice_chars)
        cards.append({
            "item_id": iid,
            "title": title,
            "source_uri": record.source_uri,
            "content_type": record.content_type,
            "metadata": record.metadata,
            "source_digest": hashlib.sha256(text.encode()).hexdigest(),
            "slices": slices,
            "full_char_length": len(text)
        })
        if len(cards) >= batch_size:
            yield _pack_cards(cards)
            cards = []

    if cards:
        yield _pack_cards(cards)


def pack_items(
    raw_records: Iterable[InputItem | dict[str, Any]],
    batch_size: int = 6,
    max_slice_chars: int | None = 6000
) -> list[dict[str, Any]]:
    """Compatibility wrapper that intentionally materializes all packed batches."""
    return list(iter_packed_batches(raw_records, batch_size=batch_size, max_slice_chars=max_slice_chars))

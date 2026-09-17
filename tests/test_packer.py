from harness_fleet.packer import pack_items


def test_pack_items():
    records = [
        {"item_id": f"item_{i}", "text": f"Description text for item {i}"}
        for i in range(10)
    ]
    batches = pack_items(records, batch_size=4)
    assert len(batches) == 3
    assert len(batches[0]["items"]) == 4
    assert len(batches[1]["items"]) == 4
    assert len(batches[2]["items"]) == 2
    assert batches[0]["batch_id"].startswith("batch_")


def test_batch_id_is_order_independent():
    records = [
        {"item_id": f"item_{i}", "text": f"Description text for item {i}"}
        for i in range(4)
    ]
    forward = pack_items(records, batch_size=4)[0]["batch_id"]
    backward = pack_items(list(reversed(records)), batch_size=4)[0]["batch_id"]
    assert forward == backward


def test_a_batch_is_bounded_by_size_not_only_by_item_count():
    """Five walked dossiers in one request was 188,236 tokens and scored nothing.

    `batch_size` counts items and says nothing about how big the request gets: a
    card carries every slice of its document, so the ordinary shape of a scored
    research run packed into a request no free model would take. The budget
    closes the batch before the card that would cross it.
    """
    from harness_fleet.models import InputItem
    from harness_fleet.packer import iter_packed_batches

    dossier = "Acme delivered a Kafka migration for a bank. " * 400   # ~18k chars
    items = [InputItem(item_id=f"firm{index}.example", text=dossier) for index in range(5)]

    unbounded = list(iter_packed_batches(items, batch_size=5))
    assert len(unbounded) == 1, "item count alone packs them all together"

    bounded = list(iter_packed_batches(items, batch_size=5, max_batch_chars=40_000))
    assert len(bounded) > 1, "the size budget splits the request"
    assert sum(len(batch["items"]) for batch in bounded) == 5, "and no item is lost"
    for batch in bounded:
        chars = sum(
            len(slice["text"]) for card in batch["items"] for slice in card["slices"]
        )
        assert chars <= 60_000, f"a request stayed near the budget, got {chars}"

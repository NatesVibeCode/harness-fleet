import pytest

from harness_fleet.engine import Engine
from harness_fleet.models import TaskSpec
from harness_fleet.packer import iter_packed_batches
from harness_fleet.store import MAX_NON_COUNTING_RETRIES, HarnessStore


def _task():
    return TaskSpec(
        name="streaming-recovery-test",
        batch_size=1,
        claims_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )


def test_failed_one_shot_ingestion_rolls_back_run_and_rate_limits_stop(tmp_path):
    store = HarnessStore(tmp_path / "broken.db")

    def broken_source():
        yield {"item_id": "one", "text": "source one"}
        raise RuntimeError("source failed")

    with pytest.raises(RuntimeError, match="source failed"):
        Engine(_task(), store=store).run_campaign(broken_source(), "broken-run", "input.jsonl")
    assert store.run_exists("broken-run") is False

    revision = store.register_task(_task())
    store.create_run("rate-run", revision, "input", "d" * 64, 1, 2, 1, "output")
    batch = next(iter_packed_batches([{"item_id": "one", "text": "source one"}], batch_size=1))
    store.enqueue_batches("rate-run", [batch], max_attempts_per_batch=1)
    for number in range(MAX_NON_COUNTING_RETRIES):
        lease = store.lease_batch("rate-run", f"worker-{number}")
        assert lease is not None
        store.release_lease("rate-run", lease["attempt_id"], f"worker-{number}", "429 rate limit")
    assert store.lease_batch("rate-run", "after-cap") is None
    assert next(iter(store.run_snapshot("rate-run")["batches"].values()))["status"] == "failed"

"""A run reports what its own evidence carries, and can gate on it."""
import io
import json
from argparse import Namespace
from contextlib import redirect_stdout

import pytest

from harness_fleet import cli
from harness_fleet.store import HarnessStore

CASE_STUDY = (
    "Acme implemented a Kafka migration for Northwind Bank and cut latency by 40%. "
    "Acme is certified Premier Partner. The programme covered the ingestion path, the "
    "streaming platform and the operating model, and the team published the measured "
    "result alongside the design decisions it made along the way."
)


def _namespace(**overrides):
    base = dict(
        task="demo", input=None, id_column=None, text_column=None, uri_column=None,
        run_id=None, output=None, db=None, workspace_root=None, json=False,
        route=None, exclude_route=None, provider=None, exclude_provider=None,
        free_only=False, zdr=False, no_data_collection=False,
        max_cost_in=None, max_cost_out=None, max_request_cost=None,
        openrouter_providers=None, openrouter_order=None, openrouter_ignore=None,
        profile=None, use_active_profile=False, from_studio=False,
        sessions=1, max_attempts=5, timeout=None, only_ids=None, only_ids_fuzzy=None,
        limit=None, sample=None, require_kinds=None,
    )
    base.update(overrides)
    return Namespace(**base)


def _run(tmp_path, monkeypatch, rows, *, run_id="ev-1", preset="classify", **overrides):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    # Init prints its own JSON; keep it out of the run output under test.
    with redirect_stdout(io.StringIO()):
        cli.cmd_init(Namespace(name="demo", preset=preset, batch_size=1, sample=None, db=str(db), json=True))
    input_path = tmp_path / "in.csv"
    body = "item_id,text,source_uri\n" + "".join(
        f'{item_id},"{text}",{uri}\n' for item_id, text, uri in rows
    )
    input_path.write_text(body, encoding="utf-8")
    cli.cmd_run(_namespace(
        db=str(db), workspace_root=str(tmp_path), input=str(input_path),
        id_column="item_id", text_column="text", uri_column="source_uri",
        run_id=run_id, route=["demo/fake"], **overrides,
    ))
    return db, tmp_path / "runs" / run_id


def test_a_run_writes_and_prints_what_its_evidence_carries(tmp_path, monkeypatch, capsys):
    _db, run_dir = _run(
        tmp_path, monkeypatch,
        [("acme.com", CASE_STUDY, "https://acme.com/case-studies/bank")],
    )
    out = capsys.readouterr().out
    assert "Evidence read:" in out
    assert "delivery_proof 1" in out

    readout = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    assert readout["run_id"] == "ev-1"
    assert readout["items"]["acme.com"]["kinds"]["delivery_proof"] is True
    assert readout["items"]["acme.com"]["kinds"]["independent_validation"] is False
    assert readout["kind_totals"]["delivery_proof"] == 1
    assert readout["text_missing"] == []


def test_a_first_party_claim_no_third_party_echoes_is_surfaced(tmp_path, monkeypatch, capsys):
    """The certification is claimed on the firm's own page and nowhere else."""
    _db, run_dir = _run(
        tmp_path, monkeypatch,
        [("acme.com", CASE_STUDY, "https://acme.com/case-studies/bank")],
    )
    out = capsys.readouterr().out
    assert "claims that stand alone" in out
    assert "certification_unverified" in out

    readout = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    assert [f["kind"] for f in readout["contradictions"]["acme.com"]] == ["certification_unverified"]


def test_the_same_text_on_a_vendor_page_is_independent_not_first_party(tmp_path, monkeypatch):
    """A vendor's story vouches for the firm; it is not the firm's own marketing."""
    _db, run_dir = _run(
        tmp_path, monkeypatch,
        [("acme.com", CASE_STUDY, "https://aws.amazon.com/partners/success/acme-bank/")],
    )
    readout = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    kinds = readout["items"]["acme.com"]["kinds"]
    assert kinds["independent_validation"] is True
    assert kinds["delivery_proof"] is True
    assert readout["contradictions"] == {}, "a vendor page is the corroboration itself"


def test_an_unsupported_tier_is_capped_and_the_gap_is_named(tmp_path, monkeypatch, capsys):
    """A tier_1 claim resting on the firm's own material alone is reduced.

    The *decision* is made by the readout against the central minimums; the
    score the pipeline stores is priced separately by source trust, so this
    checks both halves: the earned tier is reported, and a tier_1 claim over
    this evidence is capped with the gap named.
    """
    from harness_fleet.evidence import entity_evidence

    _db, run_dir = _run(
        tmp_path, monkeypatch,
        [("acme.com", CASE_STUDY, "https://acme.com/case-studies/bank")],
        run_id="capped-1", preset="account-research",
    )
    readout = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    read = readout["items"]["acme.com"]
    assert read["tier_claimed"] in {"tier_1", "tier_2"}, read
    if read["tier_claimed"] == "tier_1":
        assert read["tier_capped"] is True, "own material alone cannot hold tier_1"
        assert read["tier_reasons"], "and the gap is named"

    capped = entity_evidence(CASE_STUDY, tier="tier_1", source_uri="https://acme.com/case-studies/bank")
    assert capped["tier_claimed"] == "tier_1"
    assert capped["tier_supported"] == "tier_2", "own material cannot carry tier_1"
    assert capped["tier_capped"] is True
    assert capped["tier_reasons"] == ["tier_1 needs independent_validation"]

    # And the run says what it holds, so a person sees the gap without a model.
    assert "Evidence read:" in capsys.readouterr().out
def test_require_kinds_fails_the_run_and_names_the_entity(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as err:
        _run(
            tmp_path, monkeypatch,
            [("acme.com", CASE_STUDY, "https://acme.com/case-studies/bank")],
            run_id="gate-1", require_kinds=["independent_validation"],
        )
    message = str(err.value)
    assert "acme.com" in message
    assert "missing independent_validation" in message
    assert "--require-kinds" in message

    # The reasons stay on disk even though the run failed its gate.
    readout = json.loads((tmp_path / "runs" / "gate-1" / "evidence.json").read_text(encoding="utf-8"))
    assert readout["items"]["acme.com"]["kinds"]["independent_validation"] is False


def test_require_kinds_passes_when_every_entity_carries_it(tmp_path, monkeypatch):
    _db, run_dir = _run(
        tmp_path, monkeypatch,
        [
            ("acme.com", CASE_STUDY, "https://acme.com/case-studies/bank"),
            ("gamma.dev", "Gamma migrated Postgres for two clients and cut latency 30%.",
             "https://gamma.dev/case-studies/one"),
        ],
        run_id="gate-2", require_kinds=["delivery_proof"],
    )
    assert (run_dir / "evidence.json").is_file()


def test_the_readout_is_reported_in_json_mode_too(tmp_path, monkeypatch, capsys):
    _run(
        tmp_path, monkeypatch,
        [("acme.com", CASE_STUDY, "https://acme.com/case-studies/bank")],
        run_id="json-1", json=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence"]["items"]["acme.com"]["kinds"]["delivery_proof"] is True
    assert payload["evidence_path"].endswith("evidence.json")
    assert HarnessStore(tmp_path / "state.db").run_snapshot("json-1")["status"] == "completed"

import json
from argparse import Namespace

import pytest

from harness_fleet import cli
from harness_fleet.profile import IdealCompanyProfile
from harness_fleet.store import HarnessStore


def test_init_registers_task_and_writes_typed_sample(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(
        name="demo",
        preset="triage",
        batch_size=4,
        sample=None,
        db=str(db),
        json=True,
    ))

    task = HarnessStore(db).get_task("demo")
    assert set(task.claims_schema["properties"]) == {"priority", "reason"}
    assert (tmp_path / "demo.sample.jsonl").is_file()


def _run_namespace(**overrides):
    base = dict(
        task="demo", input=None, id_column=None, text_column=None, uri_column=None,
        run_id=None, output=None, db=None, workspace_root=None, json=True,
        route=None, exclude_route=None, provider=None, exclude_provider=None,
        free_only=False, zdr=False, no_data_collection=False,
        max_cost_in=None, max_cost_out=None, max_request_cost=None,
        openrouter_providers=None, openrouter_order=None, openrouter_ignore=None,
        profile=None, use_active_profile=False, from_studio=False,
        sessions=1, max_attempts=5, timeout=None, only_ids=None, only_ids_fuzzy=None,
        limit=None, sample=None,
    )
    base.update(overrides)
    return Namespace(**base)


def test_run_explains_a_database_with_no_verified_free_routes(tmp_path, monkeypatch):
    """The generic engine message hid the fix; the CLI must name it."""
    import pytest

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(name="demo", preset="classify", batch_size=1, sample=None, db=str(db), json=True))
    input_path = tmp_path / "in.csv"
    input_path.write_text("item_id,text\na.com,Some source text about a company.\n", encoding="utf-8")

    # A fresh database refreshes route prices by itself; make that refresh
    # offline and empty so the *message* for a genuinely route-less database is
    # what this test checks, with no network in the suite.
    from harness_fleet.catalog import RouteCatalog

    def empty_refresh(self, *args, **kwargs):
        return {"refreshed": 0, "routes": []}

    monkeypatch.setattr(RouteCatalog, "refresh_all", empty_refresh)

    with pytest.raises(ValueError) as err:
        cli.cmd_run(_run_namespace(
            db=str(db), workspace_root=str(tmp_path), input=str(input_path),
            id_column="item_id", text_column="text",
        ))

    message = str(err.value)
    assert "refreshed route prices" in message
    assert "quickstart" in message


def test_run_registers_the_demo_route_when_it_is_pinned(tmp_path, monkeypatch):
    """`run --route demo/fake` is the offline path and must work on a fresh DB."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(name="demo", preset="classify", batch_size=1, sample=None, db=str(db), json=True))
    input_path = tmp_path / "in.csv"
    input_path.write_text("item_id,text\na.com,Some source text about a company.\n", encoding="utf-8")

    cli.cmd_run(_run_namespace(
        db=str(db), workspace_root=str(tmp_path), input=str(input_path),
        id_column="item_id", text_column="text", run_id="offline-1", route=["demo/fake"],
    ))

    assert HarnessStore(db).run_snapshot("offline-1")["status"] == "completed"


def test_run_rejects_a_pinned_route_this_database_does_not_know(tmp_path, monkeypatch):
    import pytest

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(name="demo", preset="classify", batch_size=1, sample=None, db=str(db), json=True))
    input_path = tmp_path / "in.csv"
    input_path.write_text("item_id,text\na.com,Some source text about a company.\n", encoding="utf-8")

    with pytest.raises(ValueError) as err:
        cli.cmd_run(_run_namespace(
            db=str(db), workspace_root=str(tmp_path), input=str(input_path),
            id_column="item_id", text_column="text", route=["opencode/made-up-free"],
        ))

    assert "is not registered" in str(err.value)
    assert "routes refresh" in str(err.value)


def test_version_reports_the_running_code(monkeypatch, capsys):
    """A PYTHONPATH checkout next to an older install must report its own version."""
    import harness_fleet

    monkeypatch.setattr("sys.argv", ["harness-fleet"])
    value = cli._package_version()
    assert value == harness_fleet.__version__


def test_single_only_ids_value_selects_that_id(tmp_path, monkeypatch):
    """The help promises a comma-separated list, so one ID must work without a comma."""
    args = Namespace(only_ids="acme.dev", input="in.csv", workspace_root=str(tmp_path))
    _path, options = cli._input_source(args)
    assert options["only_ids"] == ["acme.dev"]

    # A real filter file still resolves as a file, and a missing one still errors.
    filter_file = tmp_path / "ids.csv"
    filter_file.write_text("item_id\nacme.dev\n", encoding="utf-8")
    _path, options = cli._input_source(Namespace(only_ids="ids.csv", input="in.csv", workspace_root=str(tmp_path)))
    assert options["only_ids"] == filter_file
    _path, options = cli._input_source(Namespace(only_ids="missing.csv", input="in.csv", workspace_root=str(tmp_path)))
    assert options["only_ids"] == tmp_path / "missing.csv"


def test_export_refuses_to_overwrite_the_runs_own_packet(tmp_path, monkeypatch, capsys):
    import pytest

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(name="demo", preset="classify", batch_size=1, sample=None, db=str(db), json=True))
    input_path = tmp_path / "in.csv"
    input_path.write_text("item_id,text\na.com,Some source text about a company.\n", encoding="utf-8")
    cli.cmd_run(_run_namespace(
        db=str(db), workspace_root=str(tmp_path), input=str(input_path),
        id_column="item_id", text_column="text", run_id="packet-run", route=["demo/fake"],
    ))
    packet = tmp_path / "runs/packet-run/clean_packet.json"
    before = packet.read_bytes()
    capsys.readouterr()

    # The default export writes a different file and leaves the packet alone.
    cli.cmd_export(Namespace(
        run_id="packet-run", db=str(db), format="json", output=None, top=1, rank=False,
        sort_by=None, desc=True, filter=None, adjust_scores=False, score_field="score",
        json=True, workspace_root=str(tmp_path), force=False,
    ))
    assert packet.read_bytes() == before
    assert (tmp_path / "runs/packet-run/export.json").is_file()

    # Pointing it at the packet is refused unless forced.
    with pytest.raises(ValueError) as err:
        cli.cmd_export(Namespace(
            run_id="packet-run", db=str(db), format="json", output=str(packet), top=1, rank=False,
            sort_by=None, desc=True, filter=None, adjust_scores=False, score_field="score",
            json=True, workspace_root=str(tmp_path), force=False,
        ))
    assert "stored packet" in str(err.value)
    assert packet.read_bytes() == before


def test_partners_fails_when_every_backend_is_unavailable(tmp_path, monkeypatch):
    """A missing dependency must not read as "found nothing"."""
    from harness_fleet.partner_sourcing import SourcingReport

    monkeypatch.chdir(tmp_path)

    def fake_enrich(entity, **kwargs):
        report = SourcingReport(stage="enrich", searched=5, candidates=[entity])
        report.skipped = [{"backend": "ddgs", "query": "q", "reason": "ddgs is not installed"}] * 5
        return [], report

    monkeypatch.setattr("harness_fleet.partner_sourcing.enrich_partner", fake_enrich)
    parser = cli.build_parser()
    args = parser.parse_args(["partners", "enrich", "trace3.com"])
    with pytest.raises(ValueError, match="unavailable"):
        cli.cmd_partners(args)

def test_validate_is_offline_and_strict(tmp_path, capsys):
    db = tmp_path / "state.db"
    input_path = tmp_path / "input.jsonl"
    cli.cmd_init(Namespace(
        name="demo", preset="classify", batch_size=4,
        sample=str(input_path), db=str(db), json=True,
    ))
    capsys.readouterr()

    cli.cmd_validate(Namespace(task="demo", input=str(input_path), db=str(db), json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["input_items"] == 1


def test_validate_reports_slicing_caveats_in_the_typed_report(tmp_path, capsys):
    """A JSON consumer must see the partial-window warning, not just stderr."""
    from harness_fleet.models import TaskSpec

    db = tmp_path / "state.db"
    store = HarnessStore(db)
    store.register_task(TaskSpec(
        name="sliced",
        instructions="Classify each record.",
        batch_size=4,
        max_slice_chars=300,
        claims_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        },
    ))

    input_path = tmp_path / "input.jsonl"
    input_path.write_text(
        json.dumps({"item_id": "long-1", "text": "Sentence about Kafka. " * 200}) + "\n",
        encoding="utf-8",
    )

    cli.cmd_validate(Namespace(task="sliced", input=str(input_path), db=str(db), json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["input_items"] == 1
    assert payload["errors"], "the slicing caveat must reach the typed report"
    assert "max_slice_chars=300" in payload["errors"][0]


def test_profile_command_persists_ideal_company_profile(tmp_path, capsys):
    profile_path = tmp_path / "ideal_company_profile.json"
    db_path = tmp_path / "state.db"
    profile = IdealCompanyProfile(profile_name="Database Buyers", required_stack=["PostgreSQL"])
    profile.save(profile_path)

    cli.cmd_profile(Namespace(path=str(profile_path), init=False, force=False, db=str(db_path), json=True))

    payload = json.loads(capsys.readouterr().out)
    stored = HarnessStore(db_path)
    assert payload["profile_kind"] == "ideal_company"
    assert stored.load_profile().model_dump() == profile.model_dump()
    assert stored.active_profile_revision_id() == payload["revision"]


def test_json_flag_works_before_command():
    args = cli.build_parser().parse_args(["--json", "tasks"])
    if args.global_json:
        args.json = True
    assert args.json is True


def test_presets_cover_each_named_bulk_job():
    assert set(cli.PRESETS) == {
        "account-research",
        "classify",
        "extract",
        "filter",
        "partner-research",
        "score",
        "summarize",
        "triage",
    }


def test_routes_add_and_list_cli(tmp_path, capsys):
    db = tmp_path / "routes_test.db"
    cli.cmd_routes(Namespace(
        action="add",
        route_id="local/test-model",
        add=None,
        provider="openai_compatible",
        free=True,
        input_cost=0.0,
        output_cost=0.0,
        disable=False,
        all=False,
        refresh=False,
        db=str(db),
        json=True,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "added"
    assert payload["route"]["id"] == "local/test-model"

    cli.cmd_routes(Namespace(
        action="list",
        route_id=None,
        add=None,
        provider=None,
        free=False,
        disable=False,
        all=False,
        refresh=False,
        db=str(db),
        json=True,
    ))
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload["count"] == 1
    assert list_payload["routes"][0]["id"] == "local/test-model"


def test_routes_add_cost_safety_defaults(tmp_path, capsys):
    db = tmp_path / "routes_safety.db"
    # Adding a route without --free or explicit costs must default to unknown price state and None costs
    cli.cmd_routes(Namespace(
        action="add",
        route_id="groq/llama-3.3-70b-versatile",
        add=None,
        provider="groq",
        free=False,
        input_cost=None,
        output_cost=None,
        disable=False,
        all=False,
        refresh=False,
        db=str(db),
        json=True,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "added"
    assert payload["route"]["price_state"] == "unknown"
    assert payload["route"]["cost_per_1k_input"] is None
    assert payload["route"]["cost_per_1k_output"] is None

    # Adding a route with --free must register as price_observed_zero and 0.0
    cli.cmd_routes(Namespace(
        action="add",
        route_id="ollama/llama3.2:latest",
        add=None,
        provider="ollama",
        free=True,
        input_cost=None,
        output_cost=None,
        disable=False,
        all=False,
        refresh=False,
        db=str(db),
        json=True,
    ))
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "added"
    assert payload["route"]["price_state"] == "price_observed_zero"
    assert payload["route"]["cost_per_1k_input"] == 0.0
    assert payload["route"]["cost_per_1k_output"] == 0.0


def test_cli_policy_flag_parsing():
    parser = cli.build_parser()

    # Comma-separated list and order
    args = parser.parse_args([
        "run", "demo", "--input", "in.jsonl",
        "--openrouter-providers", "Anthropic,Together",
        "--openrouter-order", "latency",
        "--max-request-cost", "0.05",
    ])
    policy = cli._extract_policy(args)
    assert policy.openrouter_providers == ["Anthropic", "Together"]
    assert policy.openrouter_order == ["latency"]
    assert policy.max_request_cost == 0.05

    # Repeatable flags
    args2 = parser.parse_args([
        "run", "demo", "--input", "in.jsonl",
        "--openrouter-provider", "Together",
        "--openrouter-provider", "DeepInfra",
    ])
    policy2 = cli._extract_policy(args2)
    assert policy2.openrouter_providers == ["Together", "DeepInfra"]


def test_export_and_status_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import csv

    from harness_fleet.store import HarnessStore

    db = tmp_path / "run_test.db"
    store = HarnessStore(db)

    # Register task
    cli.cmd_init(Namespace(
        name="test-triage",
        preset="triage",
        batch_size=2,
        sample=None,
        db=str(db),
        json=True,
    ))
    capsys.readouterr()

    # Create run in store
    task = store.get_task("test-triage")
    rev_id = store.register_task(task)
    run_id = "test-run-1"
    store.create_run(
        run_id=run_id,
        task_revision_id=rev_id,
        input_path="input.csv",
        input_digest="d" * 64,
        total_items=1,
        max_attempts=10,
        batch_size=2,
        output_path="out.json",
    )
    from harness_fleet.models import ProviderReceipt
    from harness_fleet.packer import pack_items
    batch = pack_items([{"item_id": "item-1", "text": "broken button error"}], batch_size=2)[0]
    store.enqueue_batches(run_id, [batch], max_attempts_per_batch=5)
    lease = store.lease_batch(run_id, "worker-1")
    assert lease is not None
    receipt = ProviderReceipt(
        id="rec-1",
        provider="openai_compatible",
        requested_route="local/test-model",
        status="complete",
        cost=0.0,
        cost_status="reported_zero",
        usage={"total_tokens": 10},
        duration_seconds=0.2,
    )
    store.complete_batch(
        run_id=run_id,
        attempt_id=lease["attempt_id"],
        worker_id="worker-1",
        results=[{
            "item_id": "item-1",
            "source_digest": batch["items"][0]["source_digest"],
            "content_type": "text/plain",
            "claims": {"priority": "high", "reason": "broken button"},
            "quotes": [{"slice_id": "full", "start": 0, "end": 6, "text": "broken"}],
        }],
        receipt=receipt,
    )

    # Test status CLI
    cli.cmd_status(Namespace(run_id=run_id, watch=False, interval=1.0, db=str(db), json=True))
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["run_id"] == run_id
    assert status_out["verified_items"] == 1

    # Test export CSV CLI
    csv_file = tmp_path / "exported.csv"
    cli.cmd_export(Namespace(run_id=run_id, format="csv", output=str(csv_file), db=str(db), json=True))
    export_out = json.loads(capsys.readouterr().out)
    assert export_out["format"] == "csv"
    assert csv_file.is_file()

    with open(csv_file, encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["item_id"] == "item-1"
        assert reader[0]["priority"] == "high"
        assert reader[0]["reason"] == "broken button"
        assert reader[0]["primary_quote_text"] == "broken"

    # Test export with sort and rank CLI
    ranked_csv = tmp_path / "ranked.csv"
    cli.cmd_export(Namespace(
        run_id=run_id,
        format="csv",
        output=str(ranked_csv),
        sort_by="priority",
        desc=True,
        top=1,
        rank=True,
        filter='{"all": [{"field": "priority", "value": "high"}]}',
        db=str(db),
        json=True,
    ))
    assert ranked_csv.is_file()
    with open(ranked_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["rank"] == "1"
    assert rows[0]["item_id"] == "item-1"


def test_init_presets_score_and_account_research(tmp_path, capsys):
    db = tmp_path / "init_test.db"

    # Test score preset
    cli.cmd_init(Namespace(
        name="score-demo",
        preset="score",
        from_example=None,
        label_column=None,
        batch_size=5,
        sample=str(tmp_path / "score_sample.jsonl"),
        db=str(db),
        json=True,
    ))
    out = json.loads(capsys.readouterr().out)
    assert out["created"] is True
    assert out["task"] == "score-demo"
    assert "score" in out["claims_schema"]["properties"]
    assert "reason" in out["claims_schema"]["properties"]

    # Test filter preset
    cli.cmd_init(Namespace(
        name="filter-demo",
        preset="filter",
        from_example=None,
        label_column=None,
        batch_size=5,
        sample=str(tmp_path / "filter_sample.jsonl"),
        db=str(db),
        json=True,
    ))
    out = json.loads(capsys.readouterr().out)
    assert out["created"] is True
    assert "passed" in out["claims_schema"]["properties"]



def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_rescore_links_lineage_and_history(tmp_path, monkeypatch, capsys):
    """cmd_rescore scores fresh evidence under a new run and history shows both rounds."""
    from harness_fleet.catalog import RouteCatalog

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    cli.cmd_init(Namespace(
        name="rescore-demo", preset="score", batch_size=4,
        source_weight=[], half_life=[],
        sample=str(tmp_path / "sample.jsonl"), db=str(db), json=True,
    ))
    capsys.readouterr()
    catalog = RouteCatalog(db_path=db)
    catalog.add_route(
        "demo/fake", provider="demo",
        cost_per_1k_input=0.0, cost_per_1k_output=0.0,
        enabled=True, price_state="price_observed_zero",
    )
    round1 = tmp_path / "round1.jsonl"
    _write_jsonl(round1, [{
        "item_id": "acme-r1",
        "text": "Acme is migrating its platform to Kubernetes this quarter with senior hiring underway.",
        "source_uri": "https://boards.greenhouse.io/acme/1",
        "metadata": {"entity": "acme", "captured_at": "2026-08-01T00:00:00+00:00"},
    }])
    run_ns = dict(
        task="rescore-demo", input=str(round1), route=["demo/fake"],
        id_column=None, text_column=None, title_column=None, uri_column=None,
        only_ids=None, only_ids_fuzzy=False, sessions=1, max_attempts=10,
        output=None, profile=None, use_active_profile=False,
        workspace_root=".", db=str(db), json=True,
    )
    cli.cmd_run(Namespace(run_id="round-1", **run_ns))
    capsys.readouterr()
    round2 = tmp_path / "round2.jsonl"
    _write_jsonl(round2, [{
        "item_id": "acme-r2",
        "text": "Acme was acquired; the combined group is doubling platform investment this year.",
        "source_uri": "https://example.com/press/acme-acquired",
        "metadata": {"entity": "acme", "captured_at": "2026-09-01T00:00:00+00:00"},
    }])
    cli.cmd_rescore(Namespace(
        parent_run="round-1", task=None, input=str(round2), route=["demo/fake"],
        id_column=None, text_column=None, title_column=None, uri_column=None,
        only_ids=None, only_ids_fuzzy=False, sessions=1, max_attempts=10,
        run_id="round-2", output=None, profile=None, use_active_profile=False,
        workspace_root=".", db=str(db), json=True,
    ))
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == "round-2" and out["parent_run"] == "round-1"
    assert out["result"]["total_verified_records"] == 1

    store = HarnessStore(db)
    assert store.run_snapshot("round-2")["parent_run_id"] == "round-1"
    rows = store.get_entity_history("acme")
    assert [row["run_id"] for row in rows] == ["round-1", "round-2"]
    # Each round carries its own source, and the contract prices them: an ATS
    # board outranks a generic press page for the same kind of claim.
    scores = {row["run_id"]: row["score"] for row in rows}
    # Round 1 is an ATS board, which may carry the claim; round 2 is a generic
    # press page, which is a lead rather than evidence, so it does not score.
    assert scores["round-1"] and scores["round-1"] > 0, scores
    assert scores["round-2"] in (0, 0.0, None), f"a generic page is not evidence: {scores}"

    cli.cmd_history(Namespace(entity="acme", db=str(db), json=True))
    history = json.loads(capsys.readouterr().out)
    assert history["entity"] == "acme" and len(history["rounds"]) == 2

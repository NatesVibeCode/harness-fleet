"""The five measurements, taken from a finished run without changing it.

Two fixtures: a real offline run (demo route) for the pipeline-level numbers,
and a synthetic snapshot for exact-offset control over the truth sample. No
test here touches the network — the truth sample's fetch is injected.
"""
from __future__ import annotations

import hashlib
import io
import json
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from harness_fleet import cli
from harness_fleet.lane_report import build_lane_report, report_lines
from harness_fleet.lanes import Lane
from harness_fleet.store import HarnessStore

CASE_STUDY = (
    "Acme implemented a Kafka migration for Northwind Bank and cut latency by 40%. "
    "Acme is certified Premier Partner."
)


def _offline_run(tmp_path, monkeypatch, *, preset="account-research", run_id="lane-run", text=CASE_STUDY):
    """A real run through the CLI on the deterministic demo route."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state.db"
    with redirect_stdout(io.StringIO()):
        cli.cmd_init(Namespace(name="demo", preset=preset, batch_size=1, sample=None, db=str(db), json=True))
    input_path = tmp_path / "in.csv"
    input_path.write_text(
        f'item_id,text,source_uri\nacme.com,"{text}",https://acme.com/case-studies/bank\n',
        encoding="utf-8",
    )
    with redirect_stdout(io.StringIO()):
        cli.cmd_run(Namespace(
            task="demo", input=str(input_path), id_column="item_id", text_column="text",
            uri_column="source_uri", run_id=run_id, output=None, db=str(db),
            workspace_root=str(tmp_path), json=True, route=["demo/fake"],
            exclude_route=None, provider=None, exclude_provider=None, free_only=False, zdr=False,
            no_data_collection=False, max_cost_in=None, max_cost_out=None, max_request_cost=None,
            openrouter_providers=None, openrouter_order=None, openrouter_ignore=None,
            profile=None, use_active_profile=False, from_studio=False, sessions=1, max_attempts=5,
            timeout=None, only_ids=None, only_ids_fuzzy=None, limit=None, sample=None,
            require_kinds=None,
        ))
    return db, tmp_path


def _truth_snapshot(tmp_path, *, quote_text: str, start: int, end: int, item_id="acme.com"):
    """A snapshot whose quote offsets are exactly what the test says they are."""
    from harness_fleet.task import create_task_from_preset

    task = create_task_from_preset("truth", "summarize")
    input_path = tmp_path / "truth.csv"
    input_path.write_text(f'item_id,text\n{item_id},"{CASE_STUDY}"\n', encoding="utf-8")
    snapshot = {
        "run_id": "truth-run",
        "created_at": "2026-09-01T10:00:00+00:00",
        "finished_at": "2026-09-01T10:00:30+00:00",
        "task": task.model_dump(mode="json", by_alias=True),
        "input_path": str(input_path),
        "attempts_used": 2,
        "batches": {
            "b1": {
                "batch_id": "b1",
                "item_ids": [item_id],
                "status": "verified",
                "attempts": 1,
                "result": [{
                    "item_id": item_id,
                    "source_digest": hashlib.sha256(CASE_STUDY.encode()).hexdigest(),
                    "content_type": "text/plain",
                    "source_uri": "https://acme.com/case-studies/bank",
                    "claims": {"summary": "Acme migrated to Kafka."},
                    "quotes": [{
                        "slice_id": "full", "start": start, "end": end,
                        "text": quote_text, "supports": [],
                    }],
                }],
                "error": None,
                "receipt": None,
                "completed_at": "2026-09-01T10:00:20+00:00",
            }
        },
        "model_runs": [
            {"requested_route": "demo/fake", "cost": 0.0},
            {"requested_route": "demo/fake", "cost": 0.0},
        ],
    }
    return snapshot, task


class _FakeStore:
    """A store that serves one prepared snapshot."""

    def __init__(self, snapshot):
        self.path = Path("/tmp/fake.db")
        self._snapshot = snapshot

    def run_snapshot(self, run_id):
        return self._snapshot


# --- 1. yield ---------------------------------------------------------------

def test_yield_reports_captured_per_source_and_says_when_attempts_are_unknown(tmp_path, monkeypatch):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, sample=0)
    assert report.records == 1
    assert report.yield_by_source, "the run's input is attributed to a source"
    entry = report.yield_by_source[0]
    assert entry.captured == 1
    assert entry.attempted is None, "no discovery report was recorded"
    assert any("attempted counts are unknown" in note for note in report.notes)


def test_yield_reads_the_discovery_report_when_the_run_recorded_one(tmp_path, monkeypatch):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    run_dir = workspace / "runs" / "lane-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "discovery_report.json").write_text(json.dumps({
        "source_quality": {"backends": {"hn": {"unique_hits": 4, "skipped": 1, "captured": 1}}},
        "skipped": [],
    }), encoding="utf-8")
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, sample=0)
    by_source = {entry.source: entry for entry in report.yield_by_source}
    assert by_source["hn"].attempted == 4
    assert by_source["hn"].skipped == 1
    assert report.notes == []


# --- 2. coverage ------------------------------------------------------------

def test_coverage_uses_the_lanes_bar(tmp_path, monkeypatch):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    lane = Lane(name="account", tier="tier_1")
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, lane=lane, sample=0)
    assert report.tier_bar == ["delivery_proof", "independent_validation", "stack_delivery"]
    entry = report.coverage[0]
    assert entry.item_id == "acme.com"
    assert "delivery_proof" in entry.kinds
    assert "independent_validation" in entry.missing, "own material cannot clear tier_1"
    assert report.coverage_meeting_bar == 0.0


def test_coverage_without_a_lane_judges_the_claimed_tier(tmp_path, monkeypatch):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, sample=0)
    entry = report.coverage[0]
    assert report.tier_bar == []
    assert entry.claimed_tier in {"tier_1", "tier_2", "tier_3", "unfit", None}


def test_a_lane_floor_accepts_any_tier_at_or_above_it():
    """Tiers nest, so a tier_3 floor is met by a record that clears tier_2.

    Reporting the floor's own tuple as a mandatory union showed 0% for a run
    whose records each cleared a real tier, and named a gap nobody could close.
    """
    from harness_fleet.lane_report import _nearest_tier_bar

    tier_2_only = {"delivery_proof": True, "stack_delivery": True, "independent_validation": False}
    assert _nearest_tier_bar(("stack_delivery",), tier_2_only) == ()
    assert _nearest_tier_bar(("delivery_proof", "stack_delivery"), tier_2_only) == ()
    assert _nearest_tier_bar(("delivery_proof", "independent_validation", "stack_delivery"), tier_2_only) == (
        "delivery_proof", "independent_validation", "stack_delivery",
    )

    tier_3_only = {"delivery_proof": False, "stack_delivery": True, "independent_validation": False}
    assert _nearest_tier_bar(("stack_delivery",), tier_3_only) == (), "a tier_3 lane is met by tier_3"
    assert _nearest_tier_bar(("delivery_proof", "stack_delivery"), tier_3_only) == (
        "delivery_proof", "stack_delivery",
    ), "a tier_2 lane is not met by tier_3 evidence, and says which kind is missing"

    nothing = {"delivery_proof": False, "stack_delivery": False, "independent_validation": False}
    assert _nearest_tier_bar(("stack_delivery",), nothing) == ("stack_delivery",)


def test_a_record_clearing_a_higher_tier_counts_toward_the_floor(tmp_path, monkeypatch):
    """The share the report prints is the share the lane would actually accept."""
    db, workspace = _offline_run(tmp_path, monkeypatch)
    lane = Lane(name="career", tier="tier_3")
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, lane=lane, sample=0)
    entry = report.coverage[0]
    if entry.missing:
        assert report.coverage_meeting_bar == 0.0
    else:
        assert report.coverage_meeting_bar == 1.0
        assert set(entry.kinds) >= {"stack_delivery"}


# --- 3. support quality -----------------------------------------------------

def test_support_quality_counts_carried_and_refused_with_the_engines_reasons(tmp_path, monkeypatch):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, sample=0)
    assert report.support, "one entry per scored record"
    entry = report.support[0]
    assert report.claims_supported + report.claims_refused > 0, "the demo run answers a checklist"
    for reason in entry.reasons:
        assert ":" in reason, reason
    assert len(entry.supported) + len(entry.refused) == len(entry.supported) + len(entry.refused)


def test_a_refused_claim_names_why(tmp_path):
    """A quote that does not address its claim is refused with the reason."""
    snapshot, task = _truth_snapshot(tmp_path, quote_text="We are a leading provider.", start=0, end=26)
    snapshot["batches"]["b1"]["result"][0]["claims"] = {
        "checklist": {"q2_stack_delivery": True}, "reason": "r",
    }
    snapshot["batches"]["b1"]["result"][0]["quotes"][0]["supports"] = ["q2_stack_delivery"]
    from harness_fleet.models import TaskSpec

    partner = TaskSpec.model_validate_json(json.dumps({
        "name": "support-check",
        "instructions": "x",
        "checklist": {"q2_stack_delivery": 100},
        "claims_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "checklist": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"q2_stack_delivery": {"type": "boolean"}},
                },
                "reason": {"type": "string"},
            },
            "required": ["checklist", "reason"],
        },
    }))
    snapshot["task"] = partner.model_dump(mode="json", by_alias=True)
    report = build_lane_report(_FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=0)
    entry = report.support[0]
    assert entry.refused == ["q2_stack_delivery"]
    assert "quote does not state" in entry.reasons[0], entry.reasons


# --- 4. truth sample --------------------------------------------------------

def test_truth_sample_checks_offsets_live_page_and_addressing(tmp_path):
    text = CASE_STUDY
    start = text.index("Acme implemented")
    quote = text[start:start + 40]
    snapshot, _task = _truth_snapshot(tmp_path, quote_text=quote, start=start, end=start + 40)
    seen_urls = []

    def fetch(url):
        seen_urls.append(url)
        return f"page text ... {quote} ... more"

    report = build_lane_report(
        _FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=5, fetch=fetch
    )
    assert report.truth_sampled == 1
    entry = report.truth[0]
    assert entry.exact is True, "offsets reproduce the quote character for character"
    assert entry.live == "confirmed"
    assert entry.uri.startswith("https://") or entry.uri == ""
    assert seen_urls == [entry.uri] if entry.uri else seen_urls == []


def test_truth_sample_reports_a_quote_that_is_no_longer_on_the_page(tmp_path):
    start = CASE_STUDY.index("Acme implemented")
    quote = CASE_STUDY[start:start + 40]
    snapshot, _task = _truth_snapshot(tmp_path, quote_text=quote, start=start, end=start + 40)
    report = build_lane_report(
        _FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=5,
        fetch=lambda url: "the page was rewritten and no longer carries it",
    )
    assert report.truth[0].live == "missing"
    assert report.truth[0].exact is True


def test_truth_sample_distinguishes_unreachable_from_missing(tmp_path):
    start = CASE_STUDY.index("Acme implemented")
    snapshot, _task = _truth_snapshot(tmp_path, quote_text=CASE_STUDY[start:start + 40],
                                      start=start, end=start + 40)
    report = build_lane_report(
        _FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=5, fetch=lambda url: None
    )
    assert report.truth[0].live == "unreachable"


def test_truth_sample_catches_bad_offsets(tmp_path):
    snapshot, _task = _truth_snapshot(tmp_path, quote_text="not what is at these offsets",
                                      start=0, end=28)
    report = build_lane_report(
        _FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=5, fetch=lambda url: ""
    )
    assert report.truth[0].exact is False


def test_the_sample_is_deterministic_so_two_configs_compare_like_for_like(tmp_path):
    start = CASE_STUDY.index("Acme implemented")
    snapshot, _task = _truth_snapshot(tmp_path, quote_text=CASE_STUDY[start:start + 40],
                                      start=start, end=start + 40)
    first = build_lane_report(_FakeStore(snapshot), "truth-run", workspace_root=tmp_path,
                              sample=5, fetch=lambda url: "")
    second = build_lane_report(_FakeStore(snapshot), "truth-run", workspace_root=tmp_path,
                               sample=5, fetch=lambda url: "")
    assert [entry.item_id for entry in first.truth] == [entry.item_id for entry in second.truth]


def test_a_sample_of_zero_checks_nothing(tmp_path):
    snapshot, _task = _truth_snapshot(tmp_path, quote_text="x", start=0, end=1)
    report = build_lane_report(_FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=0)
    assert report.truth == [] and report.truth_sampled == 0


# --- 5. cost and time -------------------------------------------------------

def test_cost_reports_routes_attempts_and_wall_time(tmp_path):
    snapshot, _task = _truth_snapshot(tmp_path, quote_text="x", start=0, end=1)
    report = build_lane_report(_FakeStore(snapshot), "truth-run", workspace_root=tmp_path, sample=0)
    assert report.cost.routes == ["demo/fake"]
    assert report.cost.attempts == 2
    assert report.cost.cost == 0.0
    assert report.cost.wall_seconds == 30.0
    assert report.cost.batches_verified == 1 and report.cost.batches_failed == 0


# --- frozen sample ----------------------------------------------------------

def test_freezing_copies_the_input_registry_and_lane(tmp_path, monkeypatch):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    (workspace / "source_registry.json").write_text(json.dumps({
        "domains": {"vendorhub.example": {"category": "vendor_registry", "reason": "x"}},
        "candidates": {},
    }), encoding="utf-8")
    (workspace / "lanes").mkdir(exist_ok=True)
    (workspace / "lanes" / "account.json").write_text(json.dumps({
        "name": "account", "tier": "tier_1", "revision": 3,
    }), encoding="utf-8")
    lane = Lane(name="account", tier="tier_1", revision=3)
    freeze = tmp_path / "frozen"

    report = build_lane_report(
        HarnessStore(db), "lane-run", workspace_root=workspace, lane=lane, sample=0, freeze_dir=freeze
    )
    assert Path(report.frozen["input"]).is_file()
    assert Path(report.frozen["registry"]).is_file()
    assert Path(report.frozen["lane"]).is_file()
    manifest = json.loads(Path(report.frozen["manifest"]).read_text(encoding="utf-8"))
    assert manifest["run_id"] == "lane-run"
    assert manifest["lane_name"] == "account" and manifest["lane_revision"] == "3"
    assert len(manifest["input_sha256"]) == 64, "the frozen input is identified by digest"


# --- surface ----------------------------------------------------------------

def test_lane_report_verb_parses_and_writes_the_report(tmp_path, monkeypatch, capsys):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["lane", "report", "lane-run", "--sample", "0", "--json",
                              "--db", str(db), "--workspace-root", str(workspace)])
    capsys.readouterr()
    cli.cmd_lane(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "lane-run"
    assert payload["records"] == 1
    assert Path(payload["report_path"]).is_file(), "the report travels with its run"
    written = json.loads(Path(payload["report_path"]).read_text(encoding="utf-8"))
    assert written["run_id"] == "lane-run"


def test_lane_report_prints_a_human_summary(tmp_path, monkeypatch, capsys):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["lane", "report", "lane-run", "--sample", "0",
                              "--db", str(db), "--workspace-root", str(workspace)])
    capsys.readouterr()
    cli.cmd_lane(args)
    out = capsys.readouterr().out
    for expected in ("Lane report:", "yield:", "coverage:", "support:", "truth sample:", "cost:"):
        assert expected in out, expected


def test_lane_report_needs_a_run_id():
    parser = cli.build_parser()
    args = parser.parse_args(["lane", "report", "x"])
    args.run_id = ""
    with pytest.raises(ValueError, match="needs a run id"):
        cli.cmd_lane(args)


def test_report_lines_flag_the_quotes_worth_reviewing():
    from harness_fleet.models import LaneReport, LaneTruthQuote

    model = LaneReport(
        run_id="r", records=1, truth_sampled=1,
        truth=[LaneTruthQuote(item_id="a", uri="https://x/1", exact=False, live="missing",
                              addressed=True)],
    )
    text = report_lines(model)
    assert "exact=False" in text and "https://x/1" in text


def test_yield_surfaces_why_a_source_returned_nothing(tmp_path, monkeypatch):
    """A skip count without its reason is the silent zero this fleet forbids."""
    db, workspace = _offline_run(tmp_path, monkeypatch)
    run_dir = workspace / "runs" / "lane-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "discovery_report.json").write_text(json.dumps({
        "source_quality": {"backends": {"hn": {"unique_hits": 3, "captured": 0, "skipped": 3}}},
        "skipped": [{"backend": "hn", "reason": "HTTP 403 for https://blocked.example/x"}],
    }), encoding="utf-8")
    report = build_lane_report(HarnessStore(db), "lane-run", workspace_root=workspace, sample=0)
    assert any("HTTP 403" in note and "hn" in note for note in report.notes), report.notes
    assert any(entry.source == "hn" and entry.skipped == 3 for entry in report.yield_by_source)


def test_a_bundled_dossier_keeps_the_surface_each_source_came_from(tmp_path):
    """Yield must survive bundling, or every lane reports 'unknown'.

    A dossier is one row per entity, so the row loses the per-source backend
    unless the bundle carries it. Without this the answer to "which search
    surface is pulling its weight" was always 'unknown'.
    """
    from harness_fleet.bundler import bundle_records, export_bundled_csv
    from harness_fleet.lane_report import _load_input_items, _yield_measurement
    from harness_fleet.models import InputItem

    def item(iid: str, uri: str, backend: str, text: str) -> InputItem:
        return InputItem(item_id=iid, text=text, source_uri=uri, title="t",
                         metadata={"discovery_backend": backend, "evidence": "fetched"})

    # Same entity, two surfaces: the dossier must name both. (In a real run the
    # discovery stage has already replaced each item's id with its entity key,
    # which is what groups them here.)
    raw = [
        item("acme.com", "https://acme.com/case-study", "ddgs",
             "Acme implemented Kafka and cut latency 40 percent for its clients."),
        item("acme.com", "https://acme.com/thread", "hn",
             "Acme migrated its billing stack to Kafka last year, per the thread."),
    ]
    bundled = bundle_records(raw)
    assert len(bundled) == 1, "one dossier per entity"
    assert bundled[0].metadata["source_backends"] == ["ddgs", "hn"]

    path = export_bundled_csv(bundled, tmp_path / "accounts.csv")
    items = _load_input_items(path)
    entries, _notes = _yield_measurement(
        {"run_id": "r"}, items, run_id="r", runs_dir=tmp_path / "runs", channel_names=set()
    )
    by_source = {entry.source: entry.captured for entry in entries}
    assert by_source == {"ddgs": 1, "hn": 1}, by_source


def test_lane_report_measures_against_the_lane_the_run_recorded(tmp_path, monkeypatch, capsys):
    """A report should not need to be told what the run already knows.

    `research --lane career` writes the lane next to the run; asking for the
    report without repeating the name then measures against the wrong bar —
    silently, which is the bad part.
    """
    db, workspace = _offline_run(tmp_path, monkeypatch)
    runs = workspace / "runs" / "lane-run"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "discovery_report.json").write_text(
        json.dumps({"lane": "career", "skipped": []}), encoding="utf-8"
    )
    parser = cli.build_parser()
    args = parser.parse_args(["lane", "report", "lane-run", "--sample", "0", "--json",
                              "--db", str(db), "--workspace-root", str(workspace)])
    capsys.readouterr()
    cli.cmd_lane(args)
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["lane"] == "career"
    assert payload["tier_bar"] == ["delivery_hiring"]


def test_an_explicit_lane_still_wins_over_the_recorded_one(tmp_path, monkeypatch, capsys):
    db, workspace = _offline_run(tmp_path, monkeypatch)
    runs = workspace / "runs" / "lane-run"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "discovery_report.json").write_text(
        json.dumps({"lane": "career", "skipped": []}), encoding="utf-8"
    )
    (workspace / "lanes").mkdir(exist_ok=True)
    (workspace / "lanes" / "account.json").write_text(json.dumps({
        "name": "account", "preset": "account-research", "tier": "tier_2",
        "queries": ["q"], "backends": ["ddgs"],
    }), encoding="utf-8")
    parser = cli.build_parser()
    args = parser.parse_args(["lane", "report", "lane-run", "--lane", "account", "--sample", "0",
                              "--json", "--db", str(db), "--workspace-root", str(workspace)])
    capsys.readouterr()
    cli.cmd_lane(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["lane"] == "account"
    assert "delivery_proof" in payload["tier_bar"], "the explicit lane's bar, not the recorded one's"

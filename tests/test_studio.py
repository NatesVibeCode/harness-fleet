"""Studio API tests over an ephemeral localhost server (stdlib only)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from harness_fleet.catalog import PriceState, RouteCatalog
from harness_fleet.studio import StudioHandler


@pytest.fixture()
def server(tmp_path: Path):
    db_path = tmp_path / "studio.db"
    catalog = RouteCatalog(db_path=db_path)
    catalog.add_route(
        route_id="demo/fake",
        provider="demo",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        enabled=True,
        price_state=PriceState.PRICE_OBSERVED_ZERO.value,
        verification_source="test",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
    httpd.workspace_root = str(tmp_path)
    httpd.db_path = str(db_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", tmp_path
    httpd.shutdown()
    thread.join(timeout=5)


def _call(base, method, path, body=None, headers=None):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers=merged,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_harnesses_lists_seven_equal_entries(server):
    base, _ = server
    status, payload = _call(base, "GET", "/api/harnesses")
    assert status == 200
    assert [h["name"] for h in payload["harnesses"]] == [
        "opencode", "claude", "codex", "cursor", "grok", "muse", "antigravity",
    ]
    assert all("available" in h and "binary" in h for h in payload["harnesses"])


def test_routes_and_presets(server):
    base, _ = server
    status, payload = _call(base, "GET", "/api/routes")
    assert status == 200
    assert "demo/fake" in {r["id"] for r in payload["routes"]}
    status, payload = _call(base, "GET", "/api/presets")
    assert status == 200
    assert "score" in payload["presets"]


def test_run_plan_with_trust_note_and_lineage(server):
    base, _ = server
    items = [
        {"item_id": "i1", "text": "The checkout button gave a 500 error and blocks purchases."},
        {"item_id": "i2", "text": "Fast shipping and recyclable packaging was appreciated."},
    ]
    status, payload = _call(base, "POST", "/api/runs", {"steps": [
        {"preset": "classify", "task": "studio-triage", "items": items,
         "routes": ["demo/fake"], "max_attempts": 10, "concurrency": 2},
        {"preset": "score", "task": "studio-score", "items": items,
         "routes": ["demo/fake"], "max_attempts": 10, "concurrency": 2,
         "allow_paid": True, "paid_note": "demo stands in for a trusted paid route"},
    ]})
    assert status == 200
    first, second = payload["steps"]
    assert first["status"] == "completed" and first["verified"] == 2
    assert second["status"] == "completed" and second["verified"] == 2
    assert second["allow_paid"] is True
    assert second["paid_note"] == "demo stands in for a trusted paid route"
    status, snapshot = _call(base, "GET", f"/api/runs/{second['run_id']}")
    assert status == 200
    assert snapshot["parent_run_id"] == first["run_id"]


def test_run_validation_is_fail_closed(server):
    base, _ = server
    status, _ = _call(base, "POST", "/api/runs", {"steps": []})
    assert status == 400
    status, _ = _call(base, "POST", "/api/runs", {"steps": [{"preset": "score"}]})
    assert status == 400
    status, _ = _call(base, "GET", "/api/runs/does-not-exist")
    assert status in (404, 500)
    status, _ = _call(base, "GET", "/api/nope")
    assert status == 404


def test_settings_refuse_an_empty_selection_instead_of_wiping_the_saved_one(server):
    """The studio autosaves while nothing is ticked; that must not clear a choice."""
    base, _ = server
    status, saved = _call(base, "PUT", "/api/settings",
                          {"mode": "specific", "providers": ["demo"], "routes": ["demo/fake"]})
    assert status == 200 and saved["revision"]

    status, error = _call(base, "PUT", "/api/settings",
                          {"mode": "specific", "providers": [], "routes": ["demo/fake"]})
    assert status == 400
    assert "no harness selected" in error["error"]

    status, current = _call(base, "GET", "/api/settings")
    assert status == 200
    assert current["settings"]["providers"] == ["demo"], "the saved selection must survive"


def test_settings_reject_unknown_keys(server):
    base, _ = server
    status, error = _call(base, "PUT", "/api/settings", {"harness": "opencode", "mode": "free"})
    assert status == 400
    assert "unknown setting" in error["error"]


def test_index_page_serves(server):
    import urllib.request as _urlopen

    base, _ = server
    with _urlopen.urlopen(base + "/", timeout=10) as response:
        assert response.status == 200
        assert "Harness Studio" in response.read().decode()


def test_scoring_view_is_derived_not_judged(server):
    base, _ = server
    status, payload = _call(base, "POST", "/api/tasks", {"name": "icp-scoring", "preset": "account-research"})
    assert status == 200
    view = payload["scoring"]
    assert view["scorable"] is True and view["derived"] is True
    assert view["total_points"] == 100 and view["score_cap"] == 100
    assert view["has_fit_tier"] is True and view["has_pass"] is False
    assert {item["item_id"] for item in view["checklist"]} == {
        "explicit_initiative", "stack_confirmed", "hiring_or_trigger", "firmographic_fit",
    }
    assert all(item["description"] for item in view["checklist"])
    assert [band["tier"] for band in view["tier_bands"]] == ["tier_1", "tier_2", "tier_3", "unfit"]
    assert view["tier_bands"][0] == {"tier": "tier_1", "min_score": 85, "max_score": 100}
    assert view["tier_bands"][-1] == {"tier": "unfit", "min_score": 0, "max_score": 49}
    assert "revision_id" in view

    status, listed = _call(base, "GET", "/api/tasks")
    assert status == 200
    assert "icp-scoring" in {task["task_name"] for task in listed["tasks"]}


def test_scoring_edit_creates_new_revision_and_preserves_old(server):
    from harness_fleet.store import HarnessStore

    base, workspace = server
    _call(base, "POST", "/api/tasks", {"name": "edit-me", "preset": "score"})
    status, before = _call(base, "GET", "/api/tasks/edit-me/scoring")
    assert status == 200
    old_revision = before["scoring"]["revision_id"]

    status, payload = _call(base, "PUT", "/api/tasks/edit-me/scoring", {
        "instructions": "Only explicit migrations count.",
        "checklist": [
            {"item_id": "initiative_named", "points": 60, "description": "Active initiative", "half_life_days": 30},
            {"item_id": "criteria_evidence", "points": 40, "description": "Criteria confirmed", "half_life_days": None},
        ],
        "source_weights": {"jobs.ashbyhq.com": 1.0, "example.com": 0.4},
        "default_source_weight": 0.5,
        "pass_score": 70,
        "min_quote_chars": 20,
    })
    assert status == 200
    view = payload["scoring"]
    assert view["revision_id"] != old_revision
    assert view["previous_revision_id"] == old_revision
    assert view["total_points"] == 100
    assert {item["item_id"]: item["points"] for item in view["checklist"]} == {
        "initiative_named": 60, "criteria_evidence": 40,
    }
    assert [item["half_life_days"] for item in view["checklist"]] == [30, None]
    assert view["source_weights"] == {"jobs.ashbyhq.com": 1.0, "example.com": 0.4}
    assert view["default_source_weight"] == 0.5 and view["pass_score"] == 70
    assert view["min_quote_chars"] == 20

    # The old revision is immutable and the pointer moved to the new one.
    store = HarnessStore(workspace / "studio.db")
    old = store.get_task_revision(old_revision)
    assert old.checklist == {"initiative_named": 40, "criteria_evidence": 35, "supporting_signals": 25}
    assert store.current_task_revision("edit-me") == view["revision_id"]
    current = store.get_task("edit-me")
    assert current.checklist == {"initiative_named": 60, "criteria_evidence": 40}
    inner = current.claims_schema["properties"]["checklist"]
    assert set(inner["properties"]) == {"initiative_named", "criteria_evidence"}
    assert inner["required"] == ["criteria_evidence", "initiative_named"]
    assert current.claims_schema["additionalProperties"] is False


def test_scoring_edit_is_fail_closed(server):
    base, _ = server
    _call(base, "POST", "/api/tasks", {"name": "guard", "preset": "score"})

    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {"score": 99})
    assert status == 400 and "derived" in payload["error"]
    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {"fit_tier": "tier_1"})
    assert status == 400
    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {"claims_schema": {"type": "object"}})
    assert status == 400
    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {"nonsense": 1})
    assert status == 400 and "unsupported" in payload["error"]
    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {"checklist": []})
    assert status == 400
    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {"half_lives": {"ghost": 10}})
    assert status == 400
    status, payload = _call(base, "PUT", "/api/tasks/guard/scoring", {
        "checklist": [{"item_id": "a", "points": 0}],
    })
    assert status == 400

    status, _ = _call(base, "GET", "/api/tasks/nope/scoring")
    assert status == 404

    # A task without a checklist has no scoring contract to edit.
    _call(base, "POST", "/api/tasks", {"name": "plain", "preset": "classify"})
    status, plain = _call(base, "GET", "/api/tasks/plain/scoring")
    assert status == 200 and plain["scoring"]["scorable"] is False
    status, payload = _call(base, "PUT", "/api/tasks/plain/scoring", {"instructions": "x"})
    assert status == 400 and "no scoring checklist" in payload["error"]

    status, payload = _call(base, "POST", "/api/tasks", {"name": "bad", "preset": "not-a-preset"})
    assert status == 400
    status, payload = _call(base, "POST", "/api/tasks", {"preset": "score"})
    assert status == 400


def test_create_task_refuses_to_clobber_a_tuned_task(server):
    base, workspace = server
    from harness_fleet.store import HarnessStore

    status, created = _call(base, "POST", "/api/tasks", {"name": "tuned", "preset": "score"})
    assert status == 200
    status, payload = _call(base, "PUT", "/api/tasks/tuned/scoring", {
        "checklist": [{"item_id": "only", "points": 100, "description": "Only signal"}],
    })
    assert status == 200
    tuned_revision = payload["scoring"]["revision_id"]

    status, payload = _call(base, "POST", "/api/tasks", {"name": "tuned", "preset": "score"})
    assert status == 409 and "already exists" in payload["error"]

    store = HarnessStore(workspace / "studio.db")
    assert store.current_task_revision("tuned") == tuned_revision
    assert store.get_task("tuned").checklist == {"only": 100}
    assert created["scoring"]["revision_id"] != tuned_revision


def test_checklist_half_lives_preserve_on_omit_and_clear_on_null(server):
    base, _ = server
    _call(base, "POST", "/api/tasks", {"name": "halves", "preset": "account-research"})

    # Explicit null clears this item's half-life; the others keep theirs.
    status, payload = _call(base, "PUT", "/api/tasks/halves/scoring", {
        "checklist": [
            {"item_id": "explicit_initiative", "points": 40, "half_life_days": None},
            {"item_id": "stack_confirmed", "points": 30},
            {"item_id": "hiring_or_trigger", "points": 20},
            {"item_id": "firmographic_fit", "points": 10},
        ],
    })
    assert status == 200
    halves = {item["item_id"]: item["half_life_days"] for item in payload["scoring"]["checklist"]}
    assert halves == {
        "explicit_initiative": None,
        "stack_confirmed": 180.0,
        "hiring_or_trigger": 21.0,
        "firmographic_fit": 365.0,
    }

    # And an omitted key on a later edit still preserves the stored value.
    status, payload = _call(base, "PUT", "/api/tasks/halves/scoring", {
        "checklist": [{"item_id": "stack_confirmed", "points": 100}],
    })
    assert status == 200
    items = payload["scoring"]["checklist"]
    assert [(item["item_id"], item["points"], item["half_life_days"]) for item in items] == [
        ("stack_confirmed", 100, 180.0),
    ]


def test_scoring_numeric_fields_reject_floats_and_out_of_range(server):
    base, _ = server
    _call(base, "POST", "/api/tasks", {"name": "numbers", "preset": "score"})
    for body in (
        {"checklist": [{"item_id": "a", "points": 3.5}]},
        {"checklist": [{"item_id": "a", "points": True}]},
        {"pass_score": 70.5},
        {"pass_score": 101},
        {"min_quote_chars": 0},
        {"batch_size": "many"},
        {"max_slice_chars": 10},
        {"candidate_top_n": 99},
    ):
        status, payload = _call(base, "PUT", "/api/tasks/numbers/scoring", body)
        assert status == 400, (body, status, payload)
        assert "must be" in payload["error"]


def test_studio_rejects_cross_origin_requests(server):
    base, _ = server
    status, _ = _call(base, "GET", "/api/tasks", headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _ = _call(base, "POST", "/api/tasks", {"name": "csrf", "preset": "score"},
                      headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _ = _call(base, "PUT", "/api/tasks/csrf/scoring", {"instructions": "x"},
                      headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _ = _call(base, "GET", "/api/tasks", headers={"Host": "evil.example"})
    assert status == 403
    # Same-origin browser requests and header-less clients still work.
    status, _ = _call(base, "GET", "/api/tasks", headers={"Origin": base})
    assert status == 200
    status, _ = _call(base, "GET", "/api/tasks")
    assert status == 200


def test_scoring_edit_honors_if_match(server):
    base, _ = server
    _call(base, "POST", "/api/tasks", {"name": "concurrent", "preset": "score"})
    current = _call(base, "GET", "/api/tasks/concurrent/scoring")[1]["scoring"]["revision_id"]

    # A stale tab (wrong revision) is refused with 412 and writes nothing.
    status, payload = _call(base, "PUT", "/api/tasks/concurrent/scoring",
                            {"instructions": "stale write"}, headers={"If-Match": "deadbeef"})
    assert status == 412 and "changed elsewhere" in payload["error"]
    assert _call(base, "GET", "/api/tasks/concurrent/scoring")[1]["scoring"]["revision_id"] == current

    # The matching revision saves and reports lineage.
    status, payload = _call(base, "PUT", "/api/tasks/concurrent/scoring",
                            {"instructions": "fresh write"}, headers={"If-Match": current})
    assert status == 200
    assert payload["scoring"]["previous_revision_id"] == current
    assert payload["scoring"]["revision_id"] != current

    # An absent precondition still works for CLI and curl clients.
    status, _ = _call(base, "PUT", "/api/tasks/concurrent/scoring", {"instructions": "cli write"})
    assert status == 200


def test_create_task_reports_readable_validation_errors(server):
    base, _ = server
    status, payload = _call(base, "POST", "/api/tasks", {"name": "not a valid name!", "preset": "score"})
    assert status == 400
    assert payload["error"].startswith("invalid task spec")
    assert "name" in payload["error"]


def test_scoring_rejects_boolean_numeric_values(server):
    base, _ = server
    _call(base, "POST", "/api/tasks", {"name": "bools", "preset": "score"})
    for body in (
        {"source_weights": {"example.com": True}},
        {"default_source_weight": True},
        {"checklist": [{"item_id": "a", "points": 50, "half_life_days": True}]},
    ):
        status, payload = _call(base, "PUT", "/api/tasks/bools/scoring", body)
        assert status == 400, (body, status, payload)
        assert "must be" in payload["error"]


def test_task_duplicate_branches_a_tuned_contract(server):
    base, _ = server
    _call(base, "POST", "/api/tasks", {"name": "src", "preset": "account-research"})
    status, _ = _call(base, "PUT", "/api/tasks/src/scoring", {
        "checklist": [{"item_id": "only", "points": 100, "description": "Only signal", "half_life_days": 5}],
        "pass_score": 60,
    })
    assert status == 200

    status, payload = _call(base, "POST", "/api/tasks/src/duplicate", {"name": "copy"})
    assert status == 200
    copy = payload["scoring"]
    assert copy["task"] == "copy"
    assert copy["checklist"] == [
        {"item_id": "only", "points": 100, "description": "Only signal", "half_life_days": 5.0},
    ]
    assert copy["pass_score"] == 60

    # The original is untouched, and a name collision is refused.
    assert _call(base, "GET", "/api/tasks/src/scoring")[1]["scoring"]["checklist"][0]["points"] == 100
    status, payload = _call(base, "POST", "/api/tasks/src/duplicate", {"name": "copy"})
    assert status == 409 and "already exists" in payload["error"]

    status, _ = _call(base, "POST", "/api/tasks/missing/duplicate", {"name": "x"})
    assert status == 404
    status, payload = _call(base, "POST", "/api/tasks/src/duplicate", {})
    assert status == 400


def test_meta_reports_workspace_and_schema(server):
    base, workspace = server
    status, payload = _call(base, "GET", "/api/meta")
    assert status == 200
    assert payload["workspace"] == str(workspace)
    assert payload["database"].endswith("studio.db")
    assert payload["schema_version"]


def test_run_step_rejects_bad_integers_as_client_errors(server):
    base, _ = server
    item = [{"item_id": "i1", "text": "We are migrating the legacy billing pipeline to Kafka now."}]
    for step in (
        {"preset": "score", "task": "ints-null", "items": item, "routes": ["demo/fake"], "max_per_batch": None},
        {"preset": "score", "task": "ints-neg", "items": item, "routes": ["demo/fake"], "concurrency": -1},
        {"preset": "score", "task": "ints-str", "items": item, "routes": ["demo/fake"], "max_attempts": "lots"},
    ):
        status, payload = _call(base, "POST", "/api/runs", {"steps": [step]})
        assert status == 400, (step, status, payload)


def test_studio_rejects_an_oversized_body(server):
    base, _ = server
    status, payload = _call(base, "POST", "/api/tasks",
                            {"name": "big", "preset": "score", "pad": "a" * 1_000_100})
    assert status == 400 and "too large" in payload["error"]


def test_routes_refresh_can_target_one_harness(server):
    base, _ = server
    # claude declares no models command, so the harness owns an empty list
    # without shelling out to a binary the test environment may not have.
    status, payload = _call(base, "POST", "/api/routes/refresh", {"provider": "claude"})
    assert status == 200
    assert payload["refresh"] == {"claude": 0}

    status, payload = _call(base, "POST", "/api/routes/refresh", {"provider": "not-a-harness"})
    assert status == 400 and "unknown harness" in payload["error"]


def test_settings_round_trip_persists_to_the_database(server):
    """The selection the studio saves is the row runs are audited against."""
    import sqlite3

    base, tmp_path = server
    status, payload = _call(base, "GET", "/api/settings")
    assert status == 200
    assert payload["settings"] is None

    status, saved = _call(base, "PUT", "/api/settings", {
        "mode": "specific",
        "providers": ["muse", "codex"],
        "routes": ["muse/x", "muse/a"],
    })
    assert status == 200
    revision = saved["revision"]
    assert revision
    assert saved["settings"]["providers"] == ["codex", "muse"]
    assert saved["settings"]["routes"] == ["muse/a", "muse/x"]

    status, reloaded = _call(base, "GET", "/api/settings")
    assert status == 200
    assert reloaded["settings"]["mode"] == "specific"
    assert reloaded["settings"]["providers"] == ["codex", "muse"]
    assert reloaded["settings"]["revision"] == revision

    connection = sqlite3.connect(tmp_path / "studio.db")
    try:
        rows = connection.execute(
            "SELECT mode, providers_json, routes_json, revision_id FROM studio_settings"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, "the singleton row must be updated, never duplicated"
    assert rows[0][0] == "specific"
    assert json.loads(rows[0][1]) == ["codex", "muse"]
    assert json.loads(rows[0][2]) == ["muse/a", "muse/x"]
    assert rows[0][3] == revision


def test_free_mode_ignores_specific_routes_and_an_empty_selection_is_refused(server):
    base, tmp_path = server
    status, saved = _call(base, "PUT", "/api/settings", {
        "mode": "free",
        "providers": ["codex"],
        "routes": ["muse/x"],
    })
    assert status == 200
    assert saved["settings"]["routes"] == [], "free mode must not pin specific routes"

    # Choosing no harness is a refusal, not a silent clear. The studio autosaves
    # while nothing is ticked, so "empty means clear" wiped real selections; the
    # explicit way to clear is `harness-fleet settings --clear`.
    status, error = _call(base, "PUT", "/api/settings", {
        "mode": "free", "providers": [], "routes": [],
    })
    assert status == 400
    assert "no harness selected" in error["error"]

    status, reloaded = _call(base, "GET", "/api/settings")
    assert reloaded["settings"]["providers"] == ["codex"], "the saved selection must survive"

    import sqlite3
    connection = sqlite3.connect(tmp_path / "studio.db")
    try:
        count = connection.execute("SELECT COUNT(*) FROM studio_settings").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_settings_clear_is_explicit(tmp_path):
    """The supported way to remove a saved selection."""
    from harness_fleet.store import HarnessStore

    db = tmp_path / "clear.db"
    store = HarnessStore(db)
    store.save_studio_selection({"mode": "free", "providers": ["codex"], "routes": []})
    assert store.get_studio_selection() is not None
    store.clear_studio_selection()
    assert store.get_studio_selection() is None


def test_settings_rejects_an_unknown_mode(server):
    base, _ = server
    status, payload = _call(base, "PUT", "/api/settings", {"mode": "everything"})
    assert status == 400
    assert "mode" in payload["error"]

import json
import subprocess
import sys

import pytest

from harness_fleet.mcp_server import Workspace, create_mcp_server


def test_workspace_rejects_escape(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(ValueError, match="escapes workspace"):
        workspace.path("../outside.json")


def test_mcp_honors_harness_fleet_database_environment(tmp_path, monkeypatch):
    configured = tmp_path / "configured.db"
    monkeypatch.setenv("HARNESS_FLEET_DB", str(configured))

    create_mcp_server(tmp_path)

    assert configured.exists()
    assert not (tmp_path / "harness-fleet.db").exists()


def test_mcp_tool_error_does_not_kill_server(tmp_path):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "harness_fleet_test",
                "arguments": {"task": "missing", "input_path": "missing.jsonl"},
            },
        },
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_fleet.cli", "serve", "--workspace-root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    responses = []
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" in message:
            responses.append(json.loads(process.stdout.readline()))
    process.stdin.close()
    exit_code = process.wait(timeout=10)

    assert exit_code == 0
    assert responses[1]["id"] == 2
    assert responses[1]["result"]["isError"] is True
    assert responses[2]["id"] == 3
    tools = responses[2]["result"]["tools"]
    # Assert the surface, not a count: tools get added, and a magic number
    # turns every addition into a false failure.
    names = {tool["name"] for tool in tools}
    assert {
        "harness_fleet_routes", "harness_fleet_tasks", "harness_fleet_export",
        "harness_fleet_sources", "harness_fleet_promote_source",
    } <= names
    tool_names = {tool["name"] for tool in tools}
    assert "harness_fleet_init" in tool_names
    assert "harness_fleet_save_profile" in tool_names
    assert "harness_fleet_get_profile" in tool_names
    assert "harness_fleet_status" in tool_names
    assert "harness_fleet_eval" in tool_names
    assert "harness_fleet_cooldowns" in tool_names
    assert "harness_fleet_history" in tool_names
    assert all(tool["description"] for tool in tools)
    assert all("inputSchema" in tool and "outputSchema" in tool for tool in tools)


def test_mcp_reports_this_package_version_not_the_sdk(tmp_path):
    """serverInfo.version must be the product's, not the MCP library's."""
    import harness_fleet

    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_fleet.cli", "serve", "--workspace-root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(messages[0]) + "\n")
    process.stdin.flush()
    server_info = json.loads(process.stdout.readline())["result"]["serverInfo"]
    process.stdin.close()
    process.wait(timeout=10)

    assert server_info["name"] == "harness-fleet"
    assert server_info["version"] == harness_fleet.__version__


def test_mcp_tasks_tool_returns_the_task_list(tmp_path):
    """The tasks tool must return what `tasks --json` prints, `scorable` included."""
    messages = [
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "1"}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "harness_fleet_tasks", "arguments": {}}},
    ]
    workspace = tmp_path / "ws"
    workspace.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_fleet.cli", "serve", "--workspace-root", str(workspace)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    responses = []
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" in message:
            responses.append(json.loads(process.stdout.readline()))
    process.stdin.close()
    process.wait(timeout=10)

    result = responses[1]["result"]
    assert result.get("isError") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["count"] == len(payload["tasks"])


def test_mcp_tools_execution(tmp_path):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "harness_fleet_doctor",
                "arguments": {},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "harness_fleet_schema",
                "arguments": {"kind": "database"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "harness_fleet_routes",
                "arguments": {"refresh": False, "observed_zero_only": False},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "harness_fleet_init",
                "arguments": {"task_name": "mcp-score-task", "preset": "score"},
            },
        },
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_fleet.cli", "serve", "--workspace-root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    responses = {}
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" in message:
            resp = json.loads(process.stdout.readline())
            responses[resp["id"]] = resp
    process.stdin.close()
    exit_code = process.wait(timeout=10)

    assert exit_code == 0
    # doctor tool call
    doctor_res = responses[2]["result"]
    assert "isError" not in doctor_res or not doctor_res["isError"]

    # schema tool call
    schema_res = responses[3]["result"]
    assert "isError" not in schema_res or not schema_res["isError"]

    # routes tool call
    routes_res = responses[4]["result"]
    assert "isError" not in routes_res or not routes_res["isError"]

    # init tool call
    init_res = responses[5]["result"]
    assert "isError" not in init_res or not init_res["isError"]
    assert "structuredContent" in init_res
    assert init_res["structuredContent"]["task"] == "mcp-score-task"


def test_mcp_validate_surfaces_slicing_caveats(tmp_path):
    """The typed MCP report must carry the same caveat the CLI prints."""
    import os

    input_path = tmp_path / "long.jsonl"
    input_path.write_text(
        json.dumps({"item_id": "long-1", "text": "Sentence about Kafka. " * 200}) + "\n",
        encoding="utf-8",
    )
    task_spec = {
        "name": "sliced",
        "instructions": "Classify each record.",
        "batch_size": 4,
        "max_slice_chars": 300,
        "claims_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": False,
        },
    }
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "harness_fleet_register_task", "arguments": {"task": task_spec}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "harness_fleet_validate",
                "arguments": {"task": "sliced", "input_path": "long.jsonl"},
            },
        },
    ]
    env = {**os.environ, "HARNESS_FLEET_DB": str(tmp_path / "mcp.db")}
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_fleet.cli", "serve", "--workspace-root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    responses = {}
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" in message:
            resp = json.loads(process.stdout.readline())
            responses[resp["id"]] = resp
    process.stdin.close()
    assert process.wait(timeout=10) == 0

    register_result = responses[2]["result"]
    assert not register_result.get("isError"), register_result
    validate_result = responses[3]["result"]
    assert not validate_result.get("isError"), validate_result
    report = validate_result["structuredContent"]
    assert report["valid"] is True
    assert report["input_items"] == 1
    assert report["errors"], "the slicing caveat must reach the MCP report"
    assert "max_slice_chars=300" in report["errors"][0]


def test_mcp_sources_tools_drive_the_taxonomy(tmp_path):
    """The taxonomy is drivable from an assistant, not only from the CLI."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "sources").mkdir()
    (workspace / "sources" / "industry.json").write_text(json.dumps({
        "name": "industry", "category": "b2b_directory_audit",
        "list_url": "https://directory.example/?q={query}",
    }), encoding="utf-8")

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "harness_fleet_sources", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "harness_fleet_promote_source",
                    "arguments": {"domain": "vendorhub.example", "category": "vendor_registry",
                                  "reason": "publishes partner stories"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "harness_fleet_promote_source",
                    "arguments": {"domain": "x.example", "category": "secret_sauce"}}},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_fleet.cli", "serve", "--workspace-root", str(workspace)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    responses = []
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" in message:
            responses.append(json.loads(process.stdout.readline()))
    process.stdin.close()
    process.wait(timeout=10)

    listed = responses[1]["result"]
    assert listed.get("isError") is not True, listed
    payload = json.loads(listed["content"][0]["text"])
    assert payload["channels"][0]["name"] == "industry"

    promoted = json.loads(responses[2]["result"]["content"][0]["text"])
    assert promoted["domains"][0]["domain"] == "vendorhub.example"
    assert promoted["domains"][0]["reason"] == "publishes partner stories"

    refused = responses[3]["result"]
    assert refused.get("isError") is True, "an unknown category must be refused"
    assert "unknown category" in json.dumps(refused)


def test_the_mcp_history_uses_the_same_reader_as_the_cli(tmp_path):
    """Two surfaces, one trajectory: the engine's rows and the ledger's, both."""
    from harness_fleet.ledger import Ledger
    from harness_fleet.store import HarnessStore

    db = tmp_path / "state.db"
    store = HarnessStore(db)
    # A score only the engine recorded, and one only a node recorded.
    store.record_score_history("direct-run", "acme.example", "acme.example", 55.0)
    Ledger(store).record(
        [{"item_id": "acme.example", "candidate": "acme.example", "outcome": "",
          "gates": [], "score": 91.0, "tier": "tier_2"}],
        dag_id="run-2", node_id="s-score", at="2020-01-01T00:00:00+00:00",
    )

    # The reader both surfaces call.
    scores = [row["score"] for row in Ledger(store).scores("acme.example")]
    assert scores == [91.0, 55.0], "the trajectory is both records, oldest first"
    # And the raw store query still sees only the engine's half, which is why
    # nothing but the engine's own bookkeeping should read it.
    assert [row["score"] for row in store.get_entity_history("acme.example")] == [55.0]

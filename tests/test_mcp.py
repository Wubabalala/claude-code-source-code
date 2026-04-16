"""Tests for agent.mcp — Phase 6 MCP stdio JSON-RPC client.

Most tests mock the subprocess to avoid requiring a real MCP server.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.mcp import MCPClient, MCPClientError, MCPInput, MCPServerConfig, MCPTool
from agent.tools import PermissionDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server_cfg(name="test") -> MCPServerConfig:
    return MCPServerConfig(name=name, command="echo", args=[], env={})


def _mock_process(responses: list[dict]):
    """Return a mock Popen whose stdout.readline returns JSON lines."""
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    lines = [json.dumps(r) + "\n" for r in responses]
    proc.stdout.readline = MagicMock(side_effect=lines)
    proc.stderr = MagicMock()
    return proc


def _tool_desc(name="read", description="Read a file", schema=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": schema or {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


def test_mcp_client_initialize_handshake():
    init_resp = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
    proc = _mock_process([init_resp])
    with patch("agent.mcp.subprocess.Popen", return_value=proc):
        client = MCPClient(_server_cfg())
        client.connect()
    assert client.available is True
    # Verify initialize was sent
    sent = proc.stdin.write.call_args_list[0][0][0]
    msg = json.loads(sent)
    assert msg["method"] == "initialize"


def test_mcp_client_list_tools():
    init_resp = {"jsonrpc": "2.0", "id": 1, "result": {}}
    list_resp = {"jsonrpc": "2.0", "id": 2, "result": {
        "tools": [_tool_desc("read"), _tool_desc("write")]
    }}
    proc = _mock_process([init_resp, list_resp])
    with patch("agent.mcp.subprocess.Popen", return_value=proc):
        client = MCPClient(_server_cfg())
        client.connect()
        tools = client.list_tools()
    assert len(tools) == 2
    assert tools[0]["name"] == "read"


def test_mcp_client_call_tool():
    init_resp = {"jsonrpc": "2.0", "id": 1, "result": {}}
    call_resp = {"jsonrpc": "2.0", "id": 2, "result": {
        "content": [{"type": "text", "text": "file content here"}]
    }}
    proc = _mock_process([init_resp, call_resp])
    with patch("agent.mcp.subprocess.Popen", return_value=proc):
        client = MCPClient(_server_cfg())
        client.connect()
        result = client.call_tool("read", {"path": "/tmp/x"})
    assert result["content"][0]["text"] == "file content here"


def test_mcp_client_server_crash_does_not_crash_agent():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin.write.side_effect = BrokenPipeError("server died")
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    with patch("agent.mcp.subprocess.Popen", return_value=proc):
        client = MCPClient(_server_cfg())
        client._process = proc
        client._available = True
        result = client.call_tool("anything", {})
    assert result["isError"] is True
    assert client.available is False


def test_mcp_client_shutdown_sends_close():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout.readline.return_value = ""  # no response to shutdown notification

    client = MCPClient(_server_cfg())
    client._process = proc
    client._available = True
    client.shutdown()
    # shutdown method was sent + terminate called
    assert proc.terminate.called
    assert client.available is False


def test_mcp_client_timeout_on_call():
    """Server that hangs on tools/call must not block the agent forever."""
    import time

    # Mock a process whose stdout.readline blocks forever (simulated by
    # a thread that sleeps instead of reading)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()

    def slow_readline():
        time.sleep(60)  # simulate hung server
        return ""

    proc.stdout.readline = slow_readline
    proc.stderr = MagicMock()

    client = MCPClient(_server_cfg(), call_timeout=0.5)
    client._process = proc
    client._available = True

    t0 = time.monotonic()
    result = client.call_tool("anything", {})
    elapsed = time.monotonic() - t0

    assert result["isError"] is True
    assert elapsed < 3, f"call_tool blocked for {elapsed:.1f}s (expected <3)"
    assert client.available is False


def test_mcp_client_connect_failure_marks_unavailable(capsys):
    with patch("agent.mcp.subprocess.Popen", side_effect=OSError("no such file")):
        client = MCPClient(_server_cfg())
        client.connect()
    assert client.available is False
    assert "connect failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# MCPTool
# ---------------------------------------------------------------------------


def test_mcp_tool_registers_as_tool_subclass():
    client = MagicMock()
    tool = MCPTool("srv", _tool_desc("read"), client)
    assert isinstance(tool, MCPTool)
    assert tool.name == "mcp_srv_read"
    assert tool.reads_from_filesystem is True
    assert tool.writes_to_filesystem is True
    assert tool.destroys_data is True


def test_mcp_tool_check_permissions_defaults_to_ask():
    client = MagicMock()
    tool = MCPTool("srv", _tool_desc("read"), client)
    outcome = tool.check_permissions(MCPInput())
    assert outcome.decision == PermissionDecision.ASK
    assert "mcp" in outcome.op_type


def test_mcp_tool_to_anthropic_schema_uses_original():
    """Model sees the MCP server's precise JSON schema, not the generic
    MCPInput schema."""
    client = MagicMock()
    schema = {"type": "object", "properties": {"path": {"type": "string"}},
              "required": ["path"]}
    tool = MCPTool("srv", _tool_desc("read", schema=schema), client)
    api_schema = tool.to_anthropic_schema()
    assert api_schema["input_schema"] == schema
    assert api_schema["name"] == "mcp_srv_read"


def test_mcp_tool_execute_forwards_to_server():
    client = MagicMock()
    client.call_tool.return_value = {
        "content": [{"type": "text", "text": "result"}],
        "isError": False,
    }
    tool = MCPTool("srv", _tool_desc("read"), client)
    result = tool.execute(MCPInput(path="/tmp/x"))
    assert result.output == "result"
    assert result.is_error is False
    client.call_tool.assert_called_once()


def test_mcp_tool_execute_handles_error():
    client = MagicMock()
    client.call_tool.return_value = {
        "content": [{"type": "text", "text": "not found"}],
        "isError": True,
    }
    tool = MCPTool("srv", _tool_desc("read"), client)
    result = tool.execute(MCPInput(path="/nope"))
    assert result.is_error is True


def test_mcp_tool_metadata_all_true():
    """All MCP tools are conservatively marked as potentially dangerous."""
    client = MagicMock()
    tool = MCPTool("srv", _tool_desc(), client)
    assert tool.reads_from_filesystem is True
    assert tool.writes_to_filesystem is True
    assert tool.destroys_data is True


def test_mcp_tool_name_prefix():
    """Tool name follows mcp_{server}_{tool} convention for stable sort."""
    client = MagicMock()
    tool = MCPTool("myserver", _tool_desc("mytool"), client)
    assert tool.name == "mcp_myserver_mytool"


def test_mcp_config_empty_servers_is_noop():
    """No configured servers → no tools, no clients."""
    from agent.mcp import discover_mcp_tools
    tools, clients = discover_mcp_tools([])
    assert tools == []
    assert clients == []


def test_mcp_input_accepts_any_kwargs():
    """MCPInput(extra='allow') accepts arbitrary parameters."""
    inp = MCPInput(path="/tmp", recursive=True, depth=3)
    assert inp.model_extra == {"path": "/tmp", "recursive": True, "depth": 3}

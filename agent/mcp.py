"""MCP client — Phase 6 stdio JSON-RPC 2.0 tool provider.

Connects to MCP servers via subprocess stdin/stdout, discovers their tools
via the standard MCP protocol (initialize → tools/list), and wraps each
as a Tool subclass (MCPTool) that integrates into the existing agent loop.

Key design decisions:
  - input_model: generic MCPInput(extra="allow") for loop-side acceptance;
    to_anthropic_schema() returns the MCP server's original JSON schema
    so the model sees precise parameter info.
  - All MCPTool default to ASK permission (untrusted external tools).
  - Server crash → mark tools as unavailable, don't crash the agent.
  - Lifecycle: connect at startup, shutdown via _cleanup() at exit.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from agent.tools import PermissionDecision, PermissionOutcome, Tool, ToolResult


class MCPInput(BaseModel):
    """Generic input model for MCP tools. Accepts any kwargs from the model.
    Actual validation is deferred to the MCP server."""
    model_config = ConfigDict(extra="allow")


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]


class MCPClientError(Exception):
    pass


class MCPClient:
    """Manages a single MCP server subprocess."""

    def __init__(self, config: MCPServerConfig, *, call_timeout: float = 30.0):
        self.config = config
        self.call_timeout = call_timeout
        self._process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def connect(self) -> None:
        """Spawn the server process and perform the initialize handshake."""
        try:
            env = dict(self.config.env) if self.config.env else None
            self._process = subprocess.Popen(
                [self.config.command] + self.config.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            resp = self._send("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent", "version": "0.1.0"},
            })
            if "error" in resp:
                raise MCPClientError(f"initialize failed: {resp['error']}")
            self._available = True
        except (OSError, MCPClientError) as e:
            print(f"[mcp] warn: {self.config.name} connect failed: {e}",
                  file=sys.stderr)
            self._available = False

    def list_tools(self) -> list[dict]:
        """Call tools/list and return the tool descriptors."""
        if not self._available:
            return []
        try:
            resp = self._send("tools/list", {})
            return resp.get("result", {}).get("tools", [])
        except MCPClientError:
            self._available = False
            return []

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call tools/call and return the result content."""
        if not self._available:
            return {"isError": True, "content": [{"type": "text", "text": "MCP server unavailable"}]}
        try:
            resp = self._send("tools/call", {"name": name, "arguments": arguments})
            if "error" in resp:
                return {"isError": True, "content": [{"type": "text", "text": str(resp["error"])}]}
            return resp.get("result", {})
        except MCPClientError as e:
            self._available = False
            return {"isError": True, "content": [{"type": "text", "text": f"MCP error: {e}"}]}

    def shutdown(self) -> None:
        """Send shutdown notification and terminate the process."""
        if self._process is None:
            return
        try:
            if self._process.poll() is None:
                self._send_raw({"jsonrpc": "2.0", "method": "shutdown"})
                self._process.terminate()
                self._process.wait(timeout=3)
        except Exception:
            if self._process.poll() is None:
                self._process.kill()
        finally:
            self._available = False

    def _send(self, method: str, params: dict) -> dict:
        with self._lock:
            self._request_id += 1
            msg = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
            return self._send_raw(msg)

    def _send_raw(self, msg: dict) -> dict:
        if self._process is None or self._process.poll() is not None:
            raise MCPClientError("process not running")
        try:
            line = json.dumps(msg) + "\n"
            self._process.stdin.write(line)
            self._process.stdin.flush()

            # For notifications (no id), don't wait for response
            if "id" not in msg:
                return {}

            resp_line = self._process.stdout.readline()
            if not resp_line:
                raise MCPClientError("empty response (server may have crashed)")
            return json.loads(resp_line)
        except (json.JSONDecodeError, BrokenPipeError, OSError) as e:
            self._available = False
            raise MCPClientError(str(e))


class MCPTool(Tool):
    """Wraps a single MCP tool as an agent Tool subclass."""

    reads_from_filesystem = True
    writes_to_filesystem = True
    destroys_data = True

    def __init__(self, server_name: str, tool_desc: dict, client: MCPClient):
        self._server_name = server_name
        self._tool_desc = tool_desc
        self._client = client
        self.name = f"mcp_{server_name}_{tool_desc['name']}"
        self._mcp_tool_name = tool_desc["name"]

    def description(self) -> str:
        return self._tool_desc.get("description", f"MCP tool: {self._mcp_tool_name}")

    @property
    def input_model(self):
        return MCPInput

    def to_anthropic_schema(self) -> dict:
        """Return the MCP server's original JSON schema — NOT the generic
        MCPInput schema. Model sees precise parameter names and types."""
        return {
            "name": self.name,
            "description": self.description(),
            "input_schema": self._tool_desc.get("inputSchema", {
                "type": "object", "properties": {},
            }),
        }

    def check_permissions(self, input: MCPInput) -> PermissionOutcome:
        """All MCP tools default to ASK — external tools are untrusted."""
        return PermissionOutcome(
            decision=PermissionDecision.ASK,
            tool_name=self.name,
            target=self._mcp_tool_name,
            op_type="mcp",
            risk=f"external MCP tool via {self._server_name}",
        )

    def execute(self, input: MCPInput) -> ToolResult:
        """Forward the call to the MCP server via tools/call."""
        args = {k: v for k, v in input.__dict__.items()
                if k not in ("model_config",) and not k.startswith("_")}
        # Pydantic v2 stores extras in model_extra
        if hasattr(input, "model_extra") and input.model_extra:
            args = dict(input.model_extra)

        result = self._client.call_tool(self._mcp_tool_name, args)

        content_parts = result.get("content", [])
        text_parts = [c.get("text", "") for c in content_parts if c.get("type") == "text"]
        output = "\n".join(text_parts) or "(no output)"
        is_error = result.get("isError", False)

        return ToolResult(output=output, is_error=bool(is_error))


def discover_mcp_tools(
    servers: list[MCPServerConfig],
    *,
    audit_logger: Any = None,
    session_id: Optional[str] = None,
) -> tuple[list[MCPTool], list[MCPClient]]:
    """Connect to all configured MCP servers, discover tools, return
    (tools, clients). Clients must be shut down at exit."""
    all_tools: list[MCPTool] = []
    all_clients: list[MCPClient] = []

    for srv in servers:
        client = MCPClient(srv)
        client.connect()
        all_clients.append(client)

        if not client.available:
            continue

        tool_descs = client.list_tools()
        for td in tool_descs:
            mcp_tool = MCPTool(srv.name, td, client)
            all_tools.append(mcp_tool)
            if audit_logger is not None:
                from agent.audit import emit
                emit(audit_logger, "mcp.tool.register",
                     session_id=session_id, tool=mcp_tool.name)

    return all_tools, all_clients

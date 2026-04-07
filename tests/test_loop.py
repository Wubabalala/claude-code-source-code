"""Tests for agent.loop.

Uses a FakeAnthropicClient (real class, not unittest.mock) for clarity.
"""
from collections import deque
from agent.loop import run_agent_loop
from agent.types import AgentResult
from agent.tools import Tool, ToolResult
from pydantic import BaseModel


# ============================================================================
# Test doubles
# ============================================================================


class _FakeBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, queue):
        self._queue = queue
        self.calls = []  # records every (model, messages) tuple for assertions

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        if not self._queue:
            raise RuntimeError("FakeAnthropicClient ran out of canned responses")
        return self._queue.popleft()


class FakeAnthropicClient:
    """A queueable fake Anthropic client for loop tests."""

    def __init__(self, responses):
        self.messages = _FakeMessages(deque(responses))


# Test tool — always echoes its input
class _EchoInput(BaseModel):
    text: str


class _EchoTool(Tool):
    name = "echo"

    def description(self) -> str:
        return "Echo text back."

    @property
    def input_model(self):
        return _EchoInput

    def execute(self, input: _EchoInput) -> ToolResult:
        return ToolResult(output=f"echoed: {input.text}")


def _user_msg(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _stub_system_prompt():
    return [{"type": "text", "text": "You are a test agent.", "cache_control": {"type": "ephemeral"}}]


# ============================================================================
# Happy path
# ============================================================================


def test_loop_completes_when_no_tool_use():
    """If the model returns text only, loop exits with status=completed."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(content=[_FakeBlock(type="text", text="hello")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert isinstance(result, AgentResult)
    assert result.status == "completed"
    # Original user message + new assistant message
    assert len(result.messages) == 2
    assert result.messages[1]["role"] == "assistant"


def test_loop_executes_tool_then_completes():
    """Turn 1: model uses tool. Turn 2: model returns text. Loop exits."""
    client = FakeAnthropicClient(responses=[
        # Turn 1: tool_use
        _FakeResponse(
            content=[
                _FakeBlock(type="text", text="I'll echo it."),
                _FakeBlock(type="tool_use", id="t1", name="echo", input={"text": "hi"}),
            ],
            stop_reason="tool_use",
        ),
        # Turn 2: final text
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("echo hi"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # user → assistant(tool_use) → user(tool_result) → assistant(text)
    assert len(result.messages) == 4
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[2]["role"] == "user"
    # Tool result is in the second user message
    tool_result_block = result.messages[2]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert "echoed: hi" in tool_result_block["content"]


def test_loop_exits_on_max_turns():
    """If model keeps calling tools forever, loop exits at max_turns."""
    # Generate 10 responses, all with tool_use
    responses = [
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id=f"t{i}", name="echo", input={"text": str(i)})],
            stop_reason="tool_use",
        )
        for i in range(10)
    ]
    client = FakeAnthropicClient(responses=responses)
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("loop"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=3,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "max_turns"


def test_loop_uses_primary_model_first():
    """No errors → only the primary model is used."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert client.messages.calls[0]["model"] == "primary"

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
    reads_from_filesystem = False
    writes_to_filesystem = False
    destroys_data = False

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


# ============================================================================
# Continue site 1: model fallback (withheld errors)
# ============================================================================


class _RecoverableError(Exception):
    status_code = 503


class _UnrecoverableError(Exception):
    status_code = 401


class _FakeMessagesWithErrors:
    """Fake messages.create that raises on first call(s) then returns canned response."""

    def __init__(self, errors_then_responses):
        self._sequence = list(errors_then_responses)
        self.calls = []

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({"model": model})
        item = self._sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClientWithErrors:
    def __init__(self, sequence):
        self.messages = _FakeMessagesWithErrors(sequence)


def test_loop_falls_back_to_secondary_model_on_recoverable_error():
    from agent.config import RetryConfig
    client = _FakeClientWithErrors([
        _RecoverableError(),  # primary model fails
        _FakeResponse(content=[_FakeBlock(type="text", text="ok via fallback")]),  # fallback succeeds
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
        retry_config=RetryConfig(budget=1),  # Phase 5: disable retry so fallback fires on first error
    )
    assert result.status == "completed"
    # First call used primary, second used fallback
    assert client.messages.calls[0]["model"] == "primary"
    assert client.messages.calls[1]["model"] == "fallback"


def test_loop_does_not_fall_back_on_unrecoverable_error():
    client = _FakeClientWithErrors([
        _UnrecoverableError(),
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
    assert result.status == "model_error"
    # Only one call attempted (primary), no fallback
    assert len(client.messages.calls) == 1


def test_loop_returns_model_error_when_fallback_also_fails():
    client = _FakeClientWithErrors([
        _RecoverableError(),
        _RecoverableError(),  # fallback also fails
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
    assert result.status == "model_error"


# ============================================================================
# Continue site 2: output token recovery
# ============================================================================


def test_loop_recovers_from_max_tokens_truncation():
    """If response stops with max_tokens AND no tool_use, request continuation."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="text", text="part 1...")],
            stop_reason="max_tokens",
        ),
        _FakeResponse(
            content=[_FakeBlock(type="text", text="...part 2 final")],
            stop_reason="end_turn",
        ),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("write a long response"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=10,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # Both partial responses preserved
    assert len(result.messages) == 3  # user + assistant(part 1) + assistant(part 2)


def test_loop_output_recovery_capped_at_3_retries():
    """Output recovery should not loop forever."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(content=[_FakeBlock(type="text", text=f"part {i}")], stop_reason="max_tokens")
        for i in range(10)
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=20,
        primary_model="primary",
        fallback_model="fallback",
    )
    # After 3 retries, loop returns completed (gives up on recovery)
    assert result.status == "completed"
    # 1 initial call + 3 retries = 4 API calls max
    assert len(client.messages.calls) <= 4


# ============================================================================
# Tool execution with permission checks
# ============================================================================
from agent.tools import PermissionDecision


class _AlwaysDenyTool(Tool):
    name = "denied_tool"
    reads_from_filesystem = False
    writes_to_filesystem = False
    destroys_data = False

    def description(self) -> str:
        return "Always denied."

    @property
    def input_model(self):
        return _EchoInput

    def check_permissions(self, input):
        from agent.tools import PermissionOutcome
        return PermissionOutcome(
            decision=PermissionDecision.DENY,
            tool_name=self.name,
            risk="always denied for test",
        )

    def execute(self, input):
        raise AssertionError("Should never execute when denied")


class _AlwaysRaiseTool(Tool):
    name = "raise_tool"
    reads_from_filesystem = False
    writes_to_filesystem = False
    destroys_data = False

    def description(self) -> str:
        return "Always raises."

    @property
    def input_model(self):
        return _EchoInput

    def execute(self, input):
        raise RuntimeError("kaboom")


def test_loop_denies_tool_returns_error_to_model():
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="denied_tool", input={"text": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="ok i won't")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AlwaysDenyTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # The tool_result message should contain the denial
    tool_result = result.messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result.get("is_error") is True
    assert "denied" in tool_result["content"].lower() or "permission" in tool_result["content"].lower()


def test_loop_tool_exception_returns_error_to_model_not_raised():
    """A raising tool must NOT crash the loop. Error goes back as tool_result."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="raise_tool", input={"text": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="that failed")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AlwaysRaiseTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    tool_result = result.messages[2]["content"][0]
    assert tool_result.get("is_error") is True
    assert "kaboom" in tool_result["content"]


def test_loop_invalid_tool_input_returns_validation_error():
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="echo", input={"WRONG_FIELD": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    tool_result = result.messages[2]["content"][0]
    assert tool_result.get("is_error") is True


def test_loop_unknown_tool_name_returns_error():
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="ghost_tool", input={})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="oh")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    tool_result = result.messages[2]["content"][0]
    assert tool_result.get("is_error") is True
    assert "ghost_tool" in tool_result["content"] or "unknown" in tool_result["content"].lower()

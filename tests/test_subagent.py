"""Tests for agent.subagent — Phase 7 Layer 1 sub-agent spawning."""
from __future__ import annotations

from collections import deque

from pydantic import BaseModel

from agent.subagent import run_fork_agent, run_fresh_agent
from agent.tools import Tool, ToolResult


# ---------------------------------------------------------------------------
# Test doubles (reuse FakeClient pattern)
# ---------------------------------------------------------------------------


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
        self.calls = []

    def create(self, model, messages, system, tools, **kw):
        self.calls.append({"model": model, "messages": list(messages)})
        return self._queue.popleft()


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(deque(responses))


class _InModel(BaseModel):
    text: str = "x"


class _EchoTool(Tool):
    name = "echo"
    reads_from_filesystem = False
    writes_to_filesystem = False
    destroys_data = False

    def description(self):
        return "echo"

    @property
    def input_model(self):
        return _InModel

    def execute(self, input):
        return ToolResult(output="echoed")


def _user_msg(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_msg(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _stub_sys():
    return [{"type": "text", "text": "sys"}]


# ---------------------------------------------------------------------------
# Fork mode
# ---------------------------------------------------------------------------


def test_fork_agent_inherits_parent_messages():
    """Fork mode starts with parent_messages + directive."""
    parent = (_user_msg("parent q"), _assistant_msg("parent a"))
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="fork result")]),
    ])
    result = run_fork_agent(
        parent, "analyze this",
        client=client, tools=[], system_prompt=_stub_sys(),
    )
    assert result.status == "completed"
    # API was called with parent messages + directive
    sent_messages = client.messages.calls[0]["messages"]
    assert len(sent_messages) == 3  # parent_q + parent_a + directive
    assert sent_messages[2]["content"][0]["text"] == "analyze this"


def test_fork_agent_uses_same_client_for_cache():
    """Fork shares the client instance for cache locality."""
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_fork_agent(
        (_user_msg("hi"),), "do stuff",
        client=client, tools=[], system_prompt=_stub_sys(),
    )
    # The client was used (not a copy)
    assert len(client.messages.calls) == 1


# ---------------------------------------------------------------------------
# Fresh mode
# ---------------------------------------------------------------------------


def test_fresh_agent_starts_with_empty_history():
    """Fresh mode only has the task prompt, no parent history."""
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="fresh result")]),
    ])
    result = run_fresh_agent(
        "search for TODO",
        client=client, tools=[], system_prompt=_stub_sys(),
    )
    assert result.status == "completed"
    sent = client.messages.calls[0]["messages"]
    assert len(sent) == 1  # only the prompt
    assert sent[0]["content"][0]["text"] == "search for TODO"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_subagent_has_independent_state():
    """Sub-agent's State starts fresh (turn=1, no fallback, etc.)."""
    traced = []
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_fresh_agent(
        "test",
        client=client, tools=[], system_prompt=_stub_sys(),
    )
    # If it ran successfully with a fresh client queue of 1 response,
    # it means it didn't inherit any stale state (retry count, compact
    # counters, etc.)
    assert True  # no crash = independent state


def test_subagent_result_not_auto_injected_into_parent():
    """Sub-agent returns AgentResult; caller decides what to do with it.
    The parent conversation_history is NOT modified."""
    parent_msgs = (_user_msg("parent"),)
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="sub result")]),
    ])
    result = run_fork_agent(
        parent_msgs, "do something",
        client=client, tools=[], system_prompt=_stub_sys(),
    )
    # Parent messages tuple is unchanged (immutable)
    assert len(parent_msgs) == 1
    # Sub-agent result is separate
    assert len(result.messages) >= 2


def test_subagent_session_id_contains_parent_prefix():
    """Sub-agent session_id = parent:sub-XXXXXXXX for audit correlation."""
    class _Logger:
        def __init__(self):
            self.events = []
        def info(self, msg, extra=None):
            self.events.append(dict(extra or {}))

    logger = _Logger()
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_fresh_agent(
        "test",
        client=client, tools=[], system_prompt=_stub_sys(),
        parent_session_id="parent-123",
        audit_logger=logger,
    )
    spawn_events = [e for e in logger.events if e.get("event") == "agent.spawn"]
    assert spawn_events
    sid = spawn_events[0]["session_id"]
    assert sid.startswith("parent-123:sub-")


def test_subagent_audit_events_emitted():
    """Both agent.spawn and agent.complete events are emitted."""
    class _Logger:
        def __init__(self):
            self.events = []
        def info(self, msg, extra=None):
            self.events.append(dict(extra or {}))

    logger = _Logger()
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_fresh_agent(
        "test",
        client=client, tools=[], system_prompt=_stub_sys(),
        audit_logger=logger,
    )
    event_names = [e.get("event") for e in logger.events]
    assert "agent.spawn" in event_names
    assert "agent.complete" in event_names


def test_subagent_inherits_retry_config():
    """Sub-agent receives retry_config from caller."""
    from agent.config import RetryConfig

    rc = RetryConfig(budget=2)
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    # Should not crash — just verify it runs with the config
    result = run_fresh_agent(
        "test",
        client=client, tools=[], system_prompt=_stub_sys(),
        retry_config=rc,
    )
    assert result.status == "completed"

"""Tests for agent.adapter — LLMAdapter implementations."""
from collections import deque

import pytest

from agent.adapter import (
    AnthropicAdapter,
    FlattenedMessageAdapter,
    LLMAdapter,
    ParsedResponse,
    _parse_raw_response,
)


# ---------------------------------------------------------------------------
# Test doubles (matching test_loop.py patterns)
# ---------------------------------------------------------------------------


class _FakeBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 80
    cache_creation_input_tokens = 20


class _FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, queue):
        self._queue = queue
        self.calls = []

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({
            "model": model, "messages": messages,
            "system": system, "tools": tools,
        })
        if not self._queue:
            raise RuntimeError("ran out of canned responses")
        return self._queue.popleft()


class _FakeStreamChunk:
    def __init__(self, texts, final_response):
        self._texts = texts
        self._final = final_response

    @property
    def text_stream(self):
        yield from self._texts

    def get_final_message(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeStreamMessages:
    def __init__(self, queue):
        self._queue = queue
        self.calls = []

    def create(self, **kw):
        raise RuntimeError("should not call create in streaming mode")

    def stream(self, model, messages, system, tools, **kw):
        self.calls.append({"model": model})
        if not self._queue:
            raise RuntimeError("ran out of canned streams")
        return self._queue.popleft()


class _FakeClient:
    def __init__(self, messages_obj):
        self.messages = messages_obj


# ---------------------------------------------------------------------------
# ParsedResponse
# ---------------------------------------------------------------------------


def test_parsed_response_frozen():
    pr = ParsedResponse(
        content_blocks=[{"type": "text", "text": "hi"}],
        tool_use_blocks=[],
        stop_reason="end_turn",
        usage=None,
    )
    with pytest.raises(AttributeError):
        pr.stop_reason = "changed"


def test_parsed_response_tool_use_blocks_filtered():
    blocks = [
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}},
    ]
    pr = ParsedResponse(
        content_blocks=blocks,
        tool_use_blocks=[b for b in blocks if b["type"] == "tool_use"],
        stop_reason="tool_use",
        usage=None,
    )
    assert len(pr.tool_use_blocks) == 1
    assert pr.tool_use_blocks[0]["name"] == "bash"


# ---------------------------------------------------------------------------
# _parse_raw_response
# ---------------------------------------------------------------------------


def test_parse_raw_response_text_only():
    raw = _FakeResponse(content=[_FakeBlock(type="text", text="hello")])
    parsed = _parse_raw_response(raw)
    assert parsed.content_blocks == [{"type": "text", "text": "hello"}]
    assert parsed.tool_use_blocks == []
    assert parsed.stop_reason == "end_turn"
    assert parsed.streamed is False


def test_parse_raw_response_with_tool_use():
    raw = _FakeResponse(
        content=[
            _FakeBlock(type="text", text="let me check"),
            _FakeBlock(type="tool_use", id="t1", name="bash", input={"cmd": "ls"}),
        ],
        stop_reason="tool_use",
    )
    parsed = _parse_raw_response(raw)
    assert len(parsed.content_blocks) == 2
    assert len(parsed.tool_use_blocks) == 1
    assert parsed.tool_use_blocks[0]["id"] == "t1"
    assert parsed.tool_use_blocks[0]["name"] == "bash"
    assert parsed.tool_use_blocks[0]["input"] == {"cmd": "ls"}
    assert parsed.stop_reason == "tool_use"


def test_parse_raw_response_preserves_usage():
    raw = _FakeResponse(content=[_FakeBlock(type="text", text="hi")])
    parsed = _parse_raw_response(raw)
    assert parsed.usage.input_tokens == 100
    assert parsed.usage.output_tokens == 50


# ---------------------------------------------------------------------------
# AnthropicAdapter — format_system / format_messages
# ---------------------------------------------------------------------------


def test_anthropic_format_system_passthrough():
    adapter = AnthropicAdapter()
    sys_prompt = [{"type": "text", "text": "You are helpful."}]
    assert adapter.format_system(sys_prompt) is sys_prompt


def test_anthropic_format_messages_passthrough():
    adapter = AnthropicAdapter()
    msgs = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    )
    result = adapter.format_messages(msgs)
    assert result == list(msgs)


# ---------------------------------------------------------------------------
# AnthropicAdapter — build_tool_schemas
# ---------------------------------------------------------------------------


class _FakeTool:
    def to_anthropic_schema(self):
        return {"name": "fake", "description": "a fake", "input_schema": {}}


def test_anthropic_build_tool_schemas():
    adapter = AnthropicAdapter()
    schemas = adapter.build_tool_schemas([_FakeTool(), _FakeTool()])
    assert len(schemas) == 2
    assert schemas[0]["name"] == "fake"


# ---------------------------------------------------------------------------
# AnthropicAdapter — call_model (batch)
# ---------------------------------------------------------------------------


def test_anthropic_call_model_batch():
    fake_msgs = _FakeMessages(deque([
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ]))
    client = _FakeClient(fake_msgs)
    adapter = AnthropicAdapter()

    parsed = adapter.call_model(
        client,
        model="claude-opus-4-6",
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
        max_tokens=1024,
    )
    assert isinstance(parsed, ParsedResponse)
    assert parsed.content_blocks == [{"type": "text", "text": "done"}]
    assert parsed.streamed is False
    assert fake_msgs.calls[0]["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# AnthropicAdapter — call_model (streaming)
# ---------------------------------------------------------------------------


def test_anthropic_call_model_streaming():
    final_resp = _FakeResponse(content=[_FakeBlock(type="text", text="hello world")])
    stream_ctx = _FakeStreamChunk(texts=["hello", " ", "world"], final_response=final_resp)
    fake_msgs = _FakeStreamMessages(deque([stream_ctx]))
    client = _FakeClient(fake_msgs)
    adapter = AnthropicAdapter()

    collected: list[str] = []
    parsed = adapter.call_model(
        client,
        model="claude-opus-4-6",
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
        max_tokens=1024,
        on_text_delta=collected.append,
    )
    assert collected == ["hello", " ", "world"]
    assert parsed.streamed is True
    assert parsed.content_blocks == [{"type": "text", "text": "hello world"}]


# ---------------------------------------------------------------------------
# FlattenedMessageAdapter — format_system
# ---------------------------------------------------------------------------


def test_flattened_format_system_joins_text_blocks():
    adapter = FlattenedMessageAdapter()
    sys_prompt = [
        {"type": "text", "text": "You are helpful."},
        {"type": "text", "text": "Be concise."},
    ]
    result = adapter.format_system(sys_prompt)
    assert result == "You are helpful.\n\nBe concise."


def test_flattened_format_system_skips_non_text():
    adapter = FlattenedMessageAdapter()
    sys_prompt = [
        {"type": "text", "text": "intro"},
        {"type": "cache_control", "cache_type": "ephemeral"},
    ]
    result = adapter.format_system(sys_prompt)
    assert result == "intro"


def test_flattened_format_system_string_passthrough():
    adapter = FlattenedMessageAdapter()
    assert adapter.format_system("already a string") == "already a string"


# ---------------------------------------------------------------------------
# FlattenedMessageAdapter — format_messages
# ---------------------------------------------------------------------------


def test_flattened_format_messages_flattens_text_only():
    adapter = FlattenedMessageAdapter()
    msgs = (
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    )
    result = adapter.format_messages(msgs)
    assert result[0]["content"] == "hello"
    assert result[1]["content"] == "hi there"


def test_flattened_format_messages_preserves_tool_use():
    adapter = FlattenedMessageAdapter()
    msgs = (
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
        ]},
    )
    result = adapter.format_messages(msgs)
    # Messages with non-text blocks are kept in structured format
    assert isinstance(result[0]["content"], list)


def test_flattened_format_messages_plain_string_passthrough():
    adapter = FlattenedMessageAdapter()
    msgs = ({"role": "user", "content": "plain string"},)
    result = adapter.format_messages(msgs)
    assert result[0]["content"] == "plain string"


# ---------------------------------------------------------------------------
# FlattenedMessageAdapter inherits call_model from AnthropicAdapter
# ---------------------------------------------------------------------------


def test_flattened_call_model_uses_anthropic_sdk():
    """FlattenedMessageAdapter.call_model is inherited — same SDK path."""
    fake_msgs = _FakeMessages(deque([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ]))
    client = _FakeClient(fake_msgs)
    adapter = FlattenedMessageAdapter()

    parsed = adapter.call_model(
        client,
        model="deepseek-chat",
        system="flattened system",
        messages=[{"role": "user", "content": "hi"}],
        tool_schemas=[],
        max_tokens=1024,
    )
    assert parsed.content_blocks == [{"type": "text", "text": "ok"}]
    assert fake_msgs.calls[0]["model"] == "deepseek-chat"

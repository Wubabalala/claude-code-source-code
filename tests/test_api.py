"""Tests for agent.api — error classification and message helpers."""
from agent.api import (
    is_recoverable,
    build_assistant_message,
    build_tool_result_block,
)


# ----------------------------------------------------------------------------
# is_recoverable
# ----------------------------------------------------------------------------


class _FakeError(Exception):
    def __init__(self, status_code=None):
        self.status_code = status_code
        super().__init__(f"Fake error {status_code}")


def test_is_recoverable_503():
    assert is_recoverable(_FakeError(status_code=503)) is True


def test_is_recoverable_429_rate_limit():
    assert is_recoverable(_FakeError(status_code=429)) is True


def test_is_recoverable_500():
    assert is_recoverable(_FakeError(status_code=500)) is True


def test_not_recoverable_401_auth():
    assert is_recoverable(_FakeError(status_code=401)) is False


def test_not_recoverable_400_bad_request():
    assert is_recoverable(_FakeError(status_code=400)) is False


def test_not_recoverable_unknown():
    """Errors without status_code default to non-recoverable (fail-closed)."""
    assert is_recoverable(Exception("unknown")) is False


# ----------------------------------------------------------------------------
# build_assistant_message — converts API response to message dict
# ----------------------------------------------------------------------------


class _FakeBlock:
    def __init__(self, type, **kwargs):
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def test_build_assistant_message_text_only():
    response = _FakeResponse(content=[_FakeBlock(type="text", text="hello")])
    msg = build_assistant_message(response)
    assert msg["role"] == "assistant"
    assert len(msg["content"]) == 1
    assert msg["content"][0] == {"type": "text", "text": "hello"}


def test_build_assistant_message_with_tool_use():
    response = _FakeResponse(content=[
        _FakeBlock(type="text", text="I'll read it."),
        _FakeBlock(type="tool_use", id="toolu_1", name="read_file", input={"file_path": "x"}),
    ])
    msg = build_assistant_message(response)
    assert len(msg["content"]) == 2
    assert msg["content"][1]["type"] == "tool_use"
    assert msg["content"][1]["id"] == "toolu_1"
    assert msg["content"][1]["name"] == "read_file"
    assert msg["content"][1]["input"] == {"file_path": "x"}


# ----------------------------------------------------------------------------
# build_tool_result_block
# ----------------------------------------------------------------------------


def test_build_tool_result_block_success():
    block = build_tool_result_block(tool_use_id="toolu_1", content="file content", is_error=False)
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_1"
    assert block["content"] == "file content"
    assert block.get("is_error", False) is False


def test_build_tool_result_block_error():
    block = build_tool_result_block(tool_use_id="toolu_2", content="boom", is_error=True)
    assert block["is_error"] is True

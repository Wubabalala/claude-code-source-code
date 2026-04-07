"""Tests for agent.main — only the testable helpers, not the REPL itself."""
from agent.main import get_tools, extract_final_text


def test_get_tools_returns_three_tools():
    tools = get_tools()
    assert len(tools) == 3


def test_get_tools_sorted_alphabetically():
    """CRITICAL: tools must be in stable order for prompt cache."""
    tools = get_tools()
    names = [t.name for t in tools]
    assert names == sorted(names)


def test_get_tools_includes_expected_tools():
    tools = get_tools()
    names = {t.name for t in tools}
    assert names == {"bash", "grep", "read_file"}


def test_extract_final_text_from_assistant_message():
    messages = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello there"}]},
    )
    assert extract_final_text(messages) == "hello there"


def test_extract_final_text_skips_tool_use_blocks():
    messages = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "the answer is x"}]},
    )
    # Should return the LAST assistant message's text
    assert extract_final_text(messages) == "the answer is x"


def test_extract_final_text_no_assistant_message():
    messages = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    )
    assert "no response" in extract_final_text(messages).lower()

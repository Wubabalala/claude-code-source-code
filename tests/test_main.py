"""Tests for agent.main — only the testable helpers, not the REPL itself."""
from agent.main import get_tools, extract_final_text


def test_get_tools_returns_builtin_tools_only():
    """get_tools() returns only the built-in tools. MCP tools are
    registered separately in repl() after server discovery."""
    tools = get_tools()
    assert len(tools) == 5  # bash, edit_file, grep, read_file, write_file


def test_get_tools_sorted_alphabetically():
    """CRITICAL: tools must be in stable order for prompt cache."""
    tools = get_tools()
    names = [t.name for t in tools]
    assert names == sorted(names)


def test_get_tools_includes_expected_builtins():
    tools = get_tools()
    names = {t.name for t in tools}
    assert names == {"bash", "edit_file", "grep", "read_file", "write_file"}


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


# ============================================================================
# Config loading tests (Task: env-based config)
# ============================================================================
import os
from agent.main import get_model_config, init_client


def test_get_model_config_defaults(monkeypatch):
    """With no env vars, returns the canonical defaults."""
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)
    primary, fallback = get_model_config()
    assert primary == "claude-opus-4-6"
    assert fallback == "claude-sonnet-4-6"


def test_get_model_config_overridden_by_env(monkeypatch):
    """Env vars override the defaults."""
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "custom-primary")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "custom-fallback")
    primary, fallback = get_model_config()
    assert primary == "custom-primary"
    assert fallback == "custom-fallback"


def test_init_client_without_base_url(monkeypatch):
    """With no ANTHROPIC_BASE_URL, client points at default Anthropic URL."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    client = init_client()
    # Default base_url points at official api.anthropic.com
    assert "anthropic.com" in str(client.base_url)


def test_init_client_with_custom_base_url(monkeypatch):
    """ANTHROPIC_BASE_URL overrides the default."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
    client = init_client()
    assert "proxy.example.com" in str(client.base_url)

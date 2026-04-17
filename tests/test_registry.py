"""Tests for agent.registry — provider registry."""
import pytest

from agent.adapter import AnthropicAdapter, FlattenedMessageAdapter, LLMAdapter
from agent.registry import get_adapter, register_provider, reset_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset registry before each test to avoid cross-test pollution."""
    reset_registry()
    yield
    reset_registry()


def test_get_adapter_claude_returns_anthropic():
    adapter = get_adapter("claude-opus-4-6")
    assert isinstance(adapter, AnthropicAdapter)
    # Should NOT be FlattenedMessageAdapter (which is a subclass)
    assert type(adapter) is AnthropicAdapter


def test_get_adapter_non_claude_returns_flattened():
    adapter = get_adapter("deepseek-chat")
    assert isinstance(adapter, FlattenedMessageAdapter)


def test_get_adapter_unknown_returns_fallback():
    adapter = get_adapter("some-random-model")
    assert isinstance(adapter, FlattenedMessageAdapter)


def test_get_adapter_longest_prefix_wins():
    """If both 'claude' and 'claude-opus' are registered, 'claude-opus' wins."""

    class SpecialAdapter(AnthropicAdapter):
        pass

    register_provider("claude-opus", SpecialAdapter)
    adapter = get_adapter("claude-opus-4-6")
    assert isinstance(adapter, SpecialAdapter)

    # "claude-sonnet-..." still hits the shorter "claude" prefix
    adapter2 = get_adapter("claude-sonnet-4-6")
    assert type(adapter2) is AnthropicAdapter


def test_register_overrides_existing():
    class Custom(AnthropicAdapter):
        pass

    register_provider("claude", Custom)
    adapter = get_adapter("claude-opus-4-6")
    assert isinstance(adapter, Custom)


def test_reset_registry_restores_defaults():
    register_provider("claude", FlattenedMessageAdapter)
    reset_registry()
    adapter = get_adapter("claude-opus-4-6")
    assert type(adapter) is AnthropicAdapter

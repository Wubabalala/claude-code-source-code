"""Provider registry — maps model name prefixes to LLMAdapter classes.

Usage:
    from agent.registry import get_adapter
    adapter = get_adapter("claude-opus-4-6")   # → AnthropicAdapter
    adapter = get_adapter("deepseek-chat")     # → FlattenedMessageAdapter (via "*")

Built-in registrations (at import time):
    "claude"  → AnthropicAdapter
    "*"       → FlattenedMessageAdapter   (catch-all fallback)
"""
from __future__ import annotations

from agent.adapter import AnthropicAdapter, FlattenedMessageAdapter, LLMAdapter


_REGISTRY: dict[str, type[LLMAdapter]] = {}


def register_provider(name: str, adapter_cls: type[LLMAdapter]) -> None:
    """Register an adapter class for a model-name prefix (or ``"*"`` for fallback)."""
    _REGISTRY[name] = adapter_cls


def get_adapter(model: str) -> LLMAdapter:
    """Resolve an adapter for *model* via longest-prefix match, then ``"*"``."""
    for prefix in sorted(_REGISTRY, key=len, reverse=True):
        if prefix != "*" and model.startswith(prefix):
            return _REGISTRY[prefix]()
    if "*" in _REGISTRY:
        return _REGISTRY["*"]()
    raise ValueError(f"No adapter registered for model {model!r}")


def reset_registry() -> None:
    """Reset to built-in defaults. For tests."""
    _REGISTRY.clear()
    _register_defaults()


def _register_defaults() -> None:
    register_provider("claude", AnthropicAdapter)
    register_provider("*", FlattenedMessageAdapter)


_register_defaults()

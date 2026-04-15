"""Shared types for the Phase 1 agent.

Design notes:
- State is frozen (immutable) — every loop iteration constructs a new State.
- messages is a tuple, not a list, so accidental .append() raises immediately.
- AgentResult is the loop exit envelope, never an exception.
- Note: ``frozen=True`` is shallow — the message dicts inside the tuple are
  still mutable. By convention, never mutate them; always construct new dicts.
"""
from dataclasses import dataclass
from typing import Any, Literal


# Message is intentionally typed loosely as dict — Anthropic SDK uses dicts.
# A real type would be a TypedDict, but Phase 1 keeps it simple.
Message = dict[str, Any]


@dataclass(frozen=True)
class State:
    """Single-turn loop state. Rebuilt every iteration, never mutated."""

    messages: tuple[Message, ...]
    turn: int
    fallback_model_used: bool
    output_retries: int
    transition_reason: Literal[
        "initial",
        "tool_use",
        "model_fallback",
        "output_recovery",
        "microcompact",
        "autocompact",
        "reactive_compact",
        "compact_tripped",
    ]
    microcompact_count: int = 0
    autocompact_count: int = 0
    consecutive_compact_failures: int = 0
    compact_tripped: bool = False
    reactive_compact_attempted: bool = False


@dataclass(frozen=True)
class AgentResult:
    """Loop exit envelope. Never raised — always returned."""

    status: Literal["completed", "max_turns", "model_error", "prompt_too_long"]
    messages: tuple[Message, ...]
    reason: str

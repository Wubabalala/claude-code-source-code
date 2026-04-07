"""Tests for agent.types — immutable State and AgentResult."""
import pytest
from dataclasses import replace, FrozenInstanceError
from agent.types import State, AgentResult


def test_state_is_frozen():
    """State must be immutable — direct mutation raises FrozenInstanceError."""
    state = State(
        messages=(),
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )
    with pytest.raises(FrozenInstanceError):
        state.turn = 2


def test_state_uses_replace_for_updates():
    """dataclasses.replace returns a NEW state, original unchanged."""
    state = State(
        messages=(),
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )
    new_state = replace(state, turn=2, transition_reason="tool_use")
    assert state.turn == 1
    assert new_state.turn == 2
    assert new_state.transition_reason == "tool_use"
    # Unchanged fields are preserved
    assert new_state.fallback_model_used is False


def test_state_messages_is_tuple():
    """messages must be a tuple, not a list — for compile-time immutability."""
    state = State(
        messages=({"role": "user", "content": "hi"},),
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )
    assert isinstance(state.messages, tuple)
    # Tuples have no append method
    assert not hasattr(state.messages, "append")


def test_agent_result_status_values():
    """AgentResult exposes the 3 valid exit statuses."""
    result = AgentResult(status="completed", messages=(), reason="done")
    assert result.status == "completed"
    # Other valid statuses
    assert AgentResult(status="max_turns", messages=(), reason="").status == "max_turns"
    assert AgentResult(status="model_error", messages=(), reason="").status == "model_error"

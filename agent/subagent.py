"""Sub-agent spawning — Phase 7 Layer 1.

Two modes:
  - Fork: inherits parent's conversation_history + appends directive.
    Shares client for prompt cache locality.
  - Fresh: starts from a blank slate with only the task prompt.

Both modes produce an AgentResult via the standard run_agent_loop.
Sub-agents have independent State (turn, fallback, compact counters
all reset), do NOT inherit parent's session permission approvals,
and do NOT auto-inject results into the parent conversation.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from agent.loop import run_agent_loop
from agent.types import AgentResult, Message


def _sub_session_id(parent_session_id: str) -> str:
    return f"{parent_session_id}:sub-{uuid.uuid4().hex[:8]}"


def run_fork_agent(
    parent_messages: tuple[Message, ...],
    directive: str,
    *,
    client: Any,
    tools: list,
    system_prompt: list[dict],
    parent_session_id: str = "",
    audit_logger: Any = None,
    retry_config: Any = None,
    hooks_config: Any = None,
    max_turns: int = 25,
) -> AgentResult:
    """Fork mode: inherit parent conversation + append directive."""
    from agent.audit import emit

    sub_sid = _sub_session_id(parent_session_id)
    emit(audit_logger, "agent.spawn", session_id=sub_sid,
         mode="fork", parent_session_id=parent_session_id)

    forked = parent_messages + (
        {"role": "user", "content": [{"type": "text", "text": directive}]},
    )
    result = run_agent_loop(
        client=client,
        initial_messages=forked,
        tools=tools,
        system_prompt=system_prompt,
        max_turns=max_turns,
        audit_logger=audit_logger,
        session_id=sub_sid,
        retry_config=retry_config,
        hooks_config=hooks_config,
    )

    emit(audit_logger, "agent.complete", session_id=sub_sid,
         status=result.status, message_count=len(result.messages))
    return result


def run_fresh_agent(
    prompt: str,
    *,
    client: Any,
    tools: list,
    system_prompt: list[dict],
    parent_session_id: str = "",
    audit_logger: Any = None,
    retry_config: Any = None,
    hooks_config: Any = None,
    max_turns: int = 25,
) -> AgentResult:
    """Fresh mode: independent context with only the task prompt."""
    from agent.audit import emit

    sub_sid = _sub_session_id(parent_session_id)
    emit(audit_logger, "agent.spawn", session_id=sub_sid,
         mode="fresh", parent_session_id=parent_session_id)

    fresh = ({"role": "user", "content": [{"type": "text", "text": prompt}]},)
    result = run_agent_loop(
        client=client,
        initial_messages=fresh,
        tools=tools,
        system_prompt=system_prompt,
        max_turns=max_turns,
        audit_logger=audit_logger,
        session_id=sub_sid,
        retry_config=retry_config,
        hooks_config=hooks_config,
    )

    emit(audit_logger, "agent.complete", session_id=sub_sid,
         status=result.status, message_count=len(result.messages))
    return result

"""Agent loop — the heart of the agent.

Phase 1: happy path, max_turns, model fallback, output recovery.
Phase 2: + microcompact / autocompact (proactive), circuit breaker,
         reactive compaction on prompt_too_long.

Contracts (see docs/plans Phase 2 plan):
  A  autocompact returns role="user" summary only; no fake assistant ack
  B  autocompact preserves last K messages for tool_use/tool_result pairing
  C  circuit breaker gates proactive Auto only — Micro and reactive Auto are
     always allowed
  D  microcompact / autocompact must signal "no change" so the loop doesn't
     infinite-continue
  D-bis  proactive Auto must strictly reduce token count; otherwise treated
     as a failure
  E  only dataclasses.replace() allowed past the initial State() construction
"""
from dataclasses import replace
from typing import Callable, Optional

from agent.types import State, AgentResult, Message
from agent.tools import Tool
from agent.api import (
    build_assistant_message,
    build_tool_result_block,
    is_prompt_too_long,
    is_recoverable,
)
from agent.compact import (
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    autocompact,
    estimate_tokens,
    microcompact,
    should_autocompact,
    should_microcompact,
)
from agent.config import RetryConfig
from agent.retry import call_with_retry


def run_agent_loop(
    client,
    initial_messages: tuple[Message, ...],
    tools: list[Tool],
    system_prompt: list[dict],
    max_turns: int = 25,
    primary_model: str = "claude-opus-4-6",
    fallback_model: str = "claude-sonnet-4-6",
    on_api_response: Optional[Callable] = None,
    on_state_transition: Optional[Callable[[State], None]] = None,
    audit_logger=None,
    session_id: Optional[str] = None,
    retry_config=None,
) -> AgentResult:
    """Run the agent loop until completion, max_turns, or unrecoverable error.

    Returns AgentResult — never raises (all errors caught and wrapped).

    Optional hooks:
      on_api_response — called with response.usage after every successful API
        call (used by the REPL to print cache stats)
      on_state_transition — called with the new State after every
        dataclasses.replace() transition. Tests use this to assert that
        fields like compact_tripped aren't silently reset across transitions.
      audit_logger — Phase 4: structured logger (agent.audit) for boundary
        events. None = audit off.
      session_id — Phase 4: correlation id attached to audit events.
    """
    _rc = retry_config or RetryConfig()

    state = State(
        messages=initial_messages,
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )

    def _transition(new_state: State) -> State:
        if on_state_transition is not None:
            on_state_transition(new_state)
        # Contract L: compact.transition audit event fires whenever the
        # transition_reason reflects a compaction-related transition (including
        # the trip event). Non-compact transitions (initial/tool_use/model_
        # fallback/output_recovery) are already observable via on_state_transition;
        # we don't double-log them to keep audit volume low.
        if audit_logger is not None and new_state.transition_reason in (
            "microcompact", "autocompact", "reactive_compact", "compact_tripped",
        ):
            from agent.audit import emit as _emit
            _emit(
                audit_logger,
                "compact.transition",
                session_id=session_id,
                turn=new_state.turn,
                reason=new_state.transition_reason,
                compact_tripped=new_state.compact_tripped,
                autocompact_count=new_state.autocompact_count,
                microcompact_count=new_state.microcompact_count,
                consecutive_compact_failures=new_state.consecutive_compact_failures,
            )
        return new_state

    while True:
        # Exit condition 2: max_turns
        if state.turn > max_turns:
            return AgentResult(
                status="max_turns",
                messages=state.messages,
                reason=f"reached max turns ({max_turns})",
            )

        model_to_use = primary_model if not state.fallback_model_used else fallback_model

        # Continue site 3: proactive compaction (microcompact + autocompact).
        # Only continues when messages actually changed AND (for Auto) tokens
        # strictly decreased. Otherwise falls through to the API call so the
        # reactive path can take over if the request truly overflows.
        if should_microcompact(state.messages):
            did_compact = False

            if should_autocompact(state.messages) and not state.compact_tripped:
                before_tokens = estimate_tokens(state.messages)
                new_msgs = autocompact(state.messages, client, model_to_use, system_prompt, retry_budget=min(_rc.budget, 2))
                auto_made_progress = (
                    new_msgs is not None
                    and estimate_tokens(new_msgs) < before_tokens
                )
                if auto_made_progress:
                    state = _transition(replace(
                        state,
                        messages=new_msgs,
                        autocompact_count=state.autocompact_count + 1,
                        consecutive_compact_failures=0,
                        compact_tripped=False,
                        transition_reason="autocompact",
                    ))
                    did_compact = True
                else:
                    # Failure or no-progress → increment + maybe trip circuit
                    # breaker. Do NOT continue; fall through to Micro below.
                    failures = state.consecutive_compact_failures + 1
                    tripped = failures >= MAX_CONSECUTIVE_COMPACT_FAILURES
                    state = _transition(replace(
                        state,
                        consecutive_compact_failures=failures,
                        compact_tripped=tripped,
                        transition_reason=(
                            "compact_tripped" if tripped else state.transition_reason
                        ),
                    ))

            if not did_compact:
                # Micro is always allowed (contract C); but must only continue
                # if it actually changed something (contract D).
                new_msgs, changed = microcompact(state.messages)
                if changed:
                    state = _transition(replace(
                        state,
                        messages=new_msgs,
                        microcompact_count=state.microcompact_count + 1,
                        consecutive_compact_failures=0,
                        compact_tripped=False,
                        transition_reason="microcompact",
                    ))
                    did_compact = True

            if did_compact:
                continue  # Re-evaluate from the top of the turn

        # Main API call (with retry-on-recoverable-error wrapping)
        try:
            response = call_with_retry(
                lambda: client.messages.create(
                    model=model_to_use,
                    messages=list(state.messages),
                    system=system_prompt,
                    tools=[t.to_anthropic_schema() for t in tools],
                    max_tokens=8192,
                ),
                budget=_rc.budget,
                backoff_base=_rc.backoff_base,
                backoff_max=_rc.backoff_max,
                jitter=_rc.jitter,
                audit_logger=audit_logger,
                session_id=session_id,
            )
        except Exception as e:
            # Continue site 4: reactive compaction on prompt_too_long.
            # Not gated by compact_tripped (last-resort), but gated by the
            # one-shot latch reactive_compact_attempted (contract A).
            if is_prompt_too_long(e):
                if state.reactive_compact_attempted:
                    return AgentResult(
                        status="prompt_too_long",
                        messages=state.messages,
                        reason="prompt still too long after reactive compaction",
                    )
                new_msgs = autocompact(state.messages, client, model_to_use, system_prompt, retry_budget=min(_rc.budget, 2))
                if new_msgs is not None:
                    state = _transition(replace(
                        state,
                        messages=new_msgs,
                        autocompact_count=state.autocompact_count + 1,
                        consecutive_compact_failures=0,
                        compact_tripped=False,
                        reactive_compact_attempted=True,
                        transition_reason="reactive_compact",
                    ))
                    continue
                return AgentResult(
                    status="prompt_too_long",
                    messages=state.messages,
                    reason="autocompact failed on prompt_too_long recovery",
                )

            # Continue site 1: model fallback (withheld error)
            if not state.fallback_model_used and is_recoverable(e):
                state = _transition(replace(
                    state,
                    fallback_model_used=True,
                    transition_reason="model_fallback",
                ))
                continue
            return AgentResult(
                status="model_error",
                messages=state.messages,
                reason=str(e),
            )

        if on_api_response is not None:
            on_api_response(getattr(response, "usage", None))

        assistant_message = build_assistant_message(response)
        new_messages = state.messages + (assistant_message,)

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        # Exit condition 1: completed (no tool_use)
        if not tool_use_blocks:
            # Continue site 2: output token recovery
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "max_tokens" and state.output_retries < 3:
                state = _transition(replace(
                    state,
                    messages=new_messages,
                    turn=state.turn + 1,
                    output_retries=state.output_retries + 1,
                    transition_reason="output_recovery",
                ))
                continue

            return AgentResult(
                status="completed",
                messages=new_messages,
                reason="model finished naturally",
            )

        # Tool execution — validate, check permissions (ALLOW/ASK/DENY), execute
        from agent.tools import PermissionDecision  # local import to avoid circular
        from agent.permissions import prompt_user_for_permission
        from agent.audit import emit as _audit_emit, hash_path as _hash_path
        import time as _time

        tool_results = []
        for tool_use in tool_use_blocks:
            tool = next((t for t in tools if t.name == tool_use.name), None)
            if tool is None:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Unknown tool: {tool_use.name}", is_error=True,
                ))
                continue

            try:
                validated = tool.input_model(**tool_use.input)
            except Exception as e:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Invalid input: {e}", is_error=True,
                ))
                continue

            outcome = tool.check_permissions(validated)
            target_hash = _hash_path(outcome.target) if outcome.target else None

            if outcome.decision == PermissionDecision.DENY:
                # Contract L: permission.decision owner = loop (single emit site)
                _audit_emit(audit_logger, "permission.decision",
                            session_id=session_id, turn=state.turn,
                            tool=tool.name, decision="DENY",
                            path=target_hash)
                tool_results.append(build_tool_result_block(
                    tool_use.id,
                    f"Permission denied: {outcome.risk or 'hard-denied'}",
                    is_error=True,
                ))
                continue

            if outcome.decision == PermissionDecision.ASK:
                final = prompt_user_for_permission(outcome, audit_logger=audit_logger)
                _audit_emit(audit_logger, "permission.decision",
                            session_id=session_id, turn=state.turn,
                            tool=tool.name, decision=final.upper(),
                            path=target_hash)
                if final == PermissionDecision.DENY:
                    tool_results.append(build_tool_result_block(
                        tool_use.id,
                        f"User denied: {outcome.target or tool.name}",
                        is_error=True,
                    ))
                    continue
            else:
                # ALLOW path with no user prompt (e.g. read-only tool)
                _audit_emit(audit_logger, "permission.decision",
                            session_id=session_id, turn=state.turn,
                            tool=tool.name, decision="ALLOW",
                            path=target_hash)

            # Execute with start/end audit bookend
            _audit_emit(audit_logger, "tool.exec.start",
                        session_id=session_id, turn=state.turn, tool=tool.name)
            _t0 = _time.monotonic()
            try:
                result = tool.execute(validated)
                tool_results.append(build_tool_result_block(
                    tool_use.id, result.output, is_error=result.is_error,
                ))
                _audit_emit(audit_logger, "tool.exec.end",
                            session_id=session_id, turn=state.turn, tool=tool.name,
                            duration_ms=int((_time.monotonic() - _t0) * 1000),
                            is_error=bool(result.is_error),
                            size=len(result.output or ""))
            except Exception as e:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Execution error: {e}", is_error=True,
                ))
                _audit_emit(audit_logger, "tool.exec.end",
                            session_id=session_id, turn=state.turn, tool=tool.name,
                            duration_ms=int((_time.monotonic() - _t0) * 1000),
                            is_error=True, size=0)

        tool_result_message = {"role": "user", "content": tool_results}
        new_messages = new_messages + (tool_result_message,)

        state = _transition(replace(
            state,
            messages=new_messages,
            turn=state.turn + 1,
            output_retries=0,
            transition_reason="tool_use",
        ))

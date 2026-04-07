"""Agent loop — the heart of the agent.

See docs/plans/2026-04-07-phase1-skeleton-design.md, Section 2.

This file implements ONLY the happy path and max_turns exit. Continue sites
(model fallback, output recovery) and tool execution come in tasks 9 and 10.
"""
from dataclasses import replace
from typing import Callable, Optional

from agent.types import State, AgentResult, Message
from agent.tools import Tool
from agent.api import build_assistant_message, build_tool_result_block, is_recoverable


def run_agent_loop(
    client,
    initial_messages: tuple[Message, ...],
    tools: list[Tool],
    system_prompt: list[dict],
    max_turns: int = 25,
    primary_model: str = "claude-opus-4-6",
    fallback_model: str = "claude-sonnet-4-6",
    on_api_response: Optional[Callable] = None,
) -> AgentResult:
    """Run the agent loop until completion, max_turns, or unrecoverable error.

    Returns AgentResult — never raises (all errors caught and wrapped).
    """
    state = State(
        messages=initial_messages,
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )

    while True:
        # Exit condition 2: max_turns
        if state.turn > max_turns:
            return AgentResult(
                status="max_turns",
                messages=state.messages,
                reason=f"reached max turns ({max_turns})",
            )

        # Call the model (Phase 1 = batch, not streaming)
        model_to_use = primary_model if not state.fallback_model_used else fallback_model
        try:
            response = client.messages.create(
                model=model_to_use,
                messages=list(state.messages),
                system=system_prompt,
                tools=[t.to_anthropic_schema() for t in tools],
                max_tokens=8192,
            )
        except Exception as e:
            # Continue site 1 (model fallback) — implemented in Task 9
            return AgentResult(
                status="model_error",
                messages=state.messages,
                reason=str(e),
            )

        if on_api_response is not None:
            on_api_response(getattr(response, "usage", None))

        # Append assistant message
        assistant_message = build_assistant_message(response)
        new_messages = state.messages + (assistant_message,)

        # Detect tool_use via content blocks (NEVER trust stop_reason)
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        # Exit condition 1: completed (no tool_use)
        if not tool_use_blocks:
            return AgentResult(
                status="completed",
                messages=new_messages,
                reason="model finished naturally",
            )

        # Tool execution — Task 10 will fill this in with permission checks.
        # For now, naive execution to make Task 8 tests pass.
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
                result = tool.execute(validated)
                tool_results.append(build_tool_result_block(
                    tool_use.id, result.output, is_error=result.is_error,
                ))
            except Exception as e:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Execution error: {e}", is_error=True,
                ))

        tool_result_message = {"role": "user", "content": tool_results}
        new_messages = new_messages + (tool_result_message,)

        # Build next state — full reconstruction, no mutation
        state = State(
            messages=new_messages,
            turn=state.turn + 1,
            fallback_model_used=state.fallback_model_used,
            output_retries=0,
            transition_reason="tool_use",
        )
        # Implicit continue → top of while

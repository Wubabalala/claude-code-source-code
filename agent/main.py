"""CLI REPL — the lungs of the agent.

Slim entrypoint that delegates initialisation to ``agent.bootstrap``
and slash commands to ``agent.commands``.
"""
import datetime
import os
import platform
import sys

from agent.loop import run_agent_loop
from agent.prompt import build_system_prompt

# Re-exports from agent.client — existing tests import these from here.
from agent.client import init_client, get_tools  # noqa: F401


def get_model_config() -> tuple[str, str]:
    """Legacy helper kept for back-compat with tests."""
    primary = os.environ.get("AGENT_PRIMARY_MODEL", "claude-opus-4-6")
    fallback = os.environ.get("AGENT_FALLBACK_MODEL", "claude-sonnet-4-6")
    return primary, fallback


def print_cache_stats(usage) -> None:
    """Print cache hit info so the user can see prompt cache working."""
    if usage is None:
        return
    from agent.ui import token_stats
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    total_cached = cache_read + cache_create
    hit_rate = (cache_read / total_cached * 100) if total_cached > 0 else 0
    token_stats(input_tokens, cache_read, cache_create, output_tokens, hit_rate)


def extract_final_text(messages: tuple) -> str:
    """Pull the text content from the LAST assistant message."""
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text_parts = [
                block["text"]
                for block in msg["content"]
                if block.get("type") == "text"
            ]
            if text_parts:
                return "\n".join(text_parts)
    return "(no response)"


def repl():
    import atexit
    import signal

    from agent.audit import emit as audit_emit
    from agent.bootstrap import AgentApp
    from agent.commands import dispatch
    from agent.ui import banner as ui_banner, assistant_response as ui_response

    app = AgentApp.create(web_mode=False)

    # atexit — safety net for abnormal exit; explicit cleanup on /exit and Ctrl-C
    def _cleanup():
        app.cleanup()

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda _s, _f: sys.exit(0))

    audit_emit(app.audit_logger, "session.open", session_id=app.session_id)

    # Hooks: SessionStart
    from agent.hooks import fire_hooks, EVENT_SESSION_START
    if app.cfg.hooks.session_start:
        fire_hooks(EVENT_SESSION_START, app.cfg.hooks.session_start,
                   {"session_id": app.session_id},
                   timeout=app.cfg.hooks.timeout_seconds,
                   audit_logger=app.audit_logger, session_id=app.session_id)

    # Banner
    base_url_display = os.environ.get("ANTHROPIC_BASE_URL", "<official>")
    mcp_tool_count = len(app.tools) - 5  # 5 built-in tools
    ui_banner(
        "Phase 8", base_url_display, app.primary_model, app.fallback_model,
        app.session_id, memory_count=len(app.memory_entries),
        mcp_tools=max(0, mcp_tool_count),
    )

    while True:
        # 1. Read input
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            app.cleanup()
            print("\nbye.")
            break

        if not user_input:
            continue

        # 2. Slash command dispatch
        result = dispatch(user_input, app)
        if result is not None:
            if result.should_break:
                app.cleanup()
                break
            continue

        # 3. Append user message
        new_user_message = {
            "role": "user",
            "content": [{"type": "text", "text": user_input}],
        }
        turn_messages = app.conversation_history + (new_user_message,)

        # 4. Build system prompt (static text identical every call → cache hits)
        system_prompt = build_system_prompt(
            cwd=os.getcwd(),
            os_name=platform.system(),
            today=datetime.date.today().isoformat(),
            memory_entries=app.memory_entries,
        )

        # 5. Run loop
        try:
            _streaming_printed = [False]

            def _on_delta(text: str):
                if not _streaming_printed[0]:
                    print()
                    _streaming_printed[0] = True
                print(text, end="", flush=True)

            loop_result = run_agent_loop(
                client=app.client,
                initial_messages=turn_messages,
                tools=app.tools,
                system_prompt=system_prompt,
                max_turns=app.cfg.repl.max_turns_per_query,
                primary_model=app.primary_model,
                fallback_model=app.fallback_model,
                on_api_response=print_cache_stats,
                audit_logger=app.audit_logger,
                session_id=app.session_id,
                retry_config=app.cfg.retry,
                hooks_config=app.cfg.hooks,
                on_text_delta=_on_delta,
            )
            if _streaming_printed[0]:
                print()
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:
            print(f"\n[unexpected error] {type(e).__name__}: {e}")
            print("(history kept; you can /reset to start over)")
            continue

        # 6. Handle result + contract P-bis commit
        prior = app.conversation_history
        if loop_result.status == "completed":
            app.commit_result(prior, tuple(loop_result.messages))
            app.conversation_history = loop_result.messages
            if not _streaming_printed[0]:
                ui_response(extract_final_text(loop_result.messages))
            print()
        elif loop_result.status == "max_turns":
            app.commit_result(prior, tuple(loop_result.messages))
            app.conversation_history = loop_result.messages
            print(f"\n[reached max turns: {app.cfg.repl.max_turns_per_query}]")
            print("(partial result above, /reset to start over)\n")
        elif loop_result.status == "prompt_too_long":
            app.commit_result(prior, tuple(loop_result.messages))
            app.conversation_history = loop_result.messages
            print(f"\n[context overflow] {loop_result.reason}")
            print("(history preserved; /reset to clear, or try a shorter question)\n")
        elif loop_result.status == "model_error":
            print(f"\n[model error] {loop_result.reason}")
            print("(history unchanged, you can retry)\n")


if __name__ == "__main__":
    repl()

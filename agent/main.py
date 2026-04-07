"""CLI REPL — the lungs of the agent.

Manages session-level conversation history and delegates each turn to
the agent loop.
"""
import os
import sys
import datetime
import platform

from anthropic import Anthropic

from agent.loop import run_agent_loop
from agent.tools import Tool, ReadFileTool, GrepTool, BashTool
from agent.prompt import build_system_prompt


PRIMARY_MODEL = "claude-opus-4-6"
FALLBACK_MODEL = "claude-sonnet-4-6"
MAX_TURNS_PER_QUERY = 25


def get_tools() -> list[Tool]:
    """Tool registry. Sorted alphabetically — order MUST be stable for cache."""
    return sorted(
        [BashTool(), GrepTool(), ReadFileTool()],
        key=lambda t: t.name,
    )


def init_client() -> Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: please set ANTHROPIC_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    return Anthropic(api_key=api_key)


def print_cache_stats(usage) -> None:
    """Print cache hit info so the user can see prompt cache working."""
    if usage is None:
        return
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    total_cached = cache_read + cache_create
    hit_rate = (cache_read / total_cached * 100) if total_cached > 0 else 0
    print(
        f"  [tokens] in={input_tokens} cache_read={cache_read} "
        f"cache_create={cache_create} out={output_tokens} hit={hit_rate:.0f}%"
    )


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
    client = init_client()
    tools = get_tools()

    print("Code Repo Assistant (Phase 1)")
    print("Type your question. /exit to quit, /reset to clear history.\n")

    conversation_history: tuple = ()

    while True:
        # 1. Read input
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not user_input:
            continue

        # 2. Slash commands
        if user_input == "/exit":
            print("bye.")
            break
        if user_input == "/reset":
            conversation_history = ()
            print("(history cleared)")
            continue

        # 3. Append user message
        new_user_message = {
            "role": "user",
            "content": [{"type": "text", "text": user_input}],
        }
        turn_messages = conversation_history + (new_user_message,)

        # 4. Build system prompt (static text identical every call → cache hits)
        system_prompt = build_system_prompt(
            cwd=os.getcwd(),
            os_name=platform.system(),
            today=datetime.date.today().isoformat(),
        )

        # 5. Run loop
        try:
            result = run_agent_loop(
                client=client,
                initial_messages=turn_messages,
                tools=tools,
                system_prompt=system_prompt,
                max_turns=MAX_TURNS_PER_QUERY,
                primary_model=PRIMARY_MODEL,
                fallback_model=FALLBACK_MODEL,
                on_api_response=print_cache_stats,
            )
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:
            print(f"\n[unexpected error] {type(e).__name__}: {e}")
            print("(history kept; you can /reset to start over)")
            continue

        # 6. Handle result
        if result.status == "completed":
            conversation_history = result.messages
            print(f"\n{extract_final_text(result.messages)}\n")
        elif result.status == "max_turns":
            conversation_history = result.messages
            print(f"\n[reached max turns: {MAX_TURNS_PER_QUERY}]")
            print("(partial result above, /reset to start over)\n")
        elif result.status == "model_error":
            # Do NOT update history — keeps conversation clean for retry
            print(f"\n[model error] {result.reason}")
            print("(history unchanged, you can retry)\n")


if __name__ == "__main__":
    repl()

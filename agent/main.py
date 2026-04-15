"""CLI REPL — the lungs of the agent.

Manages session-level conversation history and delegates each turn to
the agent loop.
"""
import os
import sys
import datetime
import platform

from dotenv import load_dotenv
from anthropic import Anthropic

from agent.loop import run_agent_loop
from agent.tools import Tool, ReadFileTool, GrepTool, BashTool
from agent.prompt import build_system_prompt


MAX_TURNS_PER_QUERY = 25


def get_model_config() -> tuple[str, str]:
    """Return (primary_model, fallback_model) from env vars with defaults.

    Env vars:
      AGENT_PRIMARY_MODEL   (default: claude-opus-4-6)
      AGENT_FALLBACK_MODEL  (default: claude-sonnet-4-6)
    """
    primary = os.environ.get("AGENT_PRIMARY_MODEL", "claude-opus-4-6")
    fallback = os.environ.get("AGENT_FALLBACK_MODEL", "claude-sonnet-4-6")
    return primary, fallback


def get_tools() -> list[Tool]:
    """Tool registry. Sorted alphabetically — order MUST be stable for cache."""
    return sorted(
        [BashTool(), GrepTool(), ReadFileTool()],
        key=lambda t: t.name,
    )


def init_client() -> Anthropic:
    """Initialize Anthropic client with optional custom base URL.

    Env vars:
      ANTHROPIC_API_KEY   (required)
      ANTHROPIC_BASE_URL  (optional — for reverse proxies like cc-switch,
                          one-api, LiteLLM Proxy, etc.)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: please set ANTHROPIC_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    kwargs: dict = {"api_key": api_key}
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return Anthropic(**kwargs)


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
    load_dotenv()  # load .env from cwd if present; noop if missing
    client = init_client()
    primary_model, fallback_model = get_model_config()
    tools = get_tools()

    print("Code Repo Assistant (Phase 1)")
    base_url_display = os.environ.get("ANTHROPIC_BASE_URL", "<official>")
    print(f"  base_url: {base_url_display}")
    print(f"  primary:  {primary_model}")
    print(f"  fallback: {fallback_model}")
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
                primary_model=primary_model,
                fallback_model=fallback_model,
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
        elif result.status == "prompt_too_long":
            # Self-healing failed. Preserve history so the user can decide
            # (Context goal: do not auto-clear progress).
            conversation_history = result.messages
            print(f"\n[context overflow] {result.reason}")
            print("(history preserved; /reset to clear, or try a shorter question)\n")
        elif result.status == "model_error":
            # Do NOT update history — keeps conversation clean for retry
            print(f"\n[model error] {result.reason}")
            print("(history unchanged, you can retry)\n")


if __name__ == "__main__":
    repl()

"""CLI REPL — the lungs of the agent.

Phase 4 additions:
  - Loads ~/.agent/config.toml (or AGENT_CONFIG_PATH) via agent/config.py
  - Initialises the `agent.audit` structured logger
  - Maintains a SessionWriter per REPL run; commits messages with prefix /
    snapshot semantics after each query
  - /resume <prefix> and /sessions slash commands
  - /reset starts a fresh session_id
"""
import os
import sys
import datetime
import platform
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

from agent.audit import emit as audit_emit, get_audit_logger
from agent.compact import configure_compact
from agent.config import AgentConfig, load_config
from agent.loop import run_agent_loop
from agent.prompt import build_system_prompt
from agent.session import (
    AmbiguousPrefixError,
    NoSuchSessionError,
    SessionError,
    SessionWriter,
    UnsupportedSessionVersion,
    list_sessions,
    load_session,
    new_session_id,
    resolve_prefix,
    truncate_corrupt_tail,
)
from agent.tools import BashTool, GrepTool, ReadFileTool, Tool


def get_model_config() -> tuple[str, str]:
    """Legacy helper kept for back-compat with tests. Phase 4 real path is
    `load_config().repl.{primary,fallback}_model`, but env vars still win
    via `_apply_env_overrides` in agent/config.py."""
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


def _commit_to_session(writer: SessionWriter, prior: tuple, new: tuple) -> None:
    """Contract P-bis: if `new` is a pure prefix-extension of `prior`, append
    each delta `message`; otherwise write one `snapshot` carrying the full
    new history (Phase 2 compaction rewrite)."""
    if len(new) >= len(prior) and tuple(new[:len(prior)]) == tuple(prior):
        for msg in new[len(prior):]:
            writer.append_message(msg)
    else:
        writer.append_snapshot(new)


def _new_writer(session_id: str, cfg: AgentConfig, primary_model: str) -> SessionWriter:
    return SessionWriter(
        session_id,
        base_dir=Path(cfg.session.base_dir).expanduser(),
        cwd=os.getcwd(),
        model=primary_model,
    )


def _handle_resume(
    arg: str,
    *,
    conversation_history: tuple,
    cfg: AgentConfig,
    audit_logger,
    current_session_id: str,
    primary_model: str,
):
    """Implements /resume <prefix>. Returns (new_session_id, new_history,
    new_writer) on success; raises on failure. Caller is responsible for
    enforcing the contract R window (conversation must be empty)."""
    if not cfg.session.resume_enabled:
        raise SessionError("session resume is disabled in config")
    if conversation_history:
        n = len(conversation_history)
        raise SessionError(
            f"session already has {n} messages; "
            f"use /exit and relaunch to resume"
        )

    base = Path(cfg.session.base_dir).expanduser()
    try:
        sid = resolve_prefix(base, arg)
    except AmbiguousPrefixError as e:
        cands = ", ".join(s[:8] for s in e.candidates)
        raise SessionError(f"prefix {arg!r} is ambiguous; candidates: {cands}")
    except NoSuchSessionError:
        raise SessionError(f"no session matching prefix {arg!r}")

    path = base / f"{sid}.jsonl"
    result = load_session(path)

    # Physically remove corrupt tail so SessionWriter doesn't append
    # after garbage (which would turn tolerable tail-corruption into
    # fatal mid-file corruption on the next resume).
    if result.truncated_tail_lines > 0:
        truncate_corrupt_tail(path)

    # Log the resume event and any tail truncation separately
    audit_emit(audit_logger, "session.resume",
               session_id=sid, msg=f"resumed from {arg!r}")
    if result.truncated_tail_lines:
        audit_emit(audit_logger, "session.recover.truncate",
                   session_id=sid,
                   msg=f"dropped {result.truncated_tail_lines} corrupt tail lines")

    writer = _new_writer(sid, cfg, primary_model)
    new_history = tuple(result.messages)
    last_iso = datetime.datetime.fromtimestamp(result.last_updated).isoformat(
        timespec="seconds")
    print(
        f"resumed {sid} ({len(new_history)} messages, last updated {last_iso})"
    )
    if result.truncated_tail_lines:
        print(
            f"  (recovered with truncated tail: dropped "
            f"{result.truncated_tail_lines} corrupt lines)"
        )
    return sid, new_history, writer


def _handle_sessions(cfg: AgentConfig) -> None:
    base = Path(cfg.session.base_dir).expanduser()
    summaries = list_sessions(base, limit=20)
    if not summaries:
        print("(no sessions)")
        return
    for s in summaries:
        mtime = datetime.datetime.fromtimestamp(s.last_updated).isoformat(
            timespec="seconds")
        print(f"  {s.session_id[:8]}  msgs={s.message_count:3d}  {mtime}")


def repl():
    load_dotenv()  # load .env from cwd if present; noop if missing
    client = init_client()

    from agent.permissions import configure_permissions

    cfg = load_config()
    configure_compact(cfg.compact)
    configure_permissions(cfg.permissions)
    audit_logger = get_audit_logger(
        audit_file=Path(cfg.logging.audit_file).expanduser(),
        level=cfg.logging.level,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
    )

    primary_model = cfg.repl.primary_model
    fallback_model = cfg.repl.fallback_model
    tools = get_tools()

    session_id = new_session_id()
    writer = _new_writer(session_id, cfg, primary_model)
    audit_emit(audit_logger, "session.open", session_id=session_id)

    print("Code Repo Assistant (Phase 4)")
    base_url_display = os.environ.get("ANTHROPIC_BASE_URL", "<official>")
    print(f"  base_url: {base_url_display}")
    print(f"  primary:  {primary_model}")
    print(f"  fallback: {fallback_model}")
    print(f"  session:  {session_id[:8]}")
    print(
        "Type your question. /exit to quit, /reset to start fresh, "
        "/resume <id>, /sessions\n"
    )

    conversation_history: tuple = ()

    while True:
        # 1. Read input
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            audit_emit(audit_logger, "session.close", session_id=session_id,
                       msg="eof or interrupt")
            print("\nbye.")
            break

        if not user_input:
            continue

        # 2. Slash commands
        if user_input == "/exit":
            audit_emit(audit_logger, "session.close", session_id=session_id,
                       msg="exit")
            print("bye.")
            break
        if user_input == "/reset":
            conversation_history = ()
            session_id = new_session_id()
            writer = _new_writer(session_id, cfg, primary_model)
            audit_emit(audit_logger, "session.open", session_id=session_id,
                       msg="reset")
            print(f"(history cleared; new session {session_id[:8]})")
            continue
        if user_input == "/sessions":
            _handle_sessions(cfg)
            continue
        if user_input.startswith("/resume"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("usage: /resume <session-id-prefix>")
                continue
            try:
                session_id, conversation_history, writer = _handle_resume(
                    parts[1].strip(),
                    conversation_history=conversation_history,
                    cfg=cfg,
                    audit_logger=audit_logger,
                    current_session_id=session_id,
                    primary_model=primary_model,
                )
            except (SessionError, UnsupportedSessionVersion) as e:
                print(f"(resume failed: {e})")
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
                max_turns=cfg.repl.max_turns_per_query,
                primary_model=primary_model,
                fallback_model=fallback_model,
                on_api_response=print_cache_stats,
                audit_logger=audit_logger,
                session_id=session_id,
            )
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:
            print(f"\n[unexpected error] {type(e).__name__}: {e}")
            print("(history kept; you can /reset to start over)")
            continue

        # 6. Handle result + contract P-bis commit
        prior = conversation_history
        if result.status == "completed":
            _commit_to_session(writer, prior, tuple(result.messages))
            conversation_history = result.messages
            print(f"\n{extract_final_text(result.messages)}\n")
        elif result.status == "max_turns":
            _commit_to_session(writer, prior, tuple(result.messages))
            conversation_history = result.messages
            print(f"\n[reached max turns: {cfg.repl.max_turns_per_query}]")
            print("(partial result above, /reset to start over)\n")
        elif result.status == "prompt_too_long":
            _commit_to_session(writer, prior, tuple(result.messages))
            conversation_history = result.messages
            print(f"\n[context overflow] {result.reason}")
            print("(history preserved; /reset to clear, or try a shorter question)\n")
        elif result.status == "model_error":
            # Do NOT commit or update history — contract P-bis: model_error
            # is a non-commit status. Next retry will re-issue the same
            # prompt.
            print(f"\n[model error] {result.reason}")
            print("(history unchanged, you can retry)\n")


if __name__ == "__main__":
    repl()

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
from agent.memory import (
    add_entry as memory_add,
    enforce_limits as memory_enforce,
    forget_entry as memory_forget,
    load_memory,
    save_memory,
)
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

    # Phase 5: graceful shutdown — register atexit cleanup with a mutable
    # holder so /reset and /resume updates are reflected at exit time.
    import atexit
    import signal

    _shutdown = {"session_id": session_id, "audit_logger": audit_logger, "closed": False}

    def _cleanup():
        if _shutdown["closed"]:
            return
        _shutdown["closed"] = True
        # Phase 6: shutdown MCP child processes first
        for c in _shutdown.get("mcp_clients", []):
            try:
                c.shutdown()
            except Exception:
                pass
        audit_emit(_shutdown["audit_logger"], "session.close",
                   session_id=_shutdown["session_id"], msg="shutdown")

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda _s, _f: sys.exit(0))

    # Phase 6: Memory — load cross-session knowledge
    memory_path = Path(cfg.memory.base_dir) / "memory.md"
    memory_entries = load_memory(memory_path)
    memory_enforce(memory_entries,
                   max_entries=cfg.memory.max_entries,
                   max_total_chars=cfg.memory.max_total_chars)

    # Phase 6: MCP — discover and register external tools
    from agent.mcp import MCPServerConfig, discover_mcp_tools
    mcp_server_configs = []
    for s in cfg.mcp_servers:
        try:
            mcp_server_configs.append(MCPServerConfig(**s))
        except (TypeError, ValueError) as e:
            print(f"[mcp] warn: bad server config {s.get('name','?')}: {e}", file=sys.stderr)
    mcp_tools, mcp_clients = discover_mcp_tools(
        mcp_server_configs, audit_logger=audit_logger, session_id=session_id,
    )
    if mcp_tools:
        tools = sorted(tools + mcp_tools, key=lambda t: t.name)
        print(f"  mcp tools: {len(mcp_tools)} from {len([c for c in mcp_clients if c.available])} servers")

    # Register MCP clients in shutdown holder for cleanup
    _shutdown["mcp_clients"] = mcp_clients

    # Phase 6: Hooks — fire SessionStart
    from agent.hooks import fire_hooks, EVENT_SESSION_START
    if cfg.hooks.session_start:
        fire_hooks(EVENT_SESSION_START, cfg.hooks.session_start,
                   {"session_id": session_id},
                   timeout=cfg.hooks.timeout_seconds,
                   audit_logger=audit_logger, session_id=session_id)

    from agent.ui import banner as ui_banner, assistant_response as ui_response
    from agent.ui import status_message, error_message, subagent_header, subagent_result
    base_url_display = os.environ.get("ANTHROPIC_BASE_URL", "<official>")
    ui_banner(
        "Phase 7", base_url_display, primary_model, fallback_model,
        session_id, memory_count=len(memory_entries),
        mcp_tools=len(mcp_tools),
    )

    conversation_history: tuple = ()
    active_team: Optional[str] = None  # set by /team-create and /agent --team

    while True:
        # 1. Read input
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            _cleanup()
            print("\nbye.")
            break

        if not user_input:
            continue

        # 2. Slash commands
        if user_input == "/exit":
            _cleanup()  # handles MCP shutdown + audit session.close (single owner)
            print("bye.")
            break
        if user_input == "/reset":
            conversation_history = ()
            session_id = new_session_id()
            writer = _new_writer(session_id, cfg, primary_model)
            _shutdown["session_id"] = session_id  # keep atexit holder current
            audit_emit(audit_logger, "session.open", session_id=session_id,
                       msg="reset")
            print(f"(history cleared; new session {session_id[:8]})")
            continue
        if user_input == "/sessions":
            _handle_sessions(cfg)
            continue
        if user_input.startswith("/memory-save "):
            text = user_input[len("/memory-save "):].strip()
            if not text:
                print("usage: /memory-save <text>")
                continue
            entry = memory_add(memory_entries, text)
            memory_enforce(memory_entries,
                           max_entries=cfg.memory.max_entries,
                           max_total_chars=cfg.memory.max_total_chars)
            save_memory(memory_path, memory_entries)
            audit_emit(audit_logger, "memory.save",
                       session_id=session_id, msg=entry.id)
            print(f"(saved: {entry.id})")
            continue
        if user_input == "/memory-list":
            if not memory_entries:
                print("(no memories)")
            else:
                for e in memory_entries:
                    print(f"  {e.id}  {e.date}  {e.title[:60]}")
            continue
        if user_input.startswith("/memory-forget "):
            mid = user_input[len("/memory-forget "):].strip()
            if memory_forget(memory_entries, mid):
                save_memory(memory_path, memory_entries)
                audit_emit(audit_logger, "memory.forget",
                           session_id=session_id, msg=mid)
                print(f"(forgotten: {mid})")
            else:
                print(f"(not found: {mid})")
            continue
        if user_input.startswith("/agent "):
            from agent.subagent import run_fork_agent, run_fresh_agent
            from agent.team import Mailbox, TeamManager, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED
            agent_text = user_input[len("/agent "):].strip()
            # Parse flags
            is_fork = False
            team_name = None
            agent_name = None
            while agent_text.startswith("--"):
                if agent_text.startswith("--fork "):
                    is_fork = True
                    agent_text = agent_text[len("--fork "):].strip()
                elif agent_text.startswith("--team "):
                    agent_text = agent_text[len("--team "):].strip()
                    team_name, _, agent_text = agent_text.partition(" ")
                    agent_text = agent_text.strip()
                elif agent_text.startswith("--name "):
                    agent_text = agent_text[len("--name "):].strip()
                    agent_name, _, agent_text = agent_text.partition(" ")
                    agent_text = agent_text.strip()
                else:
                    break
            if not agent_text:
                print("usage: /agent [--fork] [--team <name> --name <agent>] <prompt>")
                continue
            # Team setup
            team_mgr = None
            mailbox = None
            mailbox_msgs = []
            if team_name and agent_name:
                team_mgr = TeamManager(Path(cfg.memory.base_dir))
                try:
                    team_mgr.register_member(team_name, agent_name)
                    team_mgr.update_status(team_name, agent_name, STATUS_RUNNING)
                except FileNotFoundError:
                    print(f"(team {team_name!r} not found; use /team-create first)")
                    continue
                active_team = team_name  # track for /send-message
                mailbox = Mailbox(team_mgr._team_dir(team_name))
                mailbox_msgs = mailbox.peek(agent_name)
            print(f"[sub-agent] mode={'fork' if is_fork else 'fresh'}"
                  f"{f', team={team_name}, name={agent_name}' if team_name else ''}"
                  f", running...")
            try:
                sub_kw = dict(
                    client=client,
                    tools=tools,
                    system_prompt=build_system_prompt(
                        cwd=os.getcwd(), os_name=platform.system(),
                        today=datetime.date.today().isoformat(),
                        memory_entries=memory_entries,
                        mailbox_messages=mailbox_msgs),
                    parent_session_id=session_id,
                    audit_logger=audit_logger,
                    retry_config=cfg.retry,
                    hooks_config=cfg.hooks,
                    max_turns=cfg.repl.max_turns_per_query,
                )
                if is_fork:
                    sub_result = run_fork_agent(
                        conversation_history, agent_text, **sub_kw)
                else:
                    sub_result = run_fresh_agent(agent_text, **sub_kw)
                print(f"\n[sub-agent] status={sub_result.status}")
                final_text = extract_final_text(sub_result.messages)
                print(f"{final_text}\n")
                # Team: ack mailbox + auto-reply + update status
                if team_mgr and mailbox and agent_name:
                    if sub_result.status == "completed":
                        mailbox.ack(agent_name, [m.id for m in mailbox_msgs])
                        team_mgr.update_status(team_name, agent_name, STATUS_COMPLETED)
                        # Auto-reply to leader
                        team_data = team_mgr.load_team(team_name)
                        leader = team_data.get("leader", "leader")
                        mailbox.send(agent_name, leader, final_text[:2000])
                        # Show leader's new messages
                        new_msgs = mailbox.peek(leader)
                        if new_msgs:
                            print(f"[inbox] {len(new_msgs)} unread message(s):")
                            for m in new_msgs:
                                print(f"  from={m.from_name}: {m.body[:100]}")
                    else:
                        team_mgr.update_status(team_name, agent_name, STATUS_FAILED)
            except KeyboardInterrupt:
                if team_mgr and agent_name:
                    team_mgr.update_status(team_name, agent_name, STATUS_FAILED)
                print("\n[sub-agent] interrupted")
            except Exception as e:
                if team_mgr and agent_name:
                    try:
                        team_mgr.update_status(team_name, agent_name, STATUS_FAILED)
                    except Exception:
                        pass
                print(f"\n[sub-agent] error: {type(e).__name__}: {e}")
            continue
        if user_input.startswith("/team-create "):
            from agent.team import TeamManager
            tn = user_input[len("/team-create "):].strip()
            if not tn:
                print("usage: /team-create <name>")
                continue
            tm = TeamManager(Path(cfg.memory.base_dir))
            tm.create_team(tn)
            active_team = tn
            audit_emit(audit_logger, "team.create", session_id=session_id, msg=tn)
            print(f"(team {tn!r} created; you are 'leader')")
            continue
        if user_input.startswith("/send-message "):
            from agent.team import Mailbox, TeamManager
            parts = user_input[len("/send-message "):].strip().split(maxsplit=1)
            if len(parts) < 2:
                print("usage: /send-message <agent-name> <message>")
                continue
            to_name, body = parts[0], parts[1]
            if not active_team:
                print("(no active team; use /team-create first)")
                continue
            team_dir = Path(cfg.memory.base_dir) / "teams" / active_team
            if not team_dir.exists():
                print(f"(team {active_team!r} directory not found)")
                continue
            mb = Mailbox(team_dir)
            mb.send("leader", to_name, body)
            print(f"(sent to {to_name} in team {active_team!r})")
            continue
        if user_input == "/inbox":
            from agent.team import Mailbox
            teams_dir = Path(cfg.memory.base_dir) / "teams"
            if not teams_dir.exists():
                print("(no team)")
                continue
            for td in sorted(teams_dir.iterdir()):
                mb = Mailbox(td)
                msgs = mb.peek("leader")
                if msgs:
                    print(f"[inbox — {td.name}]")
                    for m in msgs:
                        print(f"  from={m.from_name} ({m.ts}): {m.body[:200]}")
                    mb.ack("leader", [m.id for m in msgs])
            if not any((Path(cfg.memory.base_dir) / "teams").iterdir()):
                print("(no unread messages)")
            continue
        if user_input == "/team-status":
            from agent.team import Mailbox, TeamManager
            teams_dir = Path(cfg.memory.base_dir) / "teams"
            if not teams_dir.exists():
                print("(no teams)")
                continue
            for td in sorted(teams_dir.iterdir()):
                try:
                    tm = TeamManager(Path(cfg.memory.base_dir))
                    data = tm.load_team(td.name)
                    mb = Mailbox(td)
                    print(f"[team: {td.name}]")
                    for member in data.get("members", []):
                        unread = mb.unread_count(member["name"])
                        print(f"  {member['name']:20s} {member['status']:12s} ({unread} unread)")
                except Exception as e:
                    print(f"  error reading {td.name}: {e}")
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
                _shutdown["session_id"] = session_id  # keep atexit holder current
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
            memory_entries=memory_entries,
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
                retry_config=cfg.retry,
                hooks_config=cfg.hooks,
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
            ui_response(extract_final_text(result.messages))
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

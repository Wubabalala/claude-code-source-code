"""Slash command dispatch — extracted from main.py.

Each handler has signature ``(app: AgentApp, args: str) -> CommandResult``.
The ``dispatch`` function matches user input to the longest registered
prefix and delegates to the handler.
"""
from __future__ import annotations

import datetime
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.bootstrap import AgentApp


# ---------------------------------------------------------------------------
# CommandResult + registry
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    should_break: bool = False


COMMANDS: dict[str, Callable] = {}


def register(name: str):
    """Decorator to register a slash command handler."""
    def decorator(fn):
        COMMANDS[name] = fn
        return fn
    return decorator


def dispatch(user_input: str, app: AgentApp) -> CommandResult | None:
    """Match *user_input* against registered commands (longest prefix first).

    Returns a ``CommandResult`` if a command matched, or ``None`` if the
    input should be handled as a regular query.
    """
    for prefix in sorted(COMMANDS, key=len, reverse=True):
        if user_input == prefix or user_input.startswith(prefix + " "):
            args = user_input[len(prefix):].strip()
            return COMMANDS[prefix](app, args)
    return None


# ---------------------------------------------------------------------------
# Helpers (moved from main.py)
# ---------------------------------------------------------------------------


def _extract_final_text(messages: tuple) -> str:
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


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


@register("/exit")
def cmd_exit(app: AgentApp, args: str) -> CommandResult:
    print("bye.")
    return CommandResult(should_break=True)


@register("/reset")
def cmd_reset(app: AgentApp, args: str) -> CommandResult:
    sid = app.reset_session()
    print(f"(history cleared; new session {sid[:8]})")
    return CommandResult()


@register("/sessions")
def cmd_sessions(app: AgentApp, args: str) -> CommandResult:
    from agent.session import list_sessions
    base = Path(app.cfg.session.base_dir).expanduser()
    summaries = list_sessions(base, limit=20)
    if not summaries:
        print("(no sessions)")
    else:
        for s in summaries:
            mtime = datetime.datetime.fromtimestamp(s.last_updated).isoformat(
                timespec="seconds")
            print(f"  {s.session_id[:8]}  msgs={s.message_count:3d}  {mtime}")
    return CommandResult()


@register("/memory-save")
def cmd_memory_save(app: AgentApp, args: str) -> CommandResult:
    if not args:
        print("usage: /memory-save <text>")
        return CommandResult()
    from agent.memory import add_entry, enforce_limits, save_memory
    from agent.audit import emit as audit_emit
    entry = add_entry(app.memory_entries, args)
    enforce_limits(app.memory_entries,
                   max_entries=app.cfg.memory.max_entries,
                   max_total_chars=app.cfg.memory.max_total_chars)
    save_memory(app.memory_path, app.memory_entries)
    audit_emit(app.audit_logger, "memory.save",
               session_id=app.session_id, msg=entry.id)
    print(f"(saved: {entry.id})")
    return CommandResult()


@register("/memory-list")
def cmd_memory_list(app: AgentApp, args: str) -> CommandResult:
    if not app.memory_entries:
        print("(no memories)")
    else:
        for e in app.memory_entries:
            print(f"  {e.id}  {e.date}  {e.title[:60]}")
    return CommandResult()


@register("/memory-forget")
def cmd_memory_forget(app: AgentApp, args: str) -> CommandResult:
    if not args:
        print("usage: /memory-forget <id>")
        return CommandResult()
    from agent.memory import forget_entry, save_memory
    from agent.audit import emit as audit_emit
    if forget_entry(app.memory_entries, args):
        save_memory(app.memory_path, app.memory_entries)
        audit_emit(app.audit_logger, "memory.forget",
                   session_id=app.session_id, msg=args)
        print(f"(forgotten: {args})")
    else:
        print(f"(not found: {args})")
    return CommandResult()


@register("/agent")
def cmd_agent(app: AgentApp, args: str) -> CommandResult:
    from agent.subagent import run_fork_agent, run_fresh_agent
    from agent.team import Mailbox, TeamManager, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED
    from agent.prompt import build_system_prompt

    agent_text = args
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
        return CommandResult()

    team_mgr = None
    mailbox = None
    mailbox_msgs = []
    if team_name and agent_name:
        team_mgr = TeamManager(Path(app.cfg.memory.base_dir))
        try:
            team_mgr.register_member(team_name, agent_name)
            team_mgr.update_status(team_name, agent_name, STATUS_RUNNING)
        except FileNotFoundError:
            print(f"(team {team_name!r} not found; use /team-create first)")
            return CommandResult()
        app.active_team = team_name
        mailbox = Mailbox(team_mgr._team_dir(team_name))
        mailbox_msgs = mailbox.peek(agent_name)

    print(f"[sub-agent] mode={'fork' if is_fork else 'fresh'}"
          f"{f', team={team_name}, name={agent_name}' if team_name else ''}"
          f", running...")

    try:
        sub_kw = dict(
            client=app.client,
            tools=app.tools,
            system_prompt=build_system_prompt(
                cwd=os.getcwd(), os_name=platform.system(),
                today=datetime.date.today().isoformat(),
                memory_entries=app.memory_entries,
                mailbox_messages=mailbox_msgs),
            parent_session_id=app.session_id,
            audit_logger=app.audit_logger,
            retry_config=app.cfg.retry,
            hooks_config=app.cfg.hooks,
            max_turns=app.cfg.repl.max_turns_per_query,
        )
        if is_fork:
            sub_result = run_fork_agent(
                app.conversation_history, agent_text, **sub_kw)
        else:
            sub_result = run_fresh_agent(agent_text, **sub_kw)
        print(f"\n[sub-agent] status={sub_result.status}")
        final_text = _extract_final_text(sub_result.messages)
        print(f"{final_text}\n")

        if team_mgr and mailbox and agent_name:
            if sub_result.status == "completed":
                mailbox.ack(agent_name, [m.id for m in mailbox_msgs])
                team_mgr.update_status(team_name, agent_name, STATUS_COMPLETED)
                team_data = team_mgr.load_team(team_name)
                leader = team_data.get("leader", "leader")
                mailbox.send(agent_name, leader, final_text[:2000])
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
    return CommandResult()


@register("/team-create")
def cmd_team_create(app: AgentApp, args: str) -> CommandResult:
    if not args:
        print("usage: /team-create <name>")
        return CommandResult()
    from agent.team import TeamManager
    from agent.audit import emit as audit_emit
    tm = TeamManager(Path(app.cfg.memory.base_dir))
    tm.create_team(args)
    app.active_team = args
    audit_emit(app.audit_logger, "team.create", session_id=app.session_id, msg=args)
    print(f"(team {args!r} created; you are 'leader')")
    return CommandResult()


@register("/send-message")
def cmd_send_message(app: AgentApp, args: str) -> CommandResult:
    from agent.team import Mailbox
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        print("usage: /send-message <agent-name> <message>")
        return CommandResult()
    to_name, body = parts[0], parts[1]
    if not app.active_team:
        print("(no active team; use /team-create first)")
        return CommandResult()
    team_dir = Path(app.cfg.memory.base_dir) / "teams" / app.active_team
    if not team_dir.exists():
        print(f"(team {app.active_team!r} directory not found)")
        return CommandResult()
    mb = Mailbox(team_dir)
    mb.send("leader", to_name, body)
    print(f"(sent to {to_name} in team {app.active_team!r})")
    return CommandResult()


@register("/inbox")
def cmd_inbox(app: AgentApp, args: str) -> CommandResult:
    from agent.team import Mailbox
    teams_dir = Path(app.cfg.memory.base_dir) / "teams"
    if not teams_dir.exists():
        print("(no team)")
        return CommandResult()
    found_any = False
    for td in sorted(teams_dir.iterdir()):
        mb = Mailbox(td)
        msgs = mb.peek("leader")
        if msgs:
            found_any = True
            print(f"[inbox — {td.name}]")
            for m in msgs:
                print(f"  from={m.from_name} ({m.ts}): {m.body[:200]}")
            mb.ack("leader", [m.id for m in msgs])
    if not found_any:
        print("(no unread messages)")
    return CommandResult()


@register("/team-status")
def cmd_team_status(app: AgentApp, args: str) -> CommandResult:
    from agent.team import Mailbox, TeamManager
    teams_dir = Path(app.cfg.memory.base_dir) / "teams"
    if not teams_dir.exists():
        print("(no teams)")
        return CommandResult()
    for td in sorted(teams_dir.iterdir()):
        try:
            tm = TeamManager(Path(app.cfg.memory.base_dir))
            data = tm.load_team(td.name)
            mb = Mailbox(td)
            print(f"[team: {td.name}]")
            for member in data.get("members", []):
                unread = mb.unread_count(member["name"])
                print(f"  {member['name']:20s} {member['status']:12s} ({unread} unread)")
        except Exception as e:
            print(f"  error reading {td.name}: {e}")
    return CommandResult()


@register("/resume")
def cmd_resume(app: AgentApp, args: str) -> CommandResult:
    if not args:
        print("usage: /resume <session-id-prefix>")
        return CommandResult()
    from agent.session import SessionError, UnsupportedSessionVersion
    try:
        sid, n_msgs, last_iso, truncated = app.resume_session(args)
        print(f"resumed {sid} ({n_msgs} messages, last updated {last_iso})")
        if truncated:
            print(f"  (recovered with truncated tail: dropped {truncated} corrupt lines)")
    except (SessionError, UnsupportedSessionVersion) as e:
        print(f"(resume failed: {e})")
    return CommandResult()

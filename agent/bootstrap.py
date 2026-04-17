"""AgentApp — unified application state for CLI and web.

Extracts shared initialisation from main.py and web.py into a single
factory (``AgentApp.create``). Behaviour methods (``reset_session``,
``resume_session``, ``commit_result``, ``cleanup``) converge state
mutations so that command handlers don't reach into fields directly.
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class AgentApp:
    cfg: Any                          # AgentConfig
    client: Any                       # Anthropic
    tools: list
    memory_entries: list
    memory_path: Path
    audit_logger: Any
    session_id: str
    writer: Any                       # SessionWriter | None (web mode)
    mcp_clients: list
    primary_model: str
    fallback_model: str
    active_team: Optional[str] = None
    conversation_history: tuple = ()
    _cleaned_up: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------ #
    # Factory
    # ------------------------------------------------------------------ #

    @classmethod
    def create(cls, *, web_mode: bool = False) -> AgentApp:
        """Shared bootstrap for CLI REPL and Gradio web UI."""
        from dotenv import load_dotenv
        load_dotenv()

        from agent.client import init_client, get_tools
        from agent.config import load_config
        from agent.compact import configure_compact
        from agent.permissions import configure_permissions
        from agent.audit import get_audit_logger
        from agent.memory import load_memory, enforce_limits as memory_enforce
        from agent.session import new_session_id, SessionWriter

        cfg = load_config()
        configure_compact(cfg.compact)

        if web_mode:
            from dataclasses import replace as dc_replace
            configure_permissions(dc_replace(cfg.permissions, non_tty_default="ALLOW"))
        else:
            configure_permissions(cfg.permissions)

        client = init_client()
        tools = get_tools()

        primary_model = cfg.repl.primary_model
        fallback_model = cfg.repl.fallback_model

        audit_logger = get_audit_logger(
            audit_file=Path(cfg.logging.audit_file).expanduser(),
            level=cfg.logging.level,
            max_bytes=cfg.logging.max_bytes,
            backup_count=cfg.logging.backup_count,
        )

        session_id = new_session_id()

        memory_path = Path(cfg.memory.base_dir) / "memory.md"
        memory_entries = load_memory(memory_path)
        memory_enforce(memory_entries,
                       max_entries=cfg.memory.max_entries,
                       max_total_chars=cfg.memory.max_total_chars)

        # MCP
        mcp_clients: list = []
        mcp_tools: list = []
        if not web_mode:
            from agent.mcp import MCPServerConfig, discover_mcp_tools
            mcp_server_configs = []
            for s in cfg.mcp_servers:
                try:
                    mcp_server_configs.append(MCPServerConfig(**s))
                except (TypeError, ValueError) as e:
                    import sys
                    print(f"[mcp] warn: bad server config {s.get('name','?')}: {e}",
                          file=sys.stderr)
            mcp_tools, mcp_clients = discover_mcp_tools(
                mcp_server_configs, audit_logger=audit_logger, session_id=session_id,
            )
            if mcp_tools:
                tools = sorted(tools + mcp_tools, key=lambda t: t.name)

        # Writer (CLI only; web mode doesn't persist sessions)
        writer = None
        if not web_mode:
            writer = SessionWriter(
                session_id,
                base_dir=Path(cfg.session.base_dir).expanduser(),
                cwd=os.getcwd(),
                model=primary_model,
            )

        return cls(
            cfg=cfg,
            client=client,
            tools=tools,
            memory_entries=memory_entries,
            memory_path=memory_path,
            audit_logger=audit_logger,
            session_id=session_id,
            writer=writer,
            mcp_clients=mcp_clients,
            primary_model=primary_model,
            fallback_model=fallback_model,
        )

    # ------------------------------------------------------------------ #
    # Behaviour methods (converge state mutations)
    # ------------------------------------------------------------------ #

    def reset_session(self) -> str:
        """Clear history, create new session. Returns new session_id."""
        from agent.session import new_session_id
        from agent.audit import emit as audit_emit
        # Close the old session before switching
        audit_emit(self.audit_logger, "session.close",
                   session_id=self.session_id, msg="reset")
        self.conversation_history = ()
        self.session_id = new_session_id()
        self.writer = self._new_writer()
        self.active_team = None
        self._cleaned_up = False  # new session → cleanup allowed again
        audit_emit(self.audit_logger, "session.open",
                   session_id=self.session_id, msg="reset")
        return self.session_id

    def resume_session(self, prefix: str):
        """Resume a previous session.

        Returns ``(session_id, n_msgs, last_iso, truncated_lines)``.
        Raises ``SessionError`` on failure.
        """
        from agent.session import (
            SessionError,
            load_session,
            resolve_prefix,
            truncate_corrupt_tail,
        )
        from agent.audit import emit as audit_emit

        if not self.cfg.session.resume_enabled:
            raise SessionError("session resume is disabled in config")
        if self.conversation_history:
            n = len(self.conversation_history)
            raise SessionError(
                f"session already has {n} messages; "
                f"use /exit and relaunch to resume"
            )

        base = Path(self.cfg.session.base_dir).expanduser()
        from agent.session import AmbiguousPrefixError, NoSuchSessionError
        try:
            sid = resolve_prefix(base, prefix)
        except AmbiguousPrefixError as e:
            cands = ", ".join(s[:8] for s in e.candidates)
            raise SessionError(f"prefix {prefix!r} is ambiguous; candidates: {cands}")
        except NoSuchSessionError:
            raise SessionError(f"no session matching prefix {prefix!r}")

        path = base / f"{sid}.jsonl"
        result = load_session(path)

        if result.truncated_tail_lines > 0:
            truncate_corrupt_tail(path)

        # Close the old session before switching
        audit_emit(self.audit_logger, "session.close",
                   session_id=self.session_id, msg="resume")
        audit_emit(self.audit_logger, "session.resume",
                   session_id=sid, msg=f"resumed from {prefix!r}")
        if result.truncated_tail_lines:
            audit_emit(self.audit_logger, "session.recover.truncate",
                       session_id=sid,
                       msg=f"dropped {result.truncated_tail_lines} corrupt tail lines")

        self.session_id = sid
        self.conversation_history = tuple(result.messages)
        self.writer = self._new_writer()
        self.active_team = None
        self._cleaned_up = False

        last_iso = datetime.datetime.fromtimestamp(result.last_updated).isoformat(
            timespec="seconds")
        return sid, len(self.conversation_history), last_iso, result.truncated_tail_lines

    def commit_result(self, prior: tuple, new: tuple) -> None:
        """Contract P-bis: append delta or write snapshot."""
        if self.writer is None:
            return
        if len(new) >= len(prior) and tuple(new[:len(prior)]) == tuple(prior):
            for msg in new[len(prior):]:
                self.writer.append_message(msg)
        else:
            self.writer.append_snapshot(new)

    def cleanup(self) -> None:
        """Idempotent shutdown: MCP clients + audit session.close.

        Safe to call from both the explicit /exit path and from atexit.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for c in self.mcp_clients:
            try:
                c.shutdown()
            except Exception:
                pass
        from agent.audit import emit as audit_emit
        audit_emit(self.audit_logger, "session.close",
                   session_id=self.session_id, msg="shutdown")

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _new_writer(self):
        from agent.session import SessionWriter
        return SessionWriter(
            self.session_id,
            base_dir=Path(self.cfg.session.base_dir).expanduser(),
            cwd=os.getcwd(),
            model=self.primary_model,
        )

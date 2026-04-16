"""Gradio Web UI — Phase 7 chat interface.

Wraps the agent loop in a Gradio ChatInterface so users can interact
via browser at http://localhost:7860.

Usage:
    python -m agent.web
"""
from __future__ import annotations

import datetime
import os
import platform
from pathlib import Path
from typing import Generator

from dotenv import load_dotenv

load_dotenv()

import gradio as gr
from anthropic import Anthropic

from agent.audit import get_audit_logger
from agent.compact import configure_compact
from agent.config import load_config
from agent.loop import run_agent_loop
from agent.memory import enforce_limits, load_memory
from agent.prompt import build_system_prompt
from agent.session import new_session_id
from agent.tools import BashTool, EditFileTool, GrepTool, ReadFileTool, Tool, WriteFileTool


def _init():
    """One-time setup: config, client, tools, memory."""
    cfg = load_config()
    configure_compact(cfg.compact)

    from agent.permissions import configure_permissions
    from dataclasses import replace as dc_replace
    # Web mode: auto-ALLOW ASK tools (no stdin for Y/N prompt).
    # Hard-deny list (Phase 3) still blocks dangerous paths regardless.
    web_perms = dc_replace(cfg.permissions, non_tty_default="ALLOW")
    configure_permissions(web_perms)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY in .env")
    client_kw: dict = {"api_key": api_key}
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        client_kw["base_url"] = base_url
    client = Anthropic(**client_kw)

    tools: list[Tool] = sorted(
        [BashTool(), EditFileTool(), GrepTool(), ReadFileTool(), WriteFileTool()],
        key=lambda t: t.name,
    )

    memory_path = Path(cfg.memory.base_dir) / "memory.md"
    memory_entries = load_memory(memory_path)
    enforce_limits(memory_entries,
                   max_entries=cfg.memory.max_entries,
                   max_total_chars=cfg.memory.max_total_chars)

    audit_logger = get_audit_logger(
        audit_file=Path(cfg.logging.audit_file).expanduser(),
        level=cfg.logging.level,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
    )

    session_id = new_session_id()

    return {
        "cfg": cfg,
        "client": client,
        "tools": tools,
        "memory_entries": memory_entries,
        "audit_logger": audit_logger,
        "session_id": session_id,
    }


_state = _init()


def _extract_text(messages: tuple) -> str:
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            parts = [b["text"] for b in msg["content"] if b.get("type") == "text"]
            if parts:
                return "\n".join(parts)
    return "(no response)"


def chat(user_message: str, history: list[list]) -> str:
    """Gradio chat callback — runs one agent query and returns the response.

    Gradio 6.x ChatInterface passes history as list of {"role":..., "content":...}
    dicts (OpenAI-style messages).
    """
    cfg = _state["cfg"]

    # Build conversation_history from Gradio history
    conversation: list[dict] = []
    for entry in history:
        role = entry.get("role", "user") if isinstance(entry, dict) else "user"
        content = entry.get("content", "") if isinstance(entry, dict) else str(entry)
        conversation.append({
            "role": role,
            "content": [{"type": "text", "text": content}],
        })

    # Add current user message
    conversation.append({
        "role": "user",
        "content": [{"type": "text", "text": user_message}],
    })

    system_prompt = build_system_prompt(
        cwd=os.getcwd(),
        os_name=platform.system(),
        today=datetime.date.today().isoformat(),
        memory_entries=_state["memory_entries"],
    )

    try:
        result = run_agent_loop(
            client=_state["client"],
            initial_messages=tuple(conversation),
            tools=_state["tools"],
            system_prompt=system_prompt,
            max_turns=cfg.repl.max_turns_per_query,
            primary_model=cfg.repl.primary_model,
            fallback_model=cfg.repl.fallback_model,
            audit_logger=_state["audit_logger"],
            session_id=_state["session_id"],
            retry_config=cfg.retry,
            hooks_config=cfg.hooks,
        )
    except Exception as e:
        return f"**Error**: {type(e).__name__}: {e}"

    if result.status == "model_error":
        return f"**Model Error**: {result.reason}"
    if result.status == "prompt_too_long":
        return f"**Context Overflow**: {result.reason}"

    return _extract_text(result.messages)


def main():
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "<official>")
    model = _state["cfg"].repl.primary_model

    demo = gr.ChatInterface(
        fn=chat,
        title="Code Repo Assistant",
        description=(
            f"Model: **{model}** via `{base_url}`  \n"
            f"Session: `{_state['session_id'][:8]}`  |  "
            f"Memory: {len(_state['memory_entries'])} entries  |  "
            f"Tools: {', '.join(t.name for t in _state['tools'])}"
        ),
        examples=[
            "What files are in the agent/ directory?",
            "Search for all TODO comments in the codebase",
            "Read agent/loop.py and explain the retry mechanism",
        ],
    )
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False)


if __name__ == "__main__":
    main()

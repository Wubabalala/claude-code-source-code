"""Gradio Web UI — browser-based chat interface.

Wraps the agent loop in a Gradio ChatInterface at http://localhost:7860.

Usage:
    python -m agent.web
"""
from __future__ import annotations

import datetime
import os
import platform
from typing import Generator

import gradio as gr

from agent.bootstrap import AgentApp
from agent.loop import run_agent_loop
from agent.main import extract_final_text
from agent.prompt import build_system_prompt


_app = AgentApp.create(web_mode=True)


def chat(user_message: str, history: list[list]) -> str:
    """Gradio chat callback — runs one agent query and returns the response.

    Gradio 6.x ChatInterface passes history as list of {"role":..., "content":...}
    dicts (OpenAI-style messages).
    """
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
        memory_entries=_app.memory_entries,
    )

    try:
        result = run_agent_loop(
            client=_app.client,
            initial_messages=tuple(conversation),
            tools=_app.tools,
            system_prompt=system_prompt,
            max_turns=_app.cfg.repl.max_turns_per_query,
            primary_model=_app.primary_model,
            fallback_model=_app.fallback_model,
            audit_logger=_app.audit_logger,
            session_id=_app.session_id,
            retry_config=_app.cfg.retry,
            hooks_config=_app.cfg.hooks,
        )
    except Exception as e:
        return f"**Error**: {type(e).__name__}: {e}"

    if result.status == "model_error":
        return f"**Model Error**: {result.reason}"
    if result.status == "prompt_too_long":
        return f"**Context Overflow**: {result.reason}"

    return extract_final_text(result.messages)


def main():
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "<official>")
    model = _app.primary_model

    demo = gr.ChatInterface(
        fn=chat,
        title="Code Repo Assistant",
        description=(
            f"Model: **{model}** via `{base_url}`  \n"
            f"Session: `{_app.session_id[:8]}`  |  "
            f"Memory: {len(_app.memory_entries)} entries  |  "
            f"Tools: {', '.join(t.name for t in _app.tools)}"
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

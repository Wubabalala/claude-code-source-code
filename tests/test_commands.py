"""Tests for agent.commands — slash command dispatch + handlers."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.commands import CommandResult, dispatch, COMMANDS


# ---------------------------------------------------------------------------
# Minimal AgentApp stand-in for command tests
# ---------------------------------------------------------------------------


class _Ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeApp:
    """Lightweight stand-in for AgentApp — only the fields commands need."""
    def __init__(self, tmp_path):
        self.cfg = _Ns(
            session=_Ns(base_dir=str(tmp_path / "sessions"), resume_enabled=True),
            memory=_Ns(
                base_dir=str(tmp_path),
                max_entries=100,
                max_total_chars=50_000,
            ),
            retry=None,
            hooks=_Ns(session_start=[], pre_tool_use=[], post_tool_use=[], timeout_seconds=5),
            repl=_Ns(max_turns_per_query=10, primary_model="test", fallback_model="test-fb"),
        )
        self.client = object()
        self.tools = []
        self.memory_entries = []
        self.memory_path = tmp_path / "memory.md"
        self.audit_logger = None
        self.session_id = "fake-session"
        self.writer = None
        self.mcp_clients = []
        self.primary_model = "test"
        self.fallback_model = "test-fb"
        self.active_team = None
        self.conversation_history = ()
        self._cleaned_up = False

    def reset_session(self):
        self.session_id = "new-session"
        self.conversation_history = ()
        self.active_team = None
        return self.session_id

    def cleanup(self):
        self._cleaned_up = True

    def commit_result(self, prior, new):
        pass


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_dispatch_returns_none_for_unknown_command(tmp_path):
    app = _FakeApp(tmp_path)
    assert dispatch("/nonexistent", app) is None


def test_dispatch_returns_none_for_plain_text(tmp_path):
    app = _FakeApp(tmp_path)
    assert dispatch("hello world", app) is None


def test_dispatch_matches_exact_command(tmp_path):
    app = _FakeApp(tmp_path)
    result = dispatch("/memory-list", app)
    assert result is not None
    assert isinstance(result, CommandResult)


def test_dispatch_matches_command_with_args(tmp_path):
    app = _FakeApp(tmp_path)
    result = dispatch("/memory-save some text", app)
    assert result is not None


def test_dispatch_longest_prefix_wins(tmp_path):
    """'/memory-save' should match before '/memory' (if it existed)."""
    app = _FakeApp(tmp_path)
    # /memory-save is registered and should match, not fall through
    result = dispatch("/memory-save test data", app)
    assert result is not None


# ---------------------------------------------------------------------------
# /exit
# ---------------------------------------------------------------------------


def test_exit_returns_should_break(tmp_path, capsys):
    app = _FakeApp(tmp_path)
    result = dispatch("/exit", app)
    assert result.should_break is True
    assert "bye" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------


def test_reset_calls_app_reset_session(tmp_path, capsys):
    app = _FakeApp(tmp_path)
    result = dispatch("/reset", app)
    assert result.should_break is False
    assert app.session_id == "new-session"
    assert "history cleared" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /memory-list
# ---------------------------------------------------------------------------


def test_memory_list_empty(tmp_path, capsys):
    app = _FakeApp(tmp_path)
    result = dispatch("/memory-list", app)
    assert result.should_break is False
    assert "no memories" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /sessions
# ---------------------------------------------------------------------------


def test_sessions_empty(tmp_path, capsys):
    app = _FakeApp(tmp_path)
    result = dispatch("/sessions", app)
    assert result.should_break is False
    assert "no sessions" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /team-status with no teams
# ---------------------------------------------------------------------------


def test_team_status_no_teams(tmp_path, capsys):
    app = _FakeApp(tmp_path)
    result = dispatch("/team-status", app)
    assert result.should_break is False
    assert "no teams" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /inbox with no teams
# ---------------------------------------------------------------------------


def test_inbox_no_teams(tmp_path, capsys):
    app = _FakeApp(tmp_path)
    result = dispatch("/inbox", app)
    assert result.should_break is False
    assert "no team" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# All 12 commands are registered
# ---------------------------------------------------------------------------


def test_all_12_commands_registered():
    expected = {
        "/exit", "/reset", "/sessions",
        "/memory-save", "/memory-list", "/memory-forget",
        "/agent", "/team-create", "/send-message", "/inbox", "/team-status",
        "/resume",
    }
    assert set(COMMANDS.keys()) == expected

"""Tests for agent.bootstrap — AgentApp behaviour methods."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Minimal AgentApp construction (bypass create() which needs full env)
# ---------------------------------------------------------------------------


def _make_app(tmp_path, *, audit_lines=None):
    """Build an AgentApp with minimal fakes for unit testing."""
    from agent.bootstrap import AgentApp
    from agent.session import SessionWriter

    session_dir = tmp_path / "sessions"
    log_file = tmp_path / "audit.jsonl"

    from agent.audit import get_audit_logger, reset_audit_logger
    reset_audit_logger()
    logger = get_audit_logger(
        audit_file=log_file, level="DEBUG", max_bytes=1_000_000, backup_count=0,
    )

    sid = "aaaa-1111"
    writer = SessionWriter(sid, base_dir=session_dir, cwd=str(tmp_path), model="test")

    app = AgentApp(
        cfg=_FakeCfg(session_dir),
        client=object(),
        tools=[],
        memory_entries=[],
        memory_path=tmp_path / "memory.md",
        audit_logger=logger,
        session_id=sid,
        writer=writer,
        mcp_clients=[],
        primary_model="test-primary",
        fallback_model="test-fallback",
    )
    return app, log_file, logger


class _FakeCfg:
    """Minimal config stand-in."""
    def __init__(self, session_dir):
        self.session = _Ns(base_dir=str(session_dir), resume_enabled=True)
        self.memory = _Ns(base_dir=str(session_dir.parent), max_entries=100, max_total_chars=50_000)
        self.logging = _Ns(audit_file="audit.jsonl", level="DEBUG", max_bytes=1_000_000, backup_count=0)

class _Ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _audit_events(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# reset_session
# ---------------------------------------------------------------------------


def test_reset_session_clears_history(tmp_path):
    app, log_file, _ = _make_app(tmp_path)
    app.conversation_history = ({"role": "user", "content": "old"},)
    app.active_team = "my-team"

    new_sid = app.reset_session()

    assert app.conversation_history == ()
    assert app.active_team is None
    assert app.session_id == new_sid
    assert new_sid != "aaaa-1111"


def test_reset_session_emits_close_then_open(tmp_path):
    app, log_file, _ = _make_app(tmp_path)
    old_sid = app.session_id

    app.reset_session()

    events = _audit_events(log_file)
    event_types = [(e["event"], e.get("session_id")) for e in events]
    # Must see close for old session, then open for new session
    assert ("session.close", old_sid) in event_types
    open_events = [(ev, sid) for ev, sid in event_types if ev == "session.open"]
    assert len(open_events) == 1
    assert open_events[0][1] == app.session_id


# ---------------------------------------------------------------------------
# commit_result
# ---------------------------------------------------------------------------


def test_commit_result_appends_delta(tmp_path):
    app, _, _ = _make_app(tmp_path)
    msg1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    msg2 = {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}

    # Force writer to create the file
    app.writer.append_message(msg1)
    prior = (msg1,)
    new = (msg1, msg2)

    app.commit_result(prior, new)
    # No exception = success (delta path)


def test_commit_result_noop_when_no_writer(tmp_path):
    app, _, _ = _make_app(tmp_path)
    app.writer = None
    # Should not raise even though writer is None
    app.commit_result((), ({"role": "user", "content": "x"},))


# ---------------------------------------------------------------------------
# cleanup idempotence
# ---------------------------------------------------------------------------


def test_cleanup_is_idempotent(tmp_path):
    app, log_file, _ = _make_app(tmp_path)
    app.cleanup()
    app.cleanup()  # second call should be no-op

    events = _audit_events(log_file)
    close_events = [e for e in events if e["event"] == "session.close"]
    assert len(close_events) == 1


def test_cleanup_shuts_down_mcp_clients(tmp_path):
    class FakeMCP:
        shut = False
        def shutdown(self):
            self.shut = True

    app, _, _ = _make_app(tmp_path)
    mcp = FakeMCP()
    app.mcp_clients = [mcp]
    app.cleanup()
    assert mcp.shut is True

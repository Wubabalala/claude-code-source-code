"""REPL-level integration tests for Phase 4.

Covers contracts P / P-bis / L / R end-to-end as they land in agent/main.py:
  - Session file lazy creation
  - Commit only on completed/max_turns/prompt_too_long (contract P-bis)
  - model_error does NOT pollute the JSONL
  - History-rewrite triggers a `snapshot` event (contract P-bis snapshot)
  - /resume valid only when conversation is empty (contract R window)
  - /resume failure keeps the new session intact
  - /sessions lists recent sessions
  - Resume respects snapshot reset

Strategy:
  - monkeypatch `agent.main.run_agent_loop` with a queue of canned
    AgentResults so we can drive REPL commit behaviour deterministically
  - monkeypatch `builtins.input` with an iterator of slash-commands and
    prompts
  - redirect HOME / config dirs via `AGENT_CONFIG_PATH` + tmp dirs so the
    test run never touches the user's real ~/.agent
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.types import AgentResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_agent_home(tmp_path, monkeypatch):
    """Point all Phase 4 paths at an ephemeral tmp dir.

    Writes a TOML that redirects session and log dirs into tmp so the test
    run leaves no trace in ~/.agent.
    """
    session_dir = tmp_path / "sessions"
    log_dir = tmp_path / "logs"
    toml = tmp_path / "config.toml"
    toml.write_text(
        f'[session]\n'
        f'base_dir = {str(session_dir)!r}\n'
        f'[logging]\n'
        f'audit_file = {str(log_dir / "audit.jsonl")!r}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(toml))
    # Real Anthropic() can't init without a key — stub init_client
    monkeypatch.setattr("agent.main.init_client", lambda: object())
    # Reset audit logger so each test gets a fresh handler
    from agent import audit as audit_mod
    audit_mod.reset_audit_logger()
    yield {
        "home": tmp_path,
        "sessions_dir": session_dir,
        "log_dir": log_dir,
    }
    audit_mod.reset_audit_logger()


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_msg(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _drive_repl(monkeypatch, lines, fake_loop_results):
    """Run `repl()` with given input lines and canned AgentResult queue."""
    from agent import main as main_mod

    it_input = iter(lines)
    monkeypatch.setattr("builtins.input", lambda _="": next(it_input))

    it_results = iter(fake_loop_results)

    def fake_loop(**kwargs):
        return next(it_results)

    monkeypatch.setattr(main_mod, "run_agent_loop", fake_loop)
    main_mod.repl()


def _find_session_file(sessions_dir: Path) -> Path:
    files = list(sessions_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected 1 session file, got {files}"
    return files[0]


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _messages_of(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "message"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_repl_empty_session_never_creates_file(isolated_agent_home, monkeypatch):
    """Contract R: starting REPL and exiting immediately must leave no
    JSONL file behind (lazy file creation)."""
    _drive_repl(monkeypatch, lines=["/exit"], fake_loop_results=[])
    assert list(isolated_agent_home["sessions_dir"].glob("*.jsonl")) == []


def test_repl_commits_messages_only_on_completed_status(isolated_agent_home, monkeypatch):
    """Contract P-bis: model_error path does NOT write message events."""
    init_user = _user_msg("hello")
    result = AgentResult(
        status="model_error",
        messages=(init_user,),  # would-be post-query state, but not committed
        reason="simulated model failure",
    )
    _drive_repl(
        monkeypatch,
        lines=["hello", "/exit"],
        fake_loop_results=[result],
    )
    # Either no file at all (no commit ever happened) or file has only
    # session_meta. In our current impl the writer stays lazy until commit,
    # so no file should exist.
    files = list(isolated_agent_home["sessions_dir"].glob("*.jsonl"))
    assert files == [], f"model_error must not commit; found {files}"


def test_repl_model_error_does_not_pollute_transcript(isolated_agent_home, monkeypatch):
    """Sequence: completed → model_error → completed.
    Expect JSONL to contain only the messages from the two completed
    queries, nothing from the model_error query."""
    q1_completed = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    q2_error = AgentResult(
        status="model_error",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="boom",
    )
    q3_completed = AgentResult(
        status="completed",
        messages=(
            _user_msg("q1"), _assistant_msg("a1"),
            _user_msg("q3"), _assistant_msg("a3"),
        ),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["q1", "q2", "q3", "/exit"],
        fake_loop_results=[q1_completed, q2_error, q3_completed],
    )
    path = _find_session_file(isolated_agent_home["sessions_dir"])
    events = _read_events(path)
    messages = _messages_of(events)
    # Exactly 4 messages: q1+a1+q3+a3 (q2 error is NOT committed)
    assert len(messages) == 4
    texts = [m["payload"]["content"][0]["text"] for m in messages]
    assert texts == ["q1", "a1", "q3", "a3"]
    # And no snapshot events (each commit was a prefix extension)
    assert not [e for e in events if e["type"] == "snapshot"]


def test_repl_commits_snapshot_when_history_rewritten(isolated_agent_home, monkeypatch):
    """Contract P-bis snapshot: when result.messages is NOT a prefix
    extension of the prior conversation_history, a single `snapshot` event
    is written instead of delta `message` events."""
    q1 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    # q2 simulates a compaction: prior = (q1, a1, q2), new = (SUMMARY, a2)
    # prior[:len(new)] != new, and len(new) < len(prior) as well. main.py
    # will write a snapshot carrying the full new tuple.
    q2_rewritten = AgentResult(
        status="completed",
        messages=(
            _user_msg("<session_summary>s</session_summary>"),
            _assistant_msg("a2"),
        ),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["q1", "q2", "/exit"],
        fake_loop_results=[q1, q2_rewritten],
    )
    path = _find_session_file(isolated_agent_home["sessions_dir"])
    events = _read_events(path)
    snapshots = [e for e in events if e["type"] == "snapshot"]
    messages = _messages_of(events)
    # After q1: 2 message events appended (q1, a1)
    # After q2: one snapshot event (rewrite), no delta messages
    assert len(snapshots) == 1
    assert len(messages) == 2
    snap_payload = snapshots[0]["payload"]
    assert len(snap_payload["messages"]) == 2
    assert snap_payload["messages"][0]["content"][0]["text"].startswith(
        "<session_summary>"
    )


def test_repl_reset_starts_new_session_id(isolated_agent_home, monkeypatch, capsys):
    """/reset should yield a fresh session id and clear history."""
    q1 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    q2 = AgentResult(
        status="completed",
        messages=(_user_msg("q2"), _assistant_msg("a2")),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["q1", "/reset", "q2", "/exit"],
        fake_loop_results=[q1, q2],
    )
    # Two session files: one for original, one for post-reset
    files = sorted(isolated_agent_home["sessions_dir"].glob("*.jsonl"))
    assert len(files) == 2


def test_repl_resume_reconstructs_history(isolated_agent_home, monkeypatch, capsys):
    """Build a saved session; /resume into a fresh REPL; expect the prior
    messages to form the new REPL's conversation history."""
    # Step 1: build a session by running one REPL that does 2 queries
    q1 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    q2 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1"),
                   _user_msg("q2"), _assistant_msg("a2")),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["q1", "q2", "/exit"],
        fake_loop_results=[q1, q2],
    )

    # Step 2: find the saved session id (prefix) and resume it in a new REPL
    saved_path = _find_session_file(isolated_agent_home["sessions_dir"])
    saved_id = saved_path.stem
    prefix = saved_id[:8]

    # After /resume, send q3 → completed with full 6-message history
    q3 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1"),
                   _user_msg("q2"), _assistant_msg("a2"),
                   _user_msg("q3"), _assistant_msg("a3")),
        reason="",
    )
    # Reset audit logger so the second REPL run configures a fresh handler
    from agent import audit as audit_mod
    audit_mod.reset_audit_logger()
    _drive_repl(
        monkeypatch,
        lines=[f"/resume {prefix}", "q3", "/exit"],
        fake_loop_results=[q3],
    )

    # Session file should now contain the resumed events + q3's delta
    events = _read_events(saved_path)
    messages = _messages_of(events)
    texts = [m["payload"]["content"][0]["text"] for m in messages]
    assert texts == ["q1", "a1", "q2", "a2", "q3", "a3"]


def test_repl_resume_respects_snapshot_reset(isolated_agent_home, monkeypatch):
    """A saved session with a mid-file `snapshot` event: resuming it and
    sending one more query must result in `snapshot.messages + new q/a`,
    not the pre-snapshot messages."""
    q1 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    # q2 rewrites the history via a compaction-style result
    q2 = AgentResult(
        status="completed",
        messages=(
            _user_msg("<session_summary>s</session_summary>"),
            _assistant_msg("ack"),
        ),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["q1", "q2", "/exit"],
        fake_loop_results=[q1, q2],
    )

    saved_path = _find_session_file(isolated_agent_home["sessions_dir"])
    prefix = saved_path.stem[:8]

    # After resume, send q3 → prefix extension of the post-snapshot state
    q3 = AgentResult(
        status="completed",
        messages=(
            _user_msg("<session_summary>s</session_summary>"),
            _assistant_msg("ack"),
            _user_msg("q3"),
            _assistant_msg("a3"),
        ),
        reason="",
    )
    from agent import audit as audit_mod
    audit_mod.reset_audit_logger()
    _drive_repl(
        monkeypatch,
        lines=[f"/resume {prefix}", "q3", "/exit"],
        fake_loop_results=[q3],
    )

    events = _read_events(saved_path)
    # Order: session_meta, q1 msg, a1 msg, snapshot, q3 msg, a3 msg
    types_in_order = [e["type"] for e in events]
    assert "snapshot" in types_in_order
    messages = _messages_of(events)
    # Only 4 message events (q1, a1 before snapshot; q3, a3 after)
    assert len(messages) == 4
    texts = [m["payload"]["content"][0]["text"] for m in messages]
    assert texts == ["q1", "a1", "q3", "a3"]


def test_repl_resume_rejected_after_any_message_committed(
    isolated_agent_home, monkeypatch, capsys
):
    """Contract R window: once the current session has committed a message,
    `/resume` must refuse with a message instructing /exit + relaunch."""
    q1 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    # Also need to pre-create a resumable session; simplest path is to
    # reuse q1's file from the first REPL then try resume in the same run.
    # But the test wants to verify resume AFTER a commit in the CURRENT
    # session — so just do: q1 → /resume anything → should refuse.
    _drive_repl(
        monkeypatch,
        lines=["q1", "/resume deadbeef", "/exit"],
        fake_loop_results=[q1],
    )
    out = capsys.readouterr().out
    assert "resume failed" in out
    # And the current session file still exists with its 2 messages intact
    path = _find_session_file(isolated_agent_home["sessions_dir"])
    messages = _messages_of(_read_events(path))
    assert len(messages) == 2


def test_repl_resume_failure_keeps_new_session_intact(
    isolated_agent_home, monkeypatch, capsys
):
    """/resume <unknown-prefix> must print an error and leave the current
    empty session available for the next real query."""
    # The fake_loop_results are only consumed when the user actually runs
    # a real query — /resume and /exit don't call it.
    q_after_failed_resume = AgentResult(
        status="completed",
        messages=(_user_msg("fresh"), _assistant_msg("ok")),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["/resume zzz-no-match", "fresh", "/exit"],
        fake_loop_results=[q_after_failed_resume],
    )
    out = capsys.readouterr().out
    assert "resume failed" in out
    # The fresh query went through → exactly one session file with 2 msgs
    path = _find_session_file(isolated_agent_home["sessions_dir"])
    messages = _messages_of(_read_events(path))
    assert len(messages) == 2
    assert messages[0]["payload"]["content"][0]["text"] == "fresh"


def test_repl_sessions_command_lists_recent(isolated_agent_home, monkeypatch, capsys):
    """/sessions prints session prefixes in mtime-desc order."""
    # Pre-seed three sessions via direct SessionWriter calls (avoids
    # running the REPL three times)
    from agent.session import SessionWriter
    import os
    sessions_dir = isolated_agent_home["sessions_dir"]
    for i, sid in enumerate(["aaa11111-0000-0000-0000-000000000001",
                              "bbb22222-0000-0000-0000-000000000002",
                              "ccc33333-0000-0000-0000-000000000003"]):
        w = SessionWriter(sid, sessions_dir, cwd=".", model="m")
        w.append_message(_user_msg(f"msg-{i}"))
        # stagger mtimes
        p = sessions_dir / f"{sid}.jsonl"
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + i * 10))

    # Now run a tiny REPL that just does /sessions and /exit
    _drive_repl(
        monkeypatch,
        lines=["/sessions", "/exit"],
        fake_loop_results=[],
    )
    out = capsys.readouterr().out
    # Newest first by mtime
    idx_aaa = out.find("aaa11111")
    idx_bbb = out.find("bbb22222")
    idx_ccc = out.find("ccc33333")
    assert idx_ccc < idx_bbb < idx_aaa
    assert all(i >= 0 for i in (idx_aaa, idx_bbb, idx_ccc))


def test_repl_session_open_and_close_events_in_audit(isolated_agent_home, monkeypatch):
    """REPL lifecycle emits session.open at start and session.close at exit
    (via the configured agent.audit logger)."""
    q1 = AgentResult(
        status="completed",
        messages=(_user_msg("q1"), _assistant_msg("a1")),
        reason="",
    )
    _drive_repl(
        monkeypatch,
        lines=["q1", "/exit"],
        fake_loop_results=[q1],
    )
    log_file = isolated_agent_home["log_dir"] / "audit.jsonl"
    assert log_file.exists()
    events = [json.loads(line)
              for line in log_file.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    names = [e.get("event") for e in events]
    assert "session.open" in names
    assert "session.close" in names

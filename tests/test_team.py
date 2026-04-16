"""Tests for agent.team — Phase 7 Layer 2 team mailbox coordination."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.team import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REGISTERED,
    STATUS_RUNNING,
    Mailbox,
    TeamManager,
)


@pytest.fixture
def team_env(tmp_path):
    """Provides a TeamManager + Mailbox rooted at tmp_path."""
    tm = TeamManager(tmp_path)
    tm.create_team("test-team")
    mb = Mailbox(tm._team_dir("test-team"))
    return {"tm": tm, "mb": mb, "team_name": "test-team", "base": tmp_path}


# ---------------------------------------------------------------------------
# TeamManager
# ---------------------------------------------------------------------------


def test_team_create_initializes_directory(tmp_path):
    tm = TeamManager(tmp_path)
    td = tm.create_team("myteam")
    assert (td / "team.json").exists()
    assert (td / "mailboxes" / "leader").is_dir()


def test_team_create_registers_leader(tmp_path):
    tm = TeamManager(tmp_path)
    tm.create_team("myteam")
    data = tm.load_team("myteam")
    assert data["leader"] == "leader"
    assert any(m["name"] == "leader" for m in data["members"])


def test_team_register_member(team_env):
    team_env["tm"].register_member("test-team", "researcher")
    data = team_env["tm"].load_team("test-team")
    names = {m["name"] for m in data["members"]}
    assert "researcher" in names
    # Mailbox dir created
    assert (team_env["tm"]._team_dir("test-team") / "mailboxes" / "researcher").is_dir()


def test_team_member_status_transitions(team_env):
    tm = team_env["tm"]
    tm.register_member("test-team", "worker")
    tm.update_status("test-team", "worker", STATUS_RUNNING)
    data = tm.load_team("test-team")
    worker = next(m for m in data["members"] if m["name"] == "worker")
    assert worker["status"] == STATUS_RUNNING

    tm.update_status("test-team", "worker", STATUS_COMPLETED)
    data = tm.load_team("test-team")
    worker = next(m for m in data["members"] if m["name"] == "worker")
    assert worker["status"] == STATUS_COMPLETED


def test_team_load_nonexistent_raises(tmp_path):
    tm = TeamManager(tmp_path)
    with pytest.raises(FileNotFoundError):
        tm.load_team("nope")


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------


def test_mailbox_send_creates_message_file(team_env):
    mb = team_env["mb"]
    p = mb.send("leader", "researcher", "do the analysis")
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["from"] == "leader"
    assert data["body"] == "do the analysis"
    assert data["consumed"] is False


def test_mailbox_peek_returns_unread_without_consuming(team_env):
    mb = team_env["mb"]
    mb.send("leader", "worker", "task 1")
    mb.send("leader", "worker", "task 2")
    msgs = mb.peek("worker")
    assert len(msgs) == 2
    # Peek again — still 2 (not consumed)
    assert len(mb.peek("worker")) == 2


def test_mailbox_ack_marks_consumed(team_env):
    mb = team_env["mb"]
    mb.send("leader", "worker", "task")
    msgs = mb.peek("worker")
    assert len(msgs) == 1
    mb.ack("worker", [msgs[0].id])
    assert len(mb.peek("worker")) == 0  # consumed


def test_mailbox_unread_count(team_env):
    mb = team_env["mb"]
    assert mb.unread_count("worker") == 0
    mb.send("leader", "worker", "a")
    mb.send("leader", "worker", "b")
    assert mb.unread_count("worker") == 2
    msgs = mb.peek("worker")
    mb.ack("worker", [msgs[0].id])
    assert mb.unread_count("worker") == 1


def test_mailbox_multiple_senders(team_env):
    mb = team_env["mb"]
    mb.send("leader", "worker", "from leader")
    mb.send("researcher", "worker", "from researcher")
    msgs = mb.peek("worker")
    assert len(msgs) == 2
    senders = {m.from_name for m in msgs}
    assert senders == {"leader", "researcher"}


# ---------------------------------------------------------------------------
# Two-phase consumption semantics
# ---------------------------------------------------------------------------


def test_agent_failure_does_not_consume_messages(team_env):
    """If agent fails, messages remain unconsumed (peek'd but not ack'd)."""
    mb = team_env["mb"]
    mb.send("leader", "worker", "important task")
    msgs = mb.peek("worker")
    assert len(msgs) == 1
    # Simulate failure: do NOT ack
    # Next peek should still see the message
    assert len(mb.peek("worker")) == 1


def test_ack_only_specific_ids(team_env):
    mb = team_env["mb"]
    mb.send("leader", "worker", "task A")
    mb.send("leader", "worker", "task B")
    msgs = mb.peek("worker")
    # Ack only first
    mb.ack("worker", [msgs[0].id])
    remaining = mb.peek("worker")
    assert len(remaining) == 1
    assert remaining[0].body == "task B"


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_mailbox_messages_injected_into_prompt(team_env):
    from agent.prompt import build_mailbox_section
    mb = team_env["mb"]
    mb.send("leader", "worker", "analyze the code")
    msgs = mb.peek("worker")
    section = build_mailbox_section(msgs)
    assert "Unread Messages" in section
    assert "leader" in section
    assert "analyze the code" in section


def test_mailbox_empty_produces_no_section():
    from agent.prompt import build_mailbox_section
    assert build_mailbox_section([]) == ""

"""Tests for agent.audit — Phase 4 structured logger."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent.audit import emit, get_audit_logger, hash_path, reset_audit_logger


@pytest.fixture(autouse=True)
def _clean_audit():
    reset_audit_logger()
    yield
    reset_audit_logger()


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Logger isolation
# ---------------------------------------------------------------------------


def test_audit_logger_does_not_pollute_root(tmp_path):
    logger = get_audit_logger(tmp_path / "audit.jsonl")
    # Root logger has no audit handlers
    root = logging.getLogger()
    assert not any(
        getattr(h, "_agent_audit_handler", False) for h in root.handlers
    )
    # Our logger has exactly one
    marks = [getattr(h, "_agent_audit_handler", False) for h in logger.handlers]
    assert marks.count(True) == 1
    # Propagation disabled
    assert logger.propagate is False


def test_audit_logger_is_idempotent(tmp_path):
    p = tmp_path / "audit.jsonl"
    l1 = get_audit_logger(p)
    l2 = get_audit_logger(p)
    assert l1 is l2
    # Still only one handler
    marks = [getattr(h, "_agent_audit_handler", False) for h in l1.handlers]
    assert marks.count(True) == 1


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


def test_audit_emits_json_lines(tmp_path):
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p)
    emit(logger, "session.open", session_id="abc123", msg="start")
    entries = _read_lines(p)
    assert len(entries) == 1
    assert entries[0]["event"] == "session.open"
    assert entries[0]["session_id"] == "abc123"
    assert entries[0]["msg"] == "start"


def test_audit_fixed_schema_fields_present(tmp_path):
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p)
    emit(logger, "test.event", session_id="s1", turn=3,
         tool="bash", decision="DENY", path="abc123:id_rsa",
         msg="x")
    entry = _read_lines(p)[0]
    for required in ("ts", "level", "event", "session_id", "turn",
                      "tool", "decision", "path", "msg"):
        assert required in entry, f"missing {required}"
    assert entry["level"] == "INFO"


def test_audit_absent_fields_are_omitted(tmp_path):
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p)
    emit(logger, "session.open", msg="hi")  # no session_id/turn/tool/...
    entry = _read_lines(p)[0]
    for absent in ("session_id", "turn", "tool", "decision", "path"):
        assert absent not in entry, f"{absent} should be omitted"


def test_audit_extra_fields_preserved(tmp_path):
    """Events like tool.exec.end carry extras (duration_ms, size, is_error)."""
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p)
    emit(logger, "tool.exec.end", tool="bash",
         duration_ms=1234, size=512, is_error=False)
    entry = _read_lines(p)[0]
    assert entry["event"] == "tool.exec.end"
    assert entry["duration_ms"] == 1234
    assert entry["size"] == 512
    assert entry["is_error"] is False


# ---------------------------------------------------------------------------
# hash_path + sensitive value protection
# ---------------------------------------------------------------------------


def test_hash_path_returns_hash_plus_basename():
    out = hash_path("/home/user/.ssh/id_rsa")
    hash_part, name = out.split(":", 1)
    assert len(hash_part) == 8
    assert name == "id_rsa"


def test_hash_path_different_paths_different_hashes():
    a = hash_path("/home/user/.ssh/id_rsa")
    b = hash_path("/home/user/.bashrc")
    assert a != b


def test_audit_path_is_hashed_not_raw(tmp_path):
    """contract L: `path` field must carry hashed form, not the raw path."""
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p)
    emit(logger, "permission.decision",
         tool="bash", decision="DENY",
         path=hash_path("/home/user/.ssh/id_rsa"))
    entry = _read_lines(p)[0]
    assert "id_rsa" in entry["path"]
    assert "/home/user" not in entry["path"]
    assert entry["path"].split(":")[0] != ""


def test_audit_tool_result_content_not_logged(tmp_path):
    """contract L: tool.exec.end logs only size + is_error, never content."""
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p)
    emit(logger, "tool.exec.end", tool="bash", size=32, is_error=False)
    entry = _read_lines(p)[0]
    assert "content" not in entry


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_audit_rotates_at_max_bytes(tmp_path):
    p = tmp_path / "audit.jsonl"
    logger = get_audit_logger(p, max_bytes=300, backup_count=3)
    # Write enough entries to overflow
    for i in range(20):
        emit(logger, "test.event", msg=f"filler {i} " * 5)
    # At least one backup should exist
    backups = sorted(tmp_path.glob("audit.jsonl.*"))
    assert len(backups) >= 1


def test_emit_with_none_logger_is_noop():
    """audit logging is opt-in; emit(None, ...) must not raise."""
    emit(None, "any.event", session_id="x")

"""Tests for agent.session — Phase 4 JSONL transcript + snapshot + resume."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent.session import (
    EVENT_MESSAGE,
    EVENT_SESSION_META,
    EVENT_SNAPSHOT,
    EVENT_STATE_MARKER,
    SESSION_VERSION,
    AmbiguousPrefixError,
    MidfileCorruptionError,
    NoSuchSessionError,
    SessionWriter,
    UnsupportedSessionVersion,
    list_sessions,
    load_session,
    new_session_id,
    resolve_prefix,
    truncate_corrupt_tail,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_writer(tmp_path: Path, *, session_id: str = None) -> SessionWriter:
    sid = session_id or "fixed-test-id"
    return SessionWriter(
        sid, tmp_path, cwd=str(tmp_path), model="claude-test",
    )


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_msg(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# SessionWriter
# ---------------------------------------------------------------------------


def test_writer_is_lazy_no_file_before_first_write(tmp_path):
    """Contract R: writer does not create a file on construction."""
    w = _make_writer(tmp_path)
    assert not w.file_created
    assert not w.path.exists()


def test_session_meta_is_first_line_with_version(tmp_path):
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("hi"))
    events = _read_events(w.path)
    assert events[0]["type"] == EVENT_SESSION_META
    assert events[0]["payload"]["version"] == SESSION_VERSION
    assert events[1]["type"] == EVENT_MESSAGE


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
def test_session_writer_creates_file_with_0600_perms(tmp_path):
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("hi"))
    mode = stat.S_IMODE(w.path.stat().st_mode)
    assert mode == 0o600


def test_message_append_round_trip(tmp_path):
    w = _make_writer(tmp_path)
    msg = _user_msg("hello")
    w.append_message(msg)
    result = load_session(w.path)
    assert len(result.messages) == 1
    assert result.messages[0]["role"] == "user"
    assert result.messages[0]["content"][0]["text"] == "hello"


def test_seq_numbers_monotonic(tmp_path):
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("a"))
    w.append_message(_assistant_msg("b"))
    w.append_message(_user_msg("c"))
    events = _read_events(w.path)
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(events)))


def test_tool_result_block_stays_inside_user_message(tmp_path):
    """Contract P: tool_result is a content block within a role=user message,
    NOT a separate event."""
    w = _make_writer(tmp_path)
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "out"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "out2"},
        ],
    }
    w.append_message(msg)
    events = _read_events(w.path)
    # 1 session_meta + 1 message (not 2 tool_result events)
    assert len(events) == 2
    assert events[1]["type"] == EVENT_MESSAGE
    assert len(events[1]["payload"]["content"]) == 2
    # resume reconstructs a single user message with both blocks
    result = load_session(w.path)
    assert len(result.messages) == 1
    assert len(result.messages[0]["content"]) == 2


def test_state_marker_is_not_replayed(tmp_path):
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("hi"))
    w.append_state_marker("autocompact", compact_tripped=False,
                          counts={"autocompact_count": 1})
    w.append_message(_assistant_msg("ok"))
    result = load_session(w.path)
    # Only the two messages, state_marker skipped
    assert len(result.messages) == 2
    assert result.messages[0]["role"] == "user"
    assert result.messages[1]["role"] == "assistant"


def test_snapshot_resets_replay(tmp_path):
    """Contract P: resume replay treats snapshot as a hard reset point."""
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("a"))
    w.append_message(_assistant_msg("b"))
    # Simulate autocompact: the original prefix is replaced
    w.append_snapshot([_user_msg("SUMMARY"), _assistant_msg("ack")])
    w.append_message(_user_msg("c"))
    result = load_session(w.path)
    # Expect the two snapshot messages + the post-snapshot user message
    assert len(result.messages) == 3
    assert result.messages[0]["content"][0]["text"] == "SUMMARY"
    assert result.messages[2]["content"][0]["text"] == "c"


def test_fsync_called_on_session_meta_and_snapshot(tmp_path, monkeypatch):
    """session_meta and snapshot events trigger fsync."""
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("hi"))     # triggers session_meta (fsync) + message (no fsync)
    w.append_snapshot([])                 # fsync
    # At minimum, one call for session_meta + one for snapshot
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# Corruption handling
# ---------------------------------------------------------------------------


def test_tail_corrupted_line_is_truncated_on_resume(tmp_path):
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("a"))
    w.append_message(_user_msg("b"))
    with open(w.path, "a", encoding="utf-8") as f:
        f.write("{this is not valid json\n")
        f.write("also garbage\n")
    result = load_session(w.path)
    assert result.truncated_tail_lines == 2
    assert len(result.messages) == 2


def test_truncate_corrupt_tail_physically_removes_garbage(tmp_path):
    """After truncate_corrupt_tail, the file only contains good lines.
    A subsequent SessionWriter reattach + append must produce a file
    that load_session can parse without mid-file corruption."""
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("a"))
    w.append_message(_user_msg("b"))
    with open(w.path, "a", encoding="utf-8") as f:
        f.write("GARBAGE\n")
        f.write("MORE GARBAGE\n")

    removed = truncate_corrupt_tail(w.path)
    assert removed == 2

    # Reattach and write a new message
    w2 = SessionWriter("fixed-test-id", tmp_path, cwd=".", model="m")
    w2.append_message(_user_msg("c"))

    # Must be loadable with zero truncated lines (garbage is gone)
    result = load_session(w.path)
    assert result.truncated_tail_lines == 0
    assert len(result.messages) == 3
    assert [m["content"][0]["text"] for m in result.messages] == ["a", "b", "c"]


def test_midfile_corruption_refuses_to_resume(tmp_path):
    """Mid-file corruption (with following good lines) must raise, not
    silently drop data."""
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("a"))
    # Inject a corrupt line between two good ones
    content = w.path.read_text(encoding="utf-8")
    lines = content.splitlines()
    bad_lines = lines[:-1] + ["{not json", lines[-1]]
    w.path.write_text("\n".join(bad_lines) + "\n", encoding="utf-8")
    with pytest.raises(MidfileCorruptionError):
        load_session(w.path)


def test_version_mismatch_on_resume_raises(tmp_path):
    w = _make_writer(tmp_path)
    w.append_message(_user_msg("a"))
    # Rewrite file with wrong version
    content = w.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(content[0])
    first["payload"]["version"] = "99"
    content[0] = json.dumps(first)
    w.path.write_text("\n".join(content) + "\n", encoding="utf-8")
    with pytest.raises(UnsupportedSessionVersion):
        load_session(w.path)


def test_empty_file_raises(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    with pytest.raises(Exception):
        load_session(p)


def test_missing_file_raises_no_such_session(tmp_path):
    with pytest.raises(NoSuchSessionError):
        load_session(tmp_path / "does-not-exist.jsonl")


# ---------------------------------------------------------------------------
# list_sessions + resolve_prefix
# ---------------------------------------------------------------------------


def _touch_session(base_dir: Path, session_id: str, mtime_offset: int = 0) -> None:
    w = SessionWriter(session_id, base_dir, cwd=".", model="m")
    w.append_message(_user_msg(session_id))
    # Set mtime for ordering tests
    if mtime_offset:
        os.utime(w.path, (w.path.stat().st_atime,
                          w.path.stat().st_mtime + mtime_offset))


def test_list_sessions_sorted_by_mtime_desc(tmp_path):
    _touch_session(tmp_path, "ab111111", mtime_offset=-1000)
    _touch_session(tmp_path, "ab222222", mtime_offset=0)
    _touch_session(tmp_path, "ab333333", mtime_offset=-500)
    summaries = list_sessions(tmp_path)
    assert len(summaries) == 3
    assert [s.session_id for s in summaries] == ["ab222222", "ab333333", "ab111111"]


def test_list_sessions_counts_messages(tmp_path):
    w = SessionWriter("cc111111", tmp_path, cwd=".", model="m")
    w.append_message(_user_msg("a"))
    w.append_message(_user_msg("b"))
    w.append_state_marker("autocompact")  # should NOT count as a message
    [summary] = list_sessions(tmp_path)
    assert summary.message_count == 2


def test_resolve_prefix_unique_match(tmp_path):
    _touch_session(tmp_path, "aaabbb")
    _touch_session(tmp_path, "xxxyyy")
    assert resolve_prefix(tmp_path, "aaa") == "aaabbb"


def test_resolve_prefix_ambiguous_raises(tmp_path):
    _touch_session(tmp_path, "shared11")
    _touch_session(tmp_path, "shared22")
    with pytest.raises(AmbiguousPrefixError) as ei:
        resolve_prefix(tmp_path, "shared")
    assert "shared" in str(ei.value)
    assert sorted(ei.value.candidates) == ["shared11", "shared22"]


def test_resolve_prefix_no_match_raises(tmp_path):
    _touch_session(tmp_path, "aaabbb")
    with pytest.raises(NoSuchSessionError):
        resolve_prefix(tmp_path, "zzz")


def test_list_sessions_returns_empty_on_missing_dir(tmp_path):
    assert list_sessions(tmp_path / "no_such_dir") == []


def test_new_session_id_is_uuid(tmp_path):
    import uuid
    sid = new_session_id()
    # Will raise if not a valid UUID
    uuid.UUID(sid)

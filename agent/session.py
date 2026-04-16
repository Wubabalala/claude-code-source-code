"""Session transcript — Phase 4 JSONL persistence.

One line = one event. Event types per contract P:

  - session_meta   first line; {created_at, version, cwd, model}
  - message        a complete Anthropic message (role + content[])
                   tool_result blocks live inside role=user messages,
                   NOT as their own event
  - snapshot       history-rewrite checkpoint; payload = {messages: [...]}
                   written when result.messages is NOT a prefix extension
                   of prior history (Phase 2 compaction rewrote the past)
  - state_marker   diagnostic trace of a State transition; NOT replayed

Resume reconstruction (contract R step 3):
  - snapshot    → reset rebuilt history to payload.messages
  - message     → append one to current rebuilt history
  - state_marker → skip
  - trailing corrupt lines → truncate + warn
  - mid-file corruption → raise (refuse to resume)

File layout: `<base_dir>/<uuid>.jsonl`, created lazily with mode 0o600.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


SESSION_VERSION = "1"

# Event type constants
EVENT_SESSION_META = "session_meta"
EVENT_MESSAGE = "message"
EVENT_SNAPSHOT = "snapshot"
EVENT_STATE_MARKER = "state_marker"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Base class for session-related errors."""


class NoSuchSessionError(SessionError):
    pass


class AmbiguousPrefixError(SessionError):
    def __init__(self, prefix: str, candidates: list[str]):
        super().__init__(f"prefix {prefix!r} matches {len(candidates)} sessions")
        self.prefix = prefix
        self.candidates = candidates


class UnsupportedSessionVersion(SessionError):
    pass


class MidfileCorruptionError(SessionError):
    def __init__(self, line_number: int, message: str):
        super().__init__(f"line {line_number}: {message}")
        self.line_number = line_number


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionWriter:
    """Append-only JSONL writer for a single session.

    File creation is LAZY (contract R): the file is not created on __init__,
    only when the first event is actually written. This lets the REPL run
    /resume on a fresh process without leaving empty session files behind
    if the user exits without sending any message.
    """

    def __init__(self, session_id: str, base_dir: Path, *, cwd: str, model: str):
        self.session_id = session_id
        self.base_dir = Path(base_dir).expanduser()
        self._cwd = cwd
        self._model = model
        self._seq = 0
        self._file_created = False
        self._path = self.base_dir / f"{session_id}.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def file_created(self) -> bool:
        return self._file_created

    def _ensure_file(self) -> None:
        if self._file_created:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            # Reattaching to an existing session file (e.g. after /resume).
            # Scan existing events to resume seq numbering; do NOT write a
            # second session_meta.
            last_seq = -1
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        last_seq = max(last_seq, entry.get("seq", -1))
            except OSError:
                pass
            self._seq = last_seq + 1
            self._file_created = True
            return

        # Fresh file: create with restrictive permissions and write the
        # session_meta first line.
        self._path.touch(mode=0o600, exist_ok=False)
        if os.name != "nt":
            os.chmod(self._path, 0o600)
        self._file_created = True
        self._append_raw(
            EVENT_SESSION_META,
            {
                "created_at": _now_iso(),
                "version": SESSION_VERSION,
                "cwd": self._cwd,
                "model": self._model,
            },
            fsync=True,
        )

    def _append_raw(self, event_type: str, payload: dict, *, fsync: bool = False) -> None:
        entry = {
            "ts": _now_iso(),
            "session_id": self.session_id,
            "seq": self._seq,
            "type": event_type,
            "payload": payload,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        self._seq += 1

    def append_message(self, message: dict) -> None:
        """Write a complete Anthropic message (role + content[]).

        Contract P: tool_result blocks live inside the message content, not
        as separate events. Caller is responsible for passing the original
        message dict produced by the agent loop.
        """
        self._ensure_file()
        self._append_raw(EVENT_MESSAGE, {"role": message["role"],
                                          "content": message["content"]})

    def append_snapshot(self, messages: Iterable[dict]) -> None:
        """Write a history-rewrite checkpoint containing the full current
        conversation_history. Used when compaction (or any rewrite) means a
        delta append is not enough to reconstruct the new state."""
        self._ensure_file()
        self._append_raw(
            EVENT_SNAPSHOT,
            {"messages": list(messages)},
            fsync=True,
        )

    def append_state_marker(self, transition_reason: str, *,
                            compact_tripped: bool = False,
                            counts: Optional[dict] = None) -> None:
        """Write a diagnostic state transition. Never replayed on resume."""
        self._ensure_file()
        self._append_raw(
            EVENT_STATE_MARKER,
            {
                "transition_reason": transition_reason,
                "compact_tripped": compact_tripped,
                "counts": counts or {},
            },
            fsync=True,
        )


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    path: Path
    last_updated: float       # file mtime
    message_count: int


@dataclass(frozen=True)
class SessionResumeResult:
    session_id: str
    messages: list[dict]
    last_updated: float
    truncated_tail_lines: int  # how many corrupt trailing lines were dropped


def _parse_lines(path: Path) -> tuple[list[dict], int]:
    """Parse JSONL file. Returns (events, truncated_tail_count).

    Raises MidfileCorruptionError when a corrupt line appears before any
    subsequent good line (to prevent silent data loss).
    """
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    # First pass: try to parse each line, collecting good and bad indices.
    parsed: list[Optional[dict]] = []
    for i, line in enumerate(raw_lines, 1):
        line = line.rstrip("\n")
        if not line:
            parsed.append(None)
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"__corrupt__": True, "line_number": i})

    # Trim a contiguous tail of corrupt lines (allowed)
    tail_truncated = 0
    while parsed and parsed[-1] is not None and parsed[-1].get("__corrupt__"):
        parsed.pop()
        tail_truncated += 1

    # Any remaining corrupt line (not at tail) is fatal
    for item in parsed:
        if item is not None and item.get("__corrupt__"):
            raise MidfileCorruptionError(
                item["line_number"], "corrupt JSONL line preceding a good line"
            )

    entries = [e for e in parsed if e is not None]
    return entries, tail_truncated


def load_session(path: Path) -> SessionResumeResult:
    """Replay a session JSONL file into a SessionResumeResult.

    Contract P recovery:
      - session_meta:  verify version
      - snapshot:      reset messages to payload.messages
      - message:       append one
      - state_marker:  skip (diagnostic only)
      - tail corruption: truncated with warning (count reported)
      - mid-file corruption: raise
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise NoSuchSessionError(f"no such session file: {path}")

    entries, truncated = _parse_lines(path)

    if not entries:
        raise SessionError(f"empty session file: {path}")

    meta = entries[0]
    if meta.get("type") != EVENT_SESSION_META:
        raise SessionError(f"first event is not session_meta: {path}")
    version = meta.get("payload", {}).get("version")
    if version != SESSION_VERSION:
        raise UnsupportedSessionVersion(
            f"session version {version!r} != supported {SESSION_VERSION!r}"
        )

    messages: list[dict] = []
    for entry in entries[1:]:
        etype = entry.get("type")
        payload = entry.get("payload", {})
        if etype == EVENT_SNAPSHOT:
            messages = list(payload.get("messages", []))
        elif etype == EVENT_MESSAGE:
            messages.append({"role": payload["role"],
                             "content": payload["content"]})
        elif etype == EVENT_STATE_MARKER:
            continue
        elif etype == EVENT_SESSION_META:
            # Shouldn't appear again; tolerate and ignore
            continue
        else:
            print(f"[session] warn: unknown event type {etype!r}; skipping",
                  file=sys.stderr)

    return SessionResumeResult(
        session_id=meta["session_id"],
        messages=messages,
        last_updated=path.stat().st_mtime,
        truncated_tail_lines=truncated,
    )


# ---------------------------------------------------------------------------
# Discovery / prefix resolution
# ---------------------------------------------------------------------------


def list_sessions(base_dir: Path, limit: int = 20) -> list[SessionSummary]:
    """Return up to `limit` most-recently-modified sessions."""
    base = Path(base_dir).expanduser()
    if not base.exists():
        return []
    summaries: list[SessionSummary] = []
    for p in base.glob("*.jsonl"):
        sid = p.stem
        try:
            with open(p, "r", encoding="utf-8") as f:
                count = sum(
                    1 for line in f
                    if line.strip() and '"type": "message"' in line
                )
        except OSError:
            count = 0
        summaries.append(SessionSummary(
            session_id=sid,
            path=p,
            last_updated=p.stat().st_mtime,
            message_count=count,
        ))
    summaries.sort(key=lambda s: s.last_updated, reverse=True)
    return summaries[:limit]


def resolve_prefix(base_dir: Path, prefix: str) -> str:
    """Resolve a session-id prefix to a unique session_id, or raise.

    - No match    → NoSuchSessionError
    - Multiple    → AmbiguousPrefixError (with candidate list)
    - Exactly one → return the session_id
    """
    base = Path(base_dir).expanduser()
    if not base.exists():
        raise NoSuchSessionError(f"no sessions directory: {base}")
    matches = [p.stem for p in base.glob(f"{prefix}*.jsonl")]
    if not matches:
        raise NoSuchSessionError(f"no session matching prefix {prefix!r}")
    if len(matches) > 1:
        raise AmbiguousPrefixError(prefix, matches)
    return matches[0]


def new_session_id() -> str:
    return str(uuid.uuid4())

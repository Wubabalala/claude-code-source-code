"""Structured audit logger — Phase 4.

Dedicated `agent.audit` logger with a JSON formatter writing to a rotating
JSONL file. Separate from session transcripts (those live in agent/session.py).

Contract L summary:
  - Logger name: `agent.audit` (propagate=False to avoid polluting root)
  - Fixed schema: ts, level, event, session_id, turn, tool, decision, path, msg
  - Absent fields are OMITTED from output (not null)
  - Path fields are hashed (sha256[:8]:basename), never raw
  - Tool result bodies are NEVER logged (only size + is_error)

Event names (Phase 4 initial set):
  session.open / session.close / session.resume / session.recover.truncate
  permission.prompted      (permissions.py emits; records ASK interaction)
  permission.decision      (loop.py emits; records final effective decision)
  tool.exec.start / tool.exec.end   (with duration_ms)
  compact.transition
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any, Optional


_LOGGER_NAME = "agent.audit"

# The complete ordered list of fields the formatter emits (others omitted).
_FIELD_ORDER = (
    "ts", "level", "event", "session_id", "turn", "tool",
    "decision", "path", "msg",
)


class _JSONFormatter(logging.Formatter):
    """Emit one JSON object per log record, with fixed key order and
    omit-absent-fields semantics."""

    def format(self, record: logging.LogRecord) -> str:
        # `extra` fields attached to the record via logger.info(..., extra={})
        # become attributes on `record`. Collect the ones we recognize.
        fields: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "event": getattr(record, "event", None),
            "session_id": getattr(record, "session_id", None),
            "turn": getattr(record, "turn", None),
            "tool": getattr(record, "tool", None),
            "decision": getattr(record, "decision", None),
            "path": getattr(record, "path", None),
            "msg": record.getMessage() or None,
        }
        # Ordered output with None/empty-string fields omitted (so that
        # absent fields don't clutter the log).
        ordered: dict[str, Any] = {}
        for key in _FIELD_ORDER:
            v = fields.get(key)
            if v is None:
                continue
            if isinstance(v, str) and v == "":
                continue
            ordered[key] = v
        # Preserve unknown extras (e.g., tool.exec.end might carry
        # duration_ms, is_error, size). Emit them after the fixed fields.
        for key, value in record.__dict__.items():
            if key in ordered or key in _FIELD_ORDER:
                continue
            if key in _STANDARD_LOGRECORD_ATTRS:
                continue
            ordered[key] = value

        return json.dumps(ordered, ensure_ascii=False)


# Attributes that logging.LogRecord sets implicitly — not user-provided extras.
_STANDARD_LOGRECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "event", "session_id",
    "turn", "tool", "decision", "path", "taskName",
})


def hash_path(raw_path: str) -> str:
    """Return a redacted path representation: first-8 sha256 hex + basename.

    Enables correlation without leaking absolute paths into audit logs.
    """
    p = Path(raw_path).expanduser()
    digest = hashlib.sha256(str(p).encode("utf-8", errors="replace")).hexdigest()
    return f"{digest[:8]}:{p.name}"


def get_audit_logger(
    audit_file: Path,
    *,
    level: str = "INFO",
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """Return the shared `agent.audit` logger, configuring it on first call.

    Idempotent: repeat calls re-use handlers rather than stacking.
    """
    logger = logging.getLogger(_LOGGER_NAME)

    # Configure only once
    if any(getattr(h, "_agent_audit_handler", False) for h in logger.handlers):
        return logger

    audit_file = Path(audit_file).expanduser()
    audit_file.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        audit_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(_JSONFormatter())
    # Mark this handler so we don't add duplicates on reconfigure
    handler._agent_audit_handler = True  # type: ignore[attr-defined]

    logger.addHandler(handler)
    logger.setLevel(level)
    # Do NOT propagate to root — contract L owner isolation
    logger.propagate = False
    return logger


def reset_audit_logger() -> None:
    """Test helper: remove all handlers from agent.audit so the next
    get_audit_logger() reconfigures from scratch."""
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        try:
            h.close()
        finally:
            logger.removeHandler(h)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def emit(logger: Optional[logging.Logger], event: str, *,
         session_id: Optional[str] = None,
         turn: Optional[int] = None,
         tool: Optional[str] = None,
         decision: Optional[str] = None,
         path: Optional[str] = None,
         msg: str = "",
         **extra: Any) -> None:
    """Convenience wrapper: emit a structured event.

    If logger is None (common in tests / smoke), this is a no-op — audit
    logging is opt-in.
    """
    if logger is None:
        return
    extra_dict: dict[str, Any] = {"event": event}
    if session_id is not None:
        extra_dict["session_id"] = session_id
    if turn is not None:
        extra_dict["turn"] = turn
    if tool is not None:
        extra_dict["tool"] = tool
    if decision is not None:
        extra_dict["decision"] = decision
    if path is not None:
        extra_dict["path"] = path
    extra_dict.update(extra)
    logger.info(msg, extra=extra_dict)

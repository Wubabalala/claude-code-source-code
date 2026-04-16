"""Tests for agent.hooks — Phase 6 event-driven shell hooks."""
from __future__ import annotations

import json
import sys

import pytest

from agent.hooks import (
    EVENT_POST_TOOL_USE,
    EVENT_PRE_TOOL_USE,
    EVENT_SESSION_START,
    fire_hooks,
)


def test_fire_hook_runs_shell_command(tmp_path):
    marker = tmp_path / "marker.txt"
    fire_hooks(EVENT_SESSION_START,
               [f'echo "fired" > "{marker}"'],
               {"session_id": "x"})
    assert marker.exists()
    assert "fired" in marker.read_text()


def test_fire_hook_timeout_does_not_block(monkeypatch):
    """A hook that exceeds timeout must not block the agent.

    Uses mock to avoid platform-specific child-process kill issues on Windows
    (cmd.exe shell=True doesn't always propagate SIGTERM to children).
    """
    import subprocess as sp
    import time

    def fake_run(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="slow", timeout=0.5)

    monkeypatch.setattr("agent.hooks.subprocess.run", fake_run)

    t0 = time.monotonic()
    fire_hooks(EVENT_SESSION_START, ["slow_command"], timeout=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 2, f"hook blocked for {elapsed:.1f}s"


def test_fire_hook_exit_0_allows():
    blocked = fire_hooks(EVENT_PRE_TOOL_USE,
                         ["python -c \"exit(0)\""],
                         {"tool_name": "bash"})
    assert blocked is False


def test_fire_hook_exit_2_blocks_pre_tool_use():
    blocked = fire_hooks(EVENT_PRE_TOOL_USE,
                         ["python -c \"exit(2)\""],
                         {"tool_name": "bash"})
    assert blocked is True


def test_fire_hook_exit_2_ignored_for_post_tool_use():
    """exit(2) only blocks PreToolUse, not PostToolUse."""
    blocked = fire_hooks(EVENT_POST_TOOL_USE,
                         ["python -c \"exit(2)\""],
                         {"tool_name": "bash"})
    assert blocked is False


def test_fire_hook_other_exit_warns_continues(capsys):
    blocked = fire_hooks(EVENT_PRE_TOOL_USE,
                         ["python -c \"exit(42)\""],
                         {"tool_name": "bash"})
    assert blocked is False
    err = capsys.readouterr().err
    assert "42" in err


def test_fire_hook_passes_json_stdin(tmp_path):
    out_file = tmp_path / "stdin.json"
    cmd = f'python -c "import sys,json; d=json.load(sys.stdin); open(r\'{out_file}\',\'w\').write(json.dumps(d))"'
    fire_hooks(EVENT_PRE_TOOL_USE, [cmd], {"tool_name": "bash", "x": 42})
    data = json.loads(out_file.read_text())
    assert data["tool_name"] == "bash"
    assert data["x"] == 42


def test_fire_hook_empty_config_is_noop():
    blocked = fire_hooks(EVENT_PRE_TOOL_USE, [], {"tool_name": "x"})
    assert blocked is False


def test_hook_audit_event_emitted():
    class _Logger:
        def __init__(self):
            self.events = []
        def info(self, msg, extra=None):
            self.events.append(dict(extra or {}))

    logger = _Logger()
    fire_hooks(EVENT_SESSION_START,
               ["python -c \"print('hi')\""],
               {"session_id": "s"},
               audit_logger=logger, session_id="s")
    hook_events = [e for e in logger.events if e.get("event") == "hook.fire"]
    assert len(hook_events) == 1
    assert hook_events[0]["hook_event"] == EVENT_SESSION_START
    assert hook_events[0]["exit_code"] == 0
    assert "stdout_size" in hook_events[0]


def test_hook_audit_does_not_log_raw_stdout():
    """Contract: audit only records sizes, not raw stdout content."""
    class _Logger:
        def __init__(self):
            self.events = []
        def info(self, msg, extra=None):
            self.events.append(dict(extra or {}))

    logger = _Logger()
    fire_hooks(EVENT_SESSION_START,
               ["python -c \"print('SECRET_VALUE_12345')\""],
               audit_logger=logger)
    event = [e for e in logger.events if e.get("event") == "hook.fire"][0]
    assert "SECRET_VALUE_12345" not in json.dumps(event)
    assert event["stdout_size"] > 0

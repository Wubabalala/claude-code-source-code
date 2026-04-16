"""Event-driven shell hooks — Phase 6.

Fires shell commands on agent lifecycle events. Config via TOML [hooks].

Events (Phase 6 initial set):
  - SessionStart: fires once at REPL startup
  - PreToolUse:   fires before each tool execution; exit(2) blocks
  - PostToolUse:  fires after each tool execution; exit code ignored

Exit code semantics:
  0   = OK
  2   = BLOCK (PreToolUse only — tool_result with is_error=True)
  other = warn + continue

Stdout/stderr NOT injected into model context (security). Audit log
records exit_code, duration_ms, stdout_size, stderr_size only; raw
output never logged (consistent with agent.audit's path-hashing /
no-content-logging policy). Stderr first 200 bytes saved in msg for
diagnostics.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Optional


# Event name constants
EVENT_SESSION_START = "SessionStart"
EVENT_PRE_TOOL_USE = "PreToolUse"
EVENT_POST_TOOL_USE = "PostToolUse"

# Exit code that means "block this tool invocation"
EXIT_BLOCK = 2


def fire_hooks(
    event: str,
    commands: list[str],
    context: Optional[dict[str, Any]] = None,
    *,
    timeout: float = 5.0,
    audit_logger: Any = None,
    session_id: Optional[str] = None,
) -> bool:
    """Run all shell commands for an event. Returns True if any command
    requested a block (exit code 2 on PreToolUse).

    Commands receive JSON context on stdin. Stdout/stderr are captured but
    NOT returned to the caller or logged in full — only sizes are audited.
    """
    if not commands:
        return False

    blocked = False
    ctx_json = json.dumps(context or {}, ensure_ascii=False)

    for cmd in commands:
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                input=ctx_json,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            duration_ms = int((time.monotonic() - t0) * 1000)
            exit_code = -1  # sentinel: timeout
            result = None
        except Exception:
            duration_ms = int((time.monotonic() - t0) * 1000)
            exit_code = -2  # sentinel: error
            result = None

        stdout_size = len(result.stdout) if result and result.stdout else 0
        stderr_size = len(result.stderr) if result and result.stderr else 0
        stderr_head = (result.stderr[:200] if result and result.stderr else "")

        if audit_logger is not None:
            from agent.audit import emit
            emit(
                audit_logger,
                "hook.fire",
                session_id=session_id,
                hook_event=event,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout_size=stdout_size,
                stderr_size=stderr_size,
                msg=stderr_head or None,
            )

        if event == EVENT_PRE_TOOL_USE and exit_code == EXIT_BLOCK:
            blocked = True

        if exit_code not in (0, EXIT_BLOCK) and exit_code >= 0:
            import sys
            print(f"[hook] warn: {event} command exited {exit_code}", file=sys.stderr)

    return blocked

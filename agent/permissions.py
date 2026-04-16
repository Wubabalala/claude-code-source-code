"""Permission utilities — Phase 3 safety layer.

Two responsibilities:

  1. `is_hard_denied(path)` — single source of truth for the hard denylist
     (.git, .ssh, dotfiles, secret patterns). Tools consult this; hits become
     DENY decisions that never reach the user ASK prompt.

  2. `prompt_user_for_permission(outcome)` — synchronous REPL Y/N prompt for
     ASK decisions. Returns only ALLOW or DENY (never ASK); fail-closed in
     non-TTY / EOF / KeyboardInterrupt scenarios.

Contract summary (see plan Phase 3):
  - Hard denylist is immutable and not configurable at runtime — it
    represents the safety floor even under ASK yes.
  - GREP_EXCLUDE_GLOBS mirrors the denylist for ripgrep `--glob '!...'`
    injection so that wide-directory grep cannot leak secret file contents.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.tools import PermissionOutcome


# ---------------------------------------------------------------------------
# Hard denylist (single source of truth)
# ---------------------------------------------------------------------------

HARD_DENY_DIR_COMPONENTS: frozenset[str] = frozenset({".git", ".ssh"})

HARD_DENY_FILENAMES: frozenset[str] = frozenset({
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".profile",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
})

HARD_DENY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.env$"),
    re.compile(r"^\.env\..+"),
    re.compile(r".*credentials.*", re.IGNORECASE),
    re.compile(r".*\.pem$", re.IGNORECASE),
    re.compile(r".*\.key$", re.IGNORECASE),
    re.compile(r"^id_rsa($|\..+)"),
    re.compile(r"^id_ed25519($|\..+)"),
)

# rg `--glob` exclusions — mirror denylist for wide-directory searches.
GREP_EXCLUDE_GLOBS: tuple[str, ...] = (
    "!.git",
    "!.git/**",
    "!.ssh",
    "!.ssh/**",
    "!.env",
    "!.env.*",
    "!*credentials*",
    "!*.pem",
    "!*.key",
    "!id_rsa*",
    "!id_ed25519*",
    "!.bashrc",
    "!.bash_profile",
    "!.zshrc",
    "!.profile",
    "!.git-credentials",
    "!.npmrc",
    "!.pypirc",
)


def is_hard_denied(path: str | Path) -> bool:
    """Return True when `path` lies inside the hard denylist.

    Matching happens AFTER `expanduser().resolve(strict=False)` so that
    symlinks or `..` traversal into a deny zone are caught. On resolution
    failure (OSError / RuntimeError / ValueError), fail closed → True.

    Matching layers:
      1. Any directory component (lowercased) ∈ HARD_DENY_DIR_COMPONENTS
      2. Filename (lowercased) ∈ HARD_DENY_FILENAMES
      3. Filename matches any HARD_DENY_PATTERNS regex
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True

    parts_lower = [p.lower() for p in resolved.parts]
    if any(p in HARD_DENY_DIR_COMPONENTS for p in parts_lower):
        return True

    name_lower = resolved.name.lower()
    if name_lower in HARD_DENY_FILENAMES:
        return True

    for pattern in HARD_DENY_PATTERNS:
        if pattern.match(name_lower):
            return True
    return False


# ---------------------------------------------------------------------------
# Synchronous Y/N prompt
# ---------------------------------------------------------------------------


def prompt_user_for_permission(
    outcome: "PermissionOutcome",
    *,
    audit_logger=None,
) -> str:
    """Block on stdin for Y/N approval. Returns ALLOW or DENY only.

    Fail-closed on every ambiguous signal:
      - sys.stdin.isatty() == False → DENY without invoking input()
      - EOFError / KeyboardInterrupt → DENY
      - Any input other than y / yes (case-insensitive, stripped) → DENY

    Phase 4: emits `permission.prompted` audit event recording the ASK
    interaction (owner: permissions.py, contract L). The final effective
    decision event (`permission.decision`) is emitted by the caller (loop)
    — not here — to keep owner single-sourced.
    """
    from agent.tools import PermissionDecision  # local import avoids cycle
    from agent.audit import emit, hash_path

    target_hash = hash_path(outcome.target) if outcome.target else None

    if not sys.stdin.isatty():
        emit(
            audit_logger,
            "permission.prompted",
            tool=outcome.tool_name,
            path=target_hash,
            user_answer="(non-tty)",
            op_type=outcome.op_type,
        )
        return PermissionDecision.DENY

    print(
        f"\n[PERMISSION] tool={outcome.tool_name} "
        f"target={outcome.target} op={outcome.op_type}\n"
        f"  risk: {outcome.risk}"
    )
    try:
        answer = input("Allow? (y/N): ")
    except (EOFError, KeyboardInterrupt):
        print()  # newline after ^C so the REPL prompt lands cleanly
        emit(
            audit_logger,
            "permission.prompted",
            tool=outcome.tool_name,
            path=target_hash,
            user_answer="(interrupt)",
            op_type=outcome.op_type,
        )
        return PermissionDecision.DENY

    emit(
        audit_logger,
        "permission.prompted",
        tool=outcome.tool_name,
        path=target_hash,
        user_answer=answer.strip()[:10],  # truncate to avoid logging long input
        op_type=outcome.op_type,
    )

    if answer.strip().lower() in ("y", "yes"):
        return PermissionDecision.ALLOW
    return PermissionDecision.DENY

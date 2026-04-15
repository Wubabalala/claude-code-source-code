"""Tool interface and built-in tools.

Phase 1 design (see docs/plans/2026-04-07-phase1-skeleton-design.md).
Phase 3 revisions (see plan compiled-squishing-stallman.md):

  - check_permissions() returns PermissionOutcome (structured), not a bare string.
  - Three new static @property on Tool base: reads_from_filesystem /
    writes_to_filesystem / destroys_data. No defaults — subclasses MUST
    declare, otherwise instantiation fails.
  - Base default check_permissions() derives decision from the static
    metadata (fail-closed: writes or destroys → ASK).
  - BashTool: blacklist replaced by conservative whitelist-then-ASK policy.
  - Hard denylist lives in agent/permissions.py (single source of truth).
"""
import re
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.permissions import GREP_EXCLUDE_GLOBS, is_hard_denied


# ---------------------------------------------------------------------------
# Permission types
# ---------------------------------------------------------------------------


class PermissionDecision:
    """String constants — kept simple, not an Enum, to match Anthropic SDK style."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PermissionOutcome:
    """Structured result of Tool.check_permissions().

    The loop dispatches on `decision`:
      - ALLOW → execute tool
      - DENY → return tool_result with is_error=True, do NOT prompt user
      - ASK → call prompt_user_for_permission(outcome)

    tool_name / target / op_type / risk populate the REPL ASK prompt; ALLOW
    and DENY outcomes may leave them blank.
    """

    decision: str
    tool_name: str = ""
    target: str = ""
    op_type: str = ""
    risk: str = ""


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


class ToolResult(BaseModel):
    """Unified return type from every tool.

    `output` goes to the model. `metadata` is for observers (logs, hooks).
    The model never sees metadata.
    """

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool base class
# ---------------------------------------------------------------------------


class Tool(ABC):
    """Base class for all tools.

    Capability declaration is split across two layers:

      1. STATIC metadata (@property, input-independent):
         reads_from_filesystem / writes_to_filesystem / destroys_data
         Class-level capability ceiling. Required — no defaults. Used by
         base check_permissions to fail-closed on forgetful subclasses.

      2. DYNAMIC per-call attributes (instance methods, input-dependent):
         is_read_only(input) / is_concurrency_safe(input)
         These refine "this specific invocation" for Phase 5 concurrency
         scheduling. Defaults fail-closed (False = "not proven safe").

      3. DYNAMIC permission decision (instance method):
         check_permissions(input) → PermissionOutcome
         Returns ALLOW/ASK/DENY with prompt context. Default implementation
         uses static metadata to drive ASK for write/destroy tools.
    """

    # —— 1. Identity ——
    name: str  # subclasses set as class attribute

    @abstractmethod
    def description(self) -> str:
        """Description shown to the model. MUST be a static string (no f-strings
        with runtime values), otherwise prompt cache fragments. See prompt.py."""

    # —— 2. Input schema ——
    @property
    @abstractmethod
    def input_model(self) -> type[BaseModel]:
        """Pydantic model for input validation."""

    # —— 3. Static capability metadata (required; no defaults) ——
    @property
    @abstractmethod
    def reads_from_filesystem(self) -> bool:
        """Static: whether this tool class may read the filesystem."""

    @property
    @abstractmethod
    def writes_to_filesystem(self) -> bool:
        """Static: whether this tool class may modify the filesystem."""

    @property
    @abstractmethod
    def destroys_data(self) -> bool:
        """Static: whether this tool class may cause irreversible destruction."""

    # —— 4. Permission check (default: fail-closed by metadata) ——
    def check_permissions(self, input: BaseModel) -> PermissionOutcome:
        """Default: fail-closed per static metadata.

        Subclasses override for finer control (hard denylist, path-specific
        rules, etc.). The default exists so a forgetful author who declared
        writes_to_filesystem=True but did not override still lands in ASK,
        not ALLOW.
        """
        if self.destroys_data or self.writes_to_filesystem:
            op_type = "destructive" if self.destroys_data else "write"
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                tool_name=self.name,
                target=str(input),
                op_type=op_type,
                risk=f"{self.name} declares {op_type}; default ASK (override to narrow)",
            )
        return PermissionOutcome(
            decision=PermissionDecision.ALLOW,
            tool_name=self.name,
        )

    # —— 5. Dynamic per-call attributes (fail-closed defaults) ——
    def is_read_only(self, input: BaseModel) -> bool:
        return False

    def is_concurrency_safe(self, input: BaseModel) -> bool:
        return False

    # —— 6. Execution ——
    @abstractmethod
    def execute(self, input: BaseModel) -> ToolResult:
        """Synchronous execution. Phase 5 will async-ify."""

    # —— 7. Anthropic API schema conversion ——
    def to_anthropic_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description(),
            "input_schema": self.input_model.model_json_schema(),
        }


# ============================================================================
# ReadFileTool — the "eyes"
# ============================================================================


class ReadFileInput(BaseModel):
    file_path: str
    offset: int = Field(0, ge=0)
    limit: int = Field(2000, ge=1)


class ReadFileTool(Tool):
    name = "read_file"

    reads_from_filesystem = True
    writes_to_filesystem = False
    destroys_data = False

    def description(self) -> str:
        return (
            "Read a file from the local filesystem. "
            "Supports offset/limit for large files. "
            "Returns content with line numbers (cat -n format)."
        )

    @property
    def input_model(self):
        return ReadFileInput

    def is_read_only(self, input):
        return True

    def is_concurrency_safe(self, input):
        return True

    def check_permissions(self, input: ReadFileInput) -> PermissionOutcome:
        """Hard-deny sensitive paths; otherwise ALLOW."""
        if is_hard_denied(input.file_path):
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                tool_name=self.name,
                target=input.file_path,
                op_type="read",
                risk="hard-denied sensitive path",
            )
        return PermissionOutcome(
            decision=PermissionDecision.ALLOW,
            tool_name=self.name,
        )

    def execute(self, input: ReadFileInput) -> ToolResult:
        path = Path(input.file_path)
        if not path.exists():
            return ToolResult(output=f"File not found: {input.file_path}", is_error=True)
        if not path.is_file():
            return ToolResult(output=f"Not a file: {input.file_path}", is_error=True)

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(output=f"Read failed: {e}", is_error=True)

        lines = content.splitlines()
        selected = lines[input.offset : input.offset + input.limit]
        numbered = "\n".join(
            f"{input.offset + i + 1:6d}\t{line}"
            for i, line in enumerate(selected)
        )
        return ToolResult(
            output=numbered,
            metadata={
                "total_lines": len(lines),
                "returned_lines": len(selected),
                "truncated": len(lines) > input.offset + input.limit,
            },
        )


# ============================================================================
# GrepTool — the "index card"
# ============================================================================


class GrepInput(BaseModel):
    pattern: str
    path: str = "."
    glob: str | None = None
    case_insensitive: bool = False
    max_results: int = 100


class GrepTool(Tool):
    name = "grep"

    reads_from_filesystem = True
    writes_to_filesystem = False
    destroys_data = False

    def description(self) -> str:
        return (
            "Search for a pattern in files using ripgrep. "
            "Supports regex, glob filtering, case sensitivity. "
            "Returns matching lines in 'file:line:content' format. "
            "Prefer this over reading many files individually."
        )

    @property
    def input_model(self):
        return GrepInput

    def is_read_only(self, input):
        return True

    def is_concurrency_safe(self, input):
        return True

    def check_permissions(self, input: GrepInput) -> PermissionOutcome:
        """Hard-deny if the search root itself is a sensitive path.

        Wide-directory recursion into sensitive files is handled separately
        via GREP_EXCLUDE_GLOBS injected into rg at execute() time.
        """
        if is_hard_denied(input.path):
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                tool_name=self.name,
                target=input.path,
                op_type="read",
                risk="hard-denied sensitive search root",
            )
        return PermissionOutcome(
            decision=PermissionDecision.ALLOW,
            tool_name=self.name,
        )

    def execute(self, input: GrepInput) -> ToolResult:
        cmd = ["rg", "--line-number", "--no-heading"]
        if input.case_insensitive:
            cmd.append("-i")
        # Hard-denylist exclusions — always injected, even when user supplies
        # their own --glob. Uses `--iglob` (case-insensitive) so that
        # AWS_Credentials.yaml / DEPLOY.KEY match the lowercase patterns,
        # consistent with is_hard_denied's re.IGNORECASE behavior.
        for g in GREP_EXCLUDE_GLOBS:
            cmd.extend(["--iglob", g])
        if input.glob:
            cmd.extend(["--glob", input.glob])
        cmd.extend(["--max-count", str(input.max_results)])
        cmd.append(input.pattern)
        cmd.append(input.path)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return ToolResult(output="Grep timeout (30s)", is_error=True)
        except FileNotFoundError:
            return ToolResult(
                output="ripgrep (rg) not installed. See https://github.com/BurntSushi/ripgrep",
                is_error=True,
            )

        if result.returncode == 1:
            return ToolResult(output="No matches found.")
        if result.returncode != 0:
            return ToolResult(output=f"Grep error: {result.stderr}", is_error=True)

        lines = result.stdout.splitlines()[: input.max_results]
        return ToolResult(
            output="\n".join(lines),
            metadata={"match_count": len(lines)},
        )


# ============================================================================
# BashTool — the "hands" (most dangerous tool)
# ============================================================================


class BashInput(BaseModel):
    command: str
    timeout: int = 60


_READ_ONLY_CMDS: frozenset[str] = frozenset({
    "ls", "pwd", "cat", "head", "tail", "echo", "which", "whoami", "date",
    "file", "stat", "wc", "sort", "uniq",
})

# Sub-commands of `git` that are safe (read-only)
_READ_ONLY_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "log", "diff", "status", "show", "branch", "blame", "rev-parse", "rev-list",
})

# Nested interpreters — hard-denied because their arguments are code in another
# language (not statically analyzable without a full parser for each language).
# See plan contract C rule 3.
_NESTED_INTERPRETERS: frozenset[str] = frozenset({
    "bash", "sh", "zsh", "python", "python3", "perl", "ruby", "node", "eval",
})

# Shell metacharacters that defeat static token analysis. The redirects > / >>
# are intentionally excluded — they appear as their own shlex tokens and are
# handled by path extraction.
_SHELL_METACHAR_RE = re.compile(r"[;&|`<\n]|\$\(")

# Writing commands whose non-flag args are path candidates.
_WRITE_COMMANDS: frozenset[str] = frozenset({
    "rm", "mv", "cp", "dd", "chmod", "chown", "touch", "ln", "install",
    "mkdir", "rmdir",
})

# `find` flags that imply write/side-effect operations.
_FIND_UNSAFE_FLAGS: frozenset[str] = frozenset({
    "-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprintf", "-fprint",
})


def _has_shell_metacharacters(cmd: str) -> bool:
    return bool(_SHELL_METACHAR_RE.search(cmd))


def _is_nested_shell(tokens: list[str]) -> bool:
    """Return True for `bash -c ...` / `python -c ...` / `node -e ...` etc."""
    if not tokens:
        return False
    if tokens[0] not in _NESTED_INTERPRETERS:
        return False
    # `eval` is always a nested shell regardless of flags.
    if tokens[0] == "eval":
        return True
    return any(t in ("-c", "-e", "--command", "--eval") for t in tokens[1:])


def _is_readonly_first_token(tokens: list[str]) -> bool:
    """Check whether `tokens` starts with a known read-only command."""
    if not tokens:
        return False
    if tokens[0] in _READ_ONLY_CMDS:
        return True
    if tokens[0] == "git" and len(tokens) >= 2 and tokens[1] in _READ_ONLY_GIT_SUBCOMMANDS:
        return True
    if tokens[0] == "find":
        return not any(t in _FIND_UNSAFE_FLAGS for t in tokens[1:])
    return False


def _path_from_arg(arg: str) -> list[str]:
    """Return candidate path strings extracted from a single token.

    Handles the `key=value` convention used by `dd` (of=PATH, if=PATH) and
    some other tools. Returns both the full token and the post-`=` fragment
    so that either form can be matched against is_hard_denied.
    """
    if arg.startswith("-"):
        return []
    out = [arg]
    if "=" in arg:
        _, _, tail = arg.partition("=")
        if tail and tail not in out:
            out.append(tail)
    return out


def _extract_path_candidates(tokens: list[str]) -> list[str]:
    """Collect path-like tokens for is_hard_denied checking.

    Focused on common write / read vectors; not exhaustive (the gate is
    DENY on hit, ASK otherwise — missed candidates default to ASK which is
    acceptable for unknowns, but hard-denied path patterns we care about
    should be caught here).
    """
    candidates: list[str] = []

    def _add(arg: str) -> None:
        for p in _path_from_arg(arg):
            if p not in candidates:
                candidates.append(p)

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]

        # Redirections: `>` / `>>` token, next token is the target path
        if tok in (">", ">>") and i + 1 < n:
            _add(tokens[i + 1])
            i += 2
            continue

        # Write commands: all non-flag args (with key=value expansion)
        if tok in _WRITE_COMMANDS:
            for arg in tokens[i + 1:]:
                _add(arg)
            i = n
            continue

        # `sed -i` / `sed --in-place` — last non-flag is the path
        if tok == "sed":
            has_inplace = any(
                t == "-i" or t.startswith("-i") or t == "--in-place"
                for t in tokens[i + 1:]
            )
            if has_inplace:
                non_flag = [t for t in tokens[i + 1:] if not t.startswith("-")]
                if non_flag:
                    _add(non_flag[-1])
            i = n
            continue

        # `tee` (optionally -a) — following token(s) are targets
        if tok == "tee":
            for arg in tokens[i + 1:]:
                _add(arg)
            i = n
            continue

        # Read-only commands that still need path checking (cat ~/.ssh/id_rsa)
        if tok in {"cat", "head", "tail", "ls", "file", "stat", "wc"}:
            for arg in tokens[i + 1:]:
                _add(arg)
            i = n
            continue

        i += 1

    # Also sweep all tokens that look path-ish (contain / or start with .)
    for tok in tokens:
        if "/" in tok or (tok.startswith(".") and not tok.startswith("-")):
            _add(tok)

    return candidates


class BashTool(Tool):
    name = "bash"

    reads_from_filesystem = True
    writes_to_filesystem = True
    destroys_data = True

    def description(self) -> str:
        return (
            "Execute a bash command. "
            "DANGEROUS: write operations modify the filesystem. "
            "Prefer read_file and grep when possible. "
            "Always quote paths with spaces."
        )

    @property
    def input_model(self):
        return BashInput

    def is_read_only(self, input: BashInput) -> bool:
        try:
            tokens = shlex.split(input.command)
        except ValueError:
            return False
        return _is_readonly_first_token(tokens)

    def is_concurrency_safe(self, input):
        return False

    def check_permissions(self, input: BashInput) -> PermissionOutcome:
        """Conservative policy (plan contract C).

        Rule order matters: hard-deny checks run BEFORE metacharacter ASK
        so that `echo x | tee ~/.bashrc` and similar don't downgrade a
        hard-denied target into a user-overridable ASK.

          1. shlex.split fails → ASK (can't analyze anything)
          2. nested interpreter (bash/python/... -c / -e) → DENY
          3. any path candidate is hard-denied → DENY (BEFORE metacharacter check)
          4. shell metacharacters → ASK (static analysis unsafe, but no
             hard-denied path was found in tokens — user decides)
          5. read-only first token → ALLOW (paths vetted in step 3 already)
          6. everything else → ASK
        """
        cmd = input.command

        # Rule 1: parseable?
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                tool_name=self.name,
                target=cmd,
                op_type="shell",
                risk="unparseable command (shlex)",
            )

        if not tokens:
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                tool_name=self.name,
                target=cmd,
                op_type="shell",
                risk="empty command",
            )

        # Rule 2: nested shell → hard-denied (another language's code in -c/-e)
        if _is_nested_shell(tokens):
            return PermissionOutcome(
                decision=PermissionDecision.DENY,
                tool_name=self.name,
                target=cmd,
                op_type="shell",
                risk="nested interpreter cannot be statically analyzed; "
                     "hard-denied to preserve safe-path guarantee",
            )

        # Rule 3: hard-denied path candidates → DENY. Runs BEFORE metachar
        # gate so that `... | tee ~/.bashrc` still lands in DENY and never
        # reaches the ASK override.
        for candidate in _extract_path_candidates(tokens):
            if is_hard_denied(candidate):
                return PermissionOutcome(
                    decision=PermissionDecision.DENY,
                    tool_name=self.name,
                    target=candidate,
                    op_type="shell",
                    risk=f"command touches hard-denied path: {candidate}",
                )

        # Rule 4: shell metacharacters → ASK
        if _has_shell_metacharacters(cmd):
            return PermissionOutcome(
                decision=PermissionDecision.ASK,
                tool_name=self.name,
                target=cmd,
                op_type="shell",
                risk="shell metacharacters detected; static analysis unsafe",
            )

        # Rule 5: read-only first token → ALLOW
        if _is_readonly_first_token(tokens):
            return PermissionOutcome(
                decision=PermissionDecision.ALLOW,
                tool_name=self.name,
            )

        # Rule 6: unknown / known-write → ASK
        return PermissionOutcome(
            decision=PermissionDecision.ASK,
            tool_name=self.name,
            target=cmd,
            op_type="write",
            risk="unproven read-only; user approval required",
        )

    def execute(self, input: BashInput) -> ToolResult:
        try:
            result = subprocess.run(
                input.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=input.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                output=f"Command timeout ({input.timeout}s)",
                is_error=True,
            )

        parts = []
        if result.stdout:
            parts.append(f"[stdout]\n{result.stdout}")
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        if not parts:
            parts.append("(no output)")
        parts.append(f"[exit code: {result.returncode}]")

        return ToolResult(
            output="\n\n".join(parts),
            is_error=(result.returncode != 0),
            metadata={"exit_code": result.returncode},
        )

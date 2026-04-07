"""Tool interface and built-in tools.

Phase 1 design (see docs/plans/2026-04-07-phase1-skeleton-design.md):
- Tool is an ABC with 6 dimensions of capability.
- Side-effect attributes are METHODS (not fields) because they may depend on input.
- Defaults are fail-closed: unknown safety = unsafe.
- check_permissions returns ALLOW/DENY/ASK; ASK is reserved for Phase 3.
"""
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PermissionDecision:
    """String constants — kept simple, not an Enum, to match Anthropic SDK style."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # Reserved for Phase 3


class ToolResult(BaseModel):
    """Unified return type from every tool.

    `output` goes to the model. `metadata` is for observers (logs, hooks).
    The model never sees metadata.
    """

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class Tool(ABC):
    """Base class for all tools. See docstring at top of file for design notes."""

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

    # —— 3. Permission check ——
    def check_permissions(self, input: BaseModel) -> str:
        """Default ALLOW. Subclasses override to add DENY rules."""
        return PermissionDecision.ALLOW

    # —— 4. Side-effect properties (fail-closed defaults) ——
    def is_read_only(self, input: BaseModel) -> bool:
        return False

    def is_concurrency_safe(self, input: BaseModel) -> bool:
        return False

    def is_destructive(self, input: BaseModel) -> bool:
        return False

    # —— 5. Execution ——
    @abstractmethod
    def execute(self, input: BaseModel) -> ToolResult:
        """Synchronous execution. Phase 5 will async-ify."""

    # —— 6. Anthropic API schema conversion ——
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

    def check_permissions(self, input: ReadFileInput) -> str:
        """Deny access to sensitive files/directories.

        Uses path normalization + component matching (not raw substring) to
        defeat simple bypasses like backslash vs forward slash or symlinks
        pointing into deny zones. This is defense in depth, not the primary
        security boundary — the real permission system lands in Phase 3.
        """
        try:
            resolved = Path(input.file_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return PermissionDecision.DENY  # fail-closed on resolution failure

        # Exact-component deny for well-known sensitive directories and files.
        # Lowercased because Windows filesystems are case-insensitive.
        parts_lower = {p.lower() for p in resolved.parts}
        DENY_COMPONENTS = {".git", ".ssh", ".env"}
        if parts_lower & DENY_COMPONENTS:
            return PermissionDecision.DENY

        # Filename-substring deny for credential bundles and key files.
        name_lower = resolved.name.lower()
        DENY_NAME_SUBSTRINGS = ("id_rsa", "id_ed25519", "credentials", "secrets", ".pem", ".key")
        if any(s in name_lower for s in DENY_NAME_SUBSTRINGS):
            return PermissionDecision.DENY

        return PermissionDecision.ALLOW

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

    def execute(self, input: GrepInput) -> ToolResult:
        cmd = ["rg", "--line-number", "--no-heading"]
        if input.case_insensitive:
            cmd.append("-i")
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


# Phase 1 minimum blacklist. Phase 3 will replace with a real command parser.
_DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    ":(){:|:&};:",  # fork bomb
    "mkfs",
    "dd if=",
    "> /dev/sda",
    "chmod -R 777 /",
    "| sh",         # curl http://... | sh
    "| bash",       # curl http://... | bash
    "sudo ",
]

_READ_ONLY_CMDS = {"ls", "pwd", "cat", "head", "tail", "echo", "which", "whoami", "date"}


class BashTool(Tool):
    name = "bash"

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
        first_token = input.command.strip().split()[0] if input.command.strip() else ""
        return first_token in _READ_ONLY_CMDS

    def is_concurrency_safe(self, input):
        return False

    def check_permissions(self, input: BashInput) -> str:
        cmd_lower = input.command.lower()
        for pattern in _DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                return PermissionDecision.DENY
        return PermissionDecision.ALLOW

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

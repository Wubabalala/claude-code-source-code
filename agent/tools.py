"""Tool interface and built-in tools.

Phase 1 design (see docs/plans/2026-04-07-phase1-skeleton-design.md):
- Tool is an ABC with 6 dimensions of capability.
- Side-effect attributes are METHODS (not fields) because they may depend on input.
- Defaults are fail-closed: unknown safety = unsafe.
- check_permissions returns ALLOW/DENY/ASK; ASK is reserved for Phase 3.
"""
from abc import ABC, abstractmethod
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
from pathlib import Path


class ReadFileInput(BaseModel):
    file_path: str
    offset: int = 0
    limit: int = 2000


_SENSITIVE_PATTERNS = [
    ".env",
    ".git/",
    "id_rsa",
    ".ssh/",
    "credentials",
    "secrets",
]


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
        path_lower = input.file_path.lower()
        if any(p in path_lower for p in _SENSITIVE_PATTERNS):
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

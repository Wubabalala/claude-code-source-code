# Phase 1 Skeleton Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a minimal viable code-repo-assistant agent in Python with Anthropic API: 4-file architecture (loop / tools / prompt / main), immutable state, content-block tool detection, 2 continue sites, 3 exit conditions, Static/Dynamic prompt split for cache.

**Architecture:** Translate the 6 lessons of Claude Code source learning into runnable Python. `loop.py` is the agent state machine, `tools.py` defines the Tool ABC + 3 built-in tools (read_file, grep, bash), `prompt.py` is the cache-aware prompt builder, `main.py` is the CLI REPL. Supporting files: `types.py` (shared dataclasses), `api.py` (Anthropic SDK wrapper).

**Tech Stack:** Python 3.10+ · Anthropic Python SDK · Pydantic v2 · pytest · ripgrep (CLI dependency for GrepTool)

**Design doc:** `docs/plans/2026-04-07-phase1-skeleton-design.md`

---

## Pre-flight: Conventions and Test Strategy

### Directory layout

```
agent/
├── __init__.py
├── types.py          # Task 1
├── tools.py          # Tasks 2–5
├── prompt.py         # Task 6
├── api.py            # Task 7
├── loop.py           # Tasks 8–10
├── main.py           # Task 11
├── requirements.txt  # Task 0
└── README.md         # Task 12
tests/
├── __init__.py
├── conftest.py       # Task 0
├── test_types.py
├── test_tools.py
├── test_prompt.py
├── test_api.py
├── test_loop.py
└── fixtures/
    ├── sample.txt
    └── sample.py
```

### Test strategy

- **Unit tests** for `types.py`, `tools.py`, `prompt.py`, `api.py` — no network, no API key needed.
- **Loop tests** use a `FakeAnthropicClient` (a real class, not `unittest.mock`) that returns canned responses by replaying a queue. This is much more readable than MagicMock and doesn't drift when the SDK changes.
- **End-to-end verification** in Task 12 uses the REAL API and is run manually (not in CI). It is the only place real API calls happen.

### Commit style

- One commit per Task (not per Step).
- Subject: `feat(agent): <task title>` or `test(agent): <task title>`.
- Stage explicit paths, never `git add -A`.

### Working directory

All paths in this plan are relative to `D:\tools\claude-code-source`. Use forward slashes in shell commands; the bash tool handles Windows paths.

---

## Task 0: Project Bootstrap

**Files:**
- Create: `agent/__init__.py`
- Create: `agent/requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/fixtures/sample.txt`
- Create: `tests/fixtures/sample.py`

**Step 1: Create empty package files**

```bash
mkdir -p agent tests/fixtures
```

Create `agent/__init__.py` (empty, marks as package).
Create `tests/__init__.py` (empty).

**Step 2: Write requirements.txt**

`agent/requirements.txt`:
```
anthropic>=0.40.0
pydantic>=2.0.0
pytest>=7.0.0
```

> Version numbers are based on training-data knowledge as of 2025-05. Verify latest stable with `pip index versions anthropic` before installing if accuracy matters.

**Step 3: Verify Python and install**

Run:
```bash
python --version
pip install -r agent/requirements.txt
```

Expected: Python 3.10+, all three packages installed.

**Step 4: Create test fixtures**

`tests/fixtures/sample.txt`:
```
line one
line two
line three
TODO: this is a marker
line five
```

`tests/fixtures/sample.py`:
```python
def hello():
    return "world"

def goodbye():
    return "moon"

# TODO: add more functions
```

**Step 5: Write conftest.py with shared helpers**

`tests/conftest.py`:
```python
"""Shared test fixtures and helpers."""
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
```

**Step 6: Verify pytest discovers tests**

Run:
```bash
cd D:/tools/claude-code-source && python -m pytest tests/ --collect-only
```

Expected: pytest runs with no errors (collects 0 tests, that's fine — we haven't written any yet).

**Step 7: Commit**

```bash
git add agent/__init__.py agent/requirements.txt tests/__init__.py tests/conftest.py tests/fixtures/
git commit -m "feat(agent): bootstrap Phase 1 project structure"
```

---

## Task 1: Shared Types (types.py)

**Files:**
- Create: `agent/types.py`
- Create: `tests/test_types.py`

**Step 1: Write the failing tests**

`tests/test_types.py`:
```python
"""Tests for agent.types — immutable State and AgentResult."""
import pytest
from dataclasses import replace, FrozenInstanceError
from agent.types import State, AgentResult


def test_state_is_frozen():
    """State must be immutable — direct mutation raises FrozenInstanceError."""
    state = State(
        messages=(),
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )
    with pytest.raises(FrozenInstanceError):
        state.turn = 2


def test_state_uses_replace_for_updates():
    """dataclasses.replace returns a NEW state, original unchanged."""
    state = State(
        messages=(),
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )
    new_state = replace(state, turn=2, transition_reason="tool_use")
    assert state.turn == 1
    assert new_state.turn == 2
    assert new_state.transition_reason == "tool_use"
    # Unchanged fields are preserved
    assert new_state.fallback_model_used is False


def test_state_messages_is_tuple():
    """messages must be a tuple, not a list — for compile-time immutability."""
    state = State(
        messages=({"role": "user", "content": "hi"},),
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )
    assert isinstance(state.messages, tuple)
    # Tuples have no append method
    assert not hasattr(state.messages, "append")


def test_agent_result_status_values():
    """AgentResult exposes the 3 valid exit statuses."""
    result = AgentResult(status="completed", messages=(), reason="done")
    assert result.status == "completed"
    # Other valid statuses
    assert AgentResult(status="max_turns", messages=(), reason="").status == "max_turns"
    assert AgentResult(status="model_error", messages=(), reason="").status == "model_error"
```

**Step 2: Run tests to verify they fail**

Run:
```bash
cd D:/tools/claude-code-source && python -m pytest tests/test_types.py -v
```

Expected: `ImportError: No module named 'agent.types'`.

**Step 3: Implement agent/types.py**

`agent/types.py`:
```python
"""Shared types for the Phase 1 agent.

Design notes:
- State is frozen (immutable) — every loop iteration constructs a new State.
- messages is a tuple, not a list, so accidental .append() raises immediately.
- AgentResult is the loop exit envelope, never an exception.
"""
from dataclasses import dataclass
from typing import Any, Literal


# Message is intentionally typed loosely as dict — Anthropic SDK uses dicts.
# A real type would be a TypedDict, but Phase 1 keeps it simple.
Message = dict[str, Any]


@dataclass(frozen=True)
class State:
    """Single-turn loop state. Rebuilt every iteration, never mutated."""

    messages: tuple[Message, ...]
    turn: int
    fallback_model_used: bool
    output_retries: int
    transition_reason: str  # "initial" | "tool_use" | "model_fallback" | "output_recovery"


@dataclass(frozen=True)
class AgentResult:
    """Loop exit envelope. Never raised — always returned."""

    status: Literal["completed", "max_turns", "model_error"]
    messages: tuple[Message, ...]
    reason: str
```

**Step 4: Run tests to verify they pass**

Run:
```bash
cd D:/tools/claude-code-source && python -m pytest tests/test_types.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add agent/types.py tests/test_types.py
git commit -m "feat(agent): add immutable State and AgentResult types"
```

---

## Task 2: Tool Base Interface (tools.py — part 1)

**Files:**
- Create: `agent/tools.py`
- Create: `tests/test_tools.py`

**Step 1: Write failing tests for the Tool ABC**

`tests/test_tools.py`:
```python
"""Tests for agent.tools."""
import pytest
from pydantic import BaseModel
from agent.tools import Tool, ToolResult, PermissionDecision


class _DummyInput(BaseModel):
    value: str


class _DummyTool(Tool):
    name = "dummy"

    def description(self) -> str:
        return "A dummy tool for testing."

    @property
    def input_model(self):
        return _DummyInput

    def execute(self, input: _DummyInput) -> ToolResult:
        return ToolResult(output=f"got {input.value}")


def test_tool_subclass_must_implement_abstract_methods():
    """Forgetting an abstract method should fail at instantiation."""
    class Incomplete(Tool):
        name = "incomplete"
        # Missing description, input_model, execute

    with pytest.raises(TypeError):
        Incomplete()


def test_tool_default_safety_attributes_are_fail_closed():
    """Defaults: not read-only, not concurrency-safe, not destructive."""
    t = _DummyTool()
    dummy_input = _DummyInput(value="x")
    assert t.is_read_only(dummy_input) is False
    assert t.is_concurrency_safe(dummy_input) is False
    assert t.is_destructive(dummy_input) is False


def test_tool_default_permission_is_allow():
    """Base check_permissions returns ALLOW. Subclasses override for stricter."""
    t = _DummyTool()
    assert t.check_permissions(_DummyInput(value="x")) == PermissionDecision.ALLOW


def test_tool_to_anthropic_schema_shape():
    """Schema must have name, description, input_schema fields."""
    t = _DummyTool()
    schema = t.to_anthropic_schema()
    assert schema["name"] == "dummy"
    assert schema["description"] == "A dummy tool for testing."
    assert "input_schema" in schema
    # Pydantic-generated JSON schema has 'properties'
    assert "properties" in schema["input_schema"]


def test_tool_result_defaults():
    """ToolResult defaults: not error, empty metadata."""
    r = ToolResult(output="hello")
    assert r.is_error is False
    assert r.metadata == {}


def test_permission_decision_values():
    """PermissionDecision exposes ALLOW / DENY / ASK."""
    assert PermissionDecision.ALLOW == "allow"
    assert PermissionDecision.DENY == "deny"
    assert PermissionDecision.ASK == "ask"
```

**Step 2: Run tests to verify they fail**

Run:
```bash
cd D:/tools/claude-code-source && python -m pytest tests/test_tools.py -v
```

Expected: ImportError.

**Step 3: Implement the Tool ABC in agent/tools.py**

`agent/tools.py` (initial version — only the base ABC; concrete tools come in tasks 3–5):
```python
"""Tool interface and built-in tools.

Phase 1 design (see docs/plans/2026-04-07-phase1-skeleton-design.md):
- Tool is an ABC with 6 dimensions of capability.
- Side-effect attributes are METHODS (not fields) because they may depend on input.
- Defaults are fail-closed: unknown safety = unsafe.
- check_permissions returns ALLOW/DENY/ASK; ASK is reserved for Phase 3.
"""
from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel


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
    metadata: dict[str, Any] = {}


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
```

**Step 4: Run tests to verify they pass**

Run:
```bash
cd D:/tools/claude-code-source && python -m pytest tests/test_tools.py -v
```

Expected: 6 passed.

**Step 5: Commit**

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "feat(agent): add Tool ABC with fail-closed defaults"
```

---

## Task 3: ReadFileTool (tools.py — part 2)

**Files:**
- Modify: `agent/tools.py` (append)
- Modify: `tests/test_tools.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_tools.py`:
```python
# ============================================================================
# ReadFileTool tests
# ============================================================================
from agent.tools import ReadFileTool, ReadFileInput
from tests.conftest import FIXTURES_DIR


def test_read_file_reads_existing_file():
    tool = ReadFileTool()
    result = tool.execute(ReadFileInput(file_path=str(FIXTURES_DIR / "sample.txt")))
    assert result.is_error is False
    assert "line one" in result.output
    assert "line two" in result.output
    # Output should have line numbers (cat -n format)
    assert "1\t" in result.output or "     1\t" in result.output


def test_read_file_missing_file_returns_error():
    tool = ReadFileTool()
    result = tool.execute(ReadFileInput(file_path="/nonexistent/path/xyz.txt"))
    assert result.is_error is True
    assert "not found" in result.output.lower()


def test_read_file_respects_offset_and_limit():
    tool = ReadFileTool()
    result = tool.execute(ReadFileInput(
        file_path=str(FIXTURES_DIR / "sample.txt"),
        offset=1,
        limit=2,
    ))
    assert result.is_error is False
    assert "line two" in result.output
    assert "line three" in result.output
    assert "line one" not in result.output


def test_read_file_denies_sensitive_paths():
    tool = ReadFileTool()
    for sensitive in [".env", "/home/user/.ssh/id_rsa", "secret/credentials.json", ".git/config"]:
        decision = tool.check_permissions(ReadFileInput(file_path=sensitive))
        assert decision == PermissionDecision.DENY, f"Should deny: {sensitive}"


def test_read_file_allows_normal_paths():
    tool = ReadFileTool()
    decision = tool.check_permissions(ReadFileInput(file_path="src/main.py"))
    assert decision == PermissionDecision.ALLOW


def test_read_file_is_read_only_and_concurrency_safe():
    tool = ReadFileTool()
    dummy = ReadFileInput(file_path="x")
    assert tool.is_read_only(dummy) is True
    assert tool.is_concurrency_safe(dummy) is True
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_tools.py -v -k "read_file"
```

Expected: ImportError on ReadFileTool / ReadFileInput.

**Step 3: Append ReadFileTool to agent/tools.py**

Append to `agent/tools.py`:
```python
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
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_tools.py -v -k "read_file"
```

Expected: 6 passed.

**Step 5: Commit**

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "feat(agent): add ReadFileTool with sensitive-path deny rules"
```

---

## Task 4: GrepTool (tools.py — part 3)

**Files:**
- Modify: `agent/tools.py` (append)
- Modify: `tests/test_tools.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_tools.py`:
```python
# ============================================================================
# GrepTool tests
# ============================================================================
import shutil
from agent.tools import GrepTool, GrepInput

_HAS_RG = shutil.which("rg") is not None


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")
def test_grep_finds_matches():
    tool = GrepTool()
    result = tool.execute(GrepInput(
        pattern="TODO",
        path=str(FIXTURES_DIR),
    ))
    assert result.is_error is False
    assert "TODO" in result.output


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")
def test_grep_no_matches_returns_friendly_message():
    tool = GrepTool()
    result = tool.execute(GrepInput(
        pattern="ZZZNONEXISTENTPATTERNZZZ",
        path=str(FIXTURES_DIR),
    ))
    assert result.is_error is False
    assert "no matches" in result.output.lower()


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")
def test_grep_respects_glob_filter():
    tool = GrepTool()
    result = tool.execute(GrepInput(
        pattern="hello",
        path=str(FIXTURES_DIR),
        glob="*.py",
    ))
    assert result.is_error is False
    assert "hello" in result.output


def test_grep_is_read_only_and_concurrency_safe():
    tool = GrepTool()
    dummy = GrepInput(pattern="x")
    assert tool.is_read_only(dummy) is True
    assert tool.is_concurrency_safe(dummy) is True
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_tools.py -v -k "grep"
```

Expected: ImportError.

**Step 3: Append GrepTool to agent/tools.py**

Append to `agent/tools.py`:
```python
# ============================================================================
# GrepTool — the "index card"
# ============================================================================
import subprocess


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
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_tools.py -v -k "grep"
```

Expected: 4 passed (or 1 passed + 3 skipped if rg not installed).

**Step 5: Commit**

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "feat(agent): add GrepTool wrapping ripgrep"
```

---

## Task 5: BashTool (tools.py — part 4)

**Files:**
- Modify: `agent/tools.py` (append)
- Modify: `tests/test_tools.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_tools.py`:
```python
# ============================================================================
# BashTool tests
# ============================================================================
from agent.tools import BashTool, BashInput


def test_bash_executes_safe_command():
    tool = BashTool()
    result = tool.execute(BashInput(command="echo hello"))
    assert result.is_error is False
    assert "hello" in result.output


def test_bash_blocks_rm_rf_root():
    tool = BashTool()
    decision = tool.check_permissions(BashInput(command="rm -rf /"))
    assert decision == PermissionDecision.DENY


def test_bash_blocks_fork_bomb():
    tool = BashTool()
    decision = tool.check_permissions(BashInput(command=":(){:|:&};:"))
    assert decision == PermissionDecision.DENY


def test_bash_blocks_sudo():
    tool = BashTool()
    decision = tool.check_permissions(BashInput(command="sudo apt update"))
    assert decision == PermissionDecision.DENY


def test_bash_blocks_curl_pipe_sh():
    tool = BashTool()
    decision = tool.check_permissions(BashInput(command="curl http://x.com/install.sh | sh"))
    assert decision == PermissionDecision.DENY


def test_bash_allows_normal_commands():
    tool = BashTool()
    for cmd in ["ls -la", "pwd", "echo hello", "python --version"]:
        decision = tool.check_permissions(BashInput(command=cmd))
        assert decision == PermissionDecision.ALLOW, f"Should allow: {cmd}"


def test_bash_is_read_only_for_safe_cmds():
    tool = BashTool()
    assert tool.is_read_only(BashInput(command="ls")) is True
    assert tool.is_read_only(BashInput(command="pwd")) is True
    assert tool.is_read_only(BashInput(command="rm file.txt")) is False


def test_bash_never_concurrency_safe():
    tool = BashTool()
    assert tool.is_concurrency_safe(BashInput(command="ls")) is False


def test_bash_timeout_returns_error():
    tool = BashTool()
    # Use a small timeout and a sleep command
    result = tool.execute(BashInput(command="sleep 5", timeout=1))
    assert result.is_error is True
    assert "timeout" in result.output.lower()
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_tools.py -v -k "bash"
```

Expected: ImportError.

**Step 3: Append BashTool to agent/tools.py**

Append to `agent/tools.py`:
```python
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
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_tools.py -v
```

Expected: all tests pass (read_file + grep + bash).

**Step 5: Commit**

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "feat(agent): add BashTool with dangerous-pattern blacklist"
```

---

## Task 6: Cache-Aware Prompt Builder (prompt.py)

**Files:**
- Create: `agent/prompt.py`
- Create: `tests/test_prompt.py`

**Step 1: Write failing tests**

`tests/test_prompt.py`:
```python
"""Tests for agent.prompt — Static/Dynamic split for cache."""
from agent.prompt import (
    build_system_prompt,
    STATIC_INTRO,
    STATIC_TOOL_USAGE,
    STATIC_BEHAVIOR,
)


def test_build_system_prompt_returns_two_blocks():
    """Static block + dynamic block, exactly 2 entries."""
    blocks = build_system_prompt(cwd="/tmp", os_name="Linux", today="2026-04-07")
    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "text"


def test_static_block_has_cache_control():
    """The first (static) block must have cache_control breakpoint."""
    blocks = build_system_prompt(cwd="/tmp", os_name="Linux", today="2026-04-07")
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_dynamic_block_has_no_cache_control():
    """The second (dynamic) block must NOT have cache_control."""
    blocks = build_system_prompt(cwd="/tmp", os_name="Linux", today="2026-04-07")
    assert "cache_control" not in blocks[1]


def test_static_block_is_byte_identical_across_calls():
    """CRITICAL: same static text every call. Otherwise cache fragments."""
    blocks_a = build_system_prompt(cwd="/tmp", os_name="Linux", today="2026-04-07")
    blocks_b = build_system_prompt(cwd="/home", os_name="Darwin", today="2030-12-31")
    # Static block (index 0) must be identical
    assert blocks_a[0]["text"] == blocks_b[0]["text"]
    # Dynamic block (index 1) must differ
    assert blocks_a[1]["text"] != blocks_b[1]["text"]


def test_dynamic_block_contains_runtime_values():
    """The dynamic block must mention cwd, os, today."""
    blocks = build_system_prompt(cwd="/tmp/specialdir", os_name="Linux", today="2026-04-07")
    dyn = blocks[1]["text"]
    assert "/tmp/specialdir" in dyn
    assert "Linux" in dyn
    assert "2026-04-07" in dyn


def test_static_constants_have_no_format_placeholders():
    """STATIC_* constants must contain no { or } that look like f-string traces."""
    # f-strings with runtime values would defeat caching. This is a sanity check
    # that the constants are pure strings.
    for const in [STATIC_INTRO, STATIC_TOOL_USAGE, STATIC_BEHAVIOR]:
        assert isinstance(const, str)
        # Allow markdown { or } only in code blocks, but verify no obvious f-string traces
        # like {today} or {cwd}
        for forbidden in ["{today}", "{cwd}", "{os_name}", "{user}", "{date}"]:
            assert forbidden not in const, f"Static constant has runtime placeholder: {forbidden}"


def test_static_block_mentions_three_tools():
    """Sanity: the static section describes read_file, grep, bash."""
    blocks = build_system_prompt(cwd="/tmp", os_name="Linux", today="2026-04-07")
    static = blocks[0]["text"]
    assert "read_file" in static
    assert "grep" in static
    assert "bash" in static
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_prompt.py -v
```

Expected: ImportError.

**Step 3: Implement agent/prompt.py**

`agent/prompt.py`:
```python
"""Cache-aware system prompt builder.

Design notes (see docs/plans/2026-04-07-phase1-skeleton-design.md, Section 4):
- Static section is module-level constant strings — byte-for-byte identical
  across all users, all sessions, all time. This is what makes prompt cache
  work.
- Dynamic section is built per-call. It comes AFTER the cache breakpoint, so
  changes there do not invalidate the static cache.
- NEVER add f-strings or if-else to the static constants. N conditions create
  2^N cache fragments.
"""

DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


# ============================================================================
# Static section — byte-identical forever
# ============================================================================

STATIC_INTRO = """\
You are a code repository assistant. You help users navigate, understand, \
and modify codebases by reading files, searching for patterns, and \
executing commands.\
"""

STATIC_TOOL_USAGE = """\
# Tools

You have access to three tools:

- **read_file**: Read a file's content. Use for understanding what a specific \
file does. Supports offset/limit for large files.

- **grep**: Search for patterns across files using regex. Use this BEFORE \
read_file when you don't know which file to look at. Prefer grep over reading \
many files individually.

- **bash**: Execute shell commands. Use sparingly. Prefer read_file and grep \
when possible. Never use destructive commands without asking the user first.

# Tool usage rules

1. When you need information, prefer searching (grep) over guessing.
2. When the user asks "where is X", use grep first, then read_file.
3. When the user asks to modify code, read first, then propose a diff, then \
ask for confirmation before writing.
4. Do not run commands that modify the filesystem without user approval.\
"""

STATIC_BEHAVIOR = """\
# Behavior

- Be concise. Lead with the answer, not the reasoning.
- When you don't know something, say so. Do not invent file paths, function \
names, or APIs.
- When referencing code, use the format `file_path:line_number` so the user \
can navigate to it.
- If a tool fails, try a different approach. Do not retry the same failing \
call repeatedly.\
"""


# ============================================================================
# Dynamic section — built per call
# ============================================================================


def build_dynamic_environment(cwd: str, os_name: str) -> str:
    return f"""\
# Environment

- Working directory: {cwd}
- Operating system: {os_name}\
"""


def build_dynamic_date(today: str) -> str:
    return f"Today's date is {today}."


# ============================================================================
# Main entry — assembles the full system prompt
# ============================================================================


def build_system_prompt(cwd: str, os_name: str, today: str) -> list[dict]:
    """Returns the Anthropic Messages API `system` field as content blocks.

    The first block carries the cache_control breakpoint. The second is
    dynamic and never cached.
    """
    static_text = "\n\n".join([STATIC_INTRO, STATIC_TOOL_USAGE, STATIC_BEHAVIOR])
    dynamic_text = "\n\n".join([
        build_dynamic_date(today),
        build_dynamic_environment(cwd, os_name),
    ])
    return [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": dynamic_text,
        },
    ]
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_prompt.py -v
```

Expected: 7 passed.

**Step 5: Commit**

```bash
git add agent/prompt.py tests/test_prompt.py
git commit -m "feat(agent): add cache-aware Static/Dynamic prompt builder"
```

---

## Task 7: API Wrapper (api.py)

**Files:**
- Create: `agent/api.py`
- Create: `tests/test_api.py`

**Step 1: Write failing tests**

`tests/test_api.py`:
```python
"""Tests for agent.api — error classification and message helpers."""
from agent.api import (
    is_recoverable,
    build_assistant_message,
    build_tool_result_block,
)


# ----------------------------------------------------------------------------
# is_recoverable
# ----------------------------------------------------------------------------


class _FakeError(Exception):
    def __init__(self, status_code=None):
        self.status_code = status_code
        super().__init__(f"Fake error {status_code}")


def test_is_recoverable_503():
    assert is_recoverable(_FakeError(status_code=503)) is True


def test_is_recoverable_429_rate_limit():
    assert is_recoverable(_FakeError(status_code=429)) is True


def test_is_recoverable_500():
    assert is_recoverable(_FakeError(status_code=500)) is True


def test_not_recoverable_401_auth():
    assert is_recoverable(_FakeError(status_code=401)) is False


def test_not_recoverable_400_bad_request():
    assert is_recoverable(_FakeError(status_code=400)) is False


def test_not_recoverable_unknown():
    """Errors without status_code default to non-recoverable (fail-closed)."""
    assert is_recoverable(Exception("unknown")) is False


# ----------------------------------------------------------------------------
# build_assistant_message — converts API response to message dict
# ----------------------------------------------------------------------------


class _FakeBlock:
    def __init__(self, type, **kwargs):
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def test_build_assistant_message_text_only():
    response = _FakeResponse(content=[_FakeBlock(type="text", text="hello")])
    msg = build_assistant_message(response)
    assert msg["role"] == "assistant"
    assert len(msg["content"]) == 1
    assert msg["content"][0] == {"type": "text", "text": "hello"}


def test_build_assistant_message_with_tool_use():
    response = _FakeResponse(content=[
        _FakeBlock(type="text", text="I'll read it."),
        _FakeBlock(type="tool_use", id="toolu_1", name="read_file", input={"file_path": "x"}),
    ])
    msg = build_assistant_message(response)
    assert len(msg["content"]) == 2
    assert msg["content"][1]["type"] == "tool_use"
    assert msg["content"][1]["id"] == "toolu_1"
    assert msg["content"][1]["name"] == "read_file"
    assert msg["content"][1]["input"] == {"file_path": "x"}


# ----------------------------------------------------------------------------
# build_tool_result_block
# ----------------------------------------------------------------------------


def test_build_tool_result_block_success():
    block = build_tool_result_block(tool_use_id="toolu_1", content="file content", is_error=False)
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_1"
    assert block["content"] == "file content"
    assert block.get("is_error", False) is False


def test_build_tool_result_block_error():
    block = build_tool_result_block(tool_use_id="toolu_2", content="boom", is_error=True)
    assert block["is_error"] is True
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_api.py -v
```

Expected: ImportError.

**Step 3: Implement agent/api.py**

`agent/api.py`:
```python
"""Anthropic SDK wrapper helpers.

Isolates SDK details so that loop.py and main.py can stay focused on
agent logic. Also keeps loop.py easy to test with a fake client.
"""
from typing import Any


# Recoverable HTTP status codes — see Section 2.4 / withheld errors design
_RECOVERABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_recoverable(error: Exception) -> bool:
    """Classify an exception as recoverable (worth retrying with fallback model)
    or non-recoverable (must surface to user).

    Fail-closed: unknown errors default to non-recoverable.
    """
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        return False
    return status_code in _RECOVERABLE_STATUS_CODES


def build_assistant_message(response: Any) -> dict:
    """Convert an Anthropic API response into a message dict suitable for
    appending to the messages list.

    Preserves all content blocks (text, tool_use, thinking) byte-for-byte.
    """
    content_blocks = []
    for block in response.content:
        if block.type == "text":
            content_blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content_blocks.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        else:
            # Unknown block type — preserve via __dict__ for forward compat
            content_blocks.append({"type": block.type, **{
                k: v for k, v in vars(block).items() if k != "type"
            }})
    return {"role": "assistant", "content": content_blocks}


def build_tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    """Build a tool_result content block matching Anthropic's expected shape."""
    block: dict = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_api.py -v
```

Expected: 10 passed.

**Step 5: Commit**

```bash
git add agent/api.py tests/test_api.py
git commit -m "feat(agent): add Anthropic SDK wrapper helpers"
```

---

## Task 8: Agent Loop Core (loop.py — part 1: happy path)

**Files:**
- Create: `agent/loop.py`
- Create: `tests/test_loop.py`

**Step 1: Build a FakeAnthropicClient and write happy-path tests**

`tests/test_loop.py`:
```python
"""Tests for agent.loop.

Uses a FakeAnthropicClient (real class, not unittest.mock) for clarity.
"""
from collections import deque
from agent.loop import run_agent_loop
from agent.types import AgentResult
from agent.tools import Tool, ToolResult
from pydantic import BaseModel


# ============================================================================
# Test doubles
# ============================================================================


class _FakeBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, queue):
        self._queue = queue
        self.calls = []  # records every (model, messages) tuple for assertions

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        if not self._queue:
            raise RuntimeError("FakeAnthropicClient ran out of canned responses")
        return self._queue.popleft()


class FakeAnthropicClient:
    """A queueable fake Anthropic client for loop tests."""

    def __init__(self, responses):
        self.messages = _FakeMessages(deque(responses))


# Test tool — always echoes its input
class _EchoInput(BaseModel):
    text: str


class _EchoTool(Tool):
    name = "echo"

    def description(self) -> str:
        return "Echo text back."

    @property
    def input_model(self):
        return _EchoInput

    def execute(self, input: _EchoInput) -> ToolResult:
        return ToolResult(output=f"echoed: {input.text}")


def _user_msg(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _stub_system_prompt():
    return [{"type": "text", "text": "You are a test agent.", "cache_control": {"type": "ephemeral"}}]


# ============================================================================
# Happy path
# ============================================================================


def test_loop_completes_when_no_tool_use():
    """If the model returns text only, loop exits with status=completed."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(content=[_FakeBlock(type="text", text="hello")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert isinstance(result, AgentResult)
    assert result.status == "completed"
    # Original user message + new assistant message
    assert len(result.messages) == 2
    assert result.messages[1]["role"] == "assistant"


def test_loop_executes_tool_then_completes():
    """Turn 1: model uses tool. Turn 2: model returns text. Loop exits."""
    client = FakeAnthropicClient(responses=[
        # Turn 1: tool_use
        _FakeResponse(
            content=[
                _FakeBlock(type="text", text="I'll echo it."),
                _FakeBlock(type="tool_use", id="t1", name="echo", input={"text": "hi"}),
            ],
            stop_reason="tool_use",
        ),
        # Turn 2: final text
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("echo hi"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # user → assistant(tool_use) → user(tool_result) → assistant(text)
    assert len(result.messages) == 4
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[2]["role"] == "user"
    # Tool result is in the second user message
    tool_result_block = result.messages[2]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert "echoed: hi" in tool_result_block["content"]


def test_loop_exits_on_max_turns():
    """If model keeps calling tools forever, loop exits at max_turns."""
    # Generate 10 responses, all with tool_use
    responses = [
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id=f"t{i}", name="echo", input={"text": str(i)})],
            stop_reason="tool_use",
        )
        for i in range(10)
    ]
    client = FakeAnthropicClient(responses=responses)
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("loop"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=3,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "max_turns"


def test_loop_uses_primary_model_first():
    """No errors → only the primary model is used."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert client.messages.calls[0]["model"] == "primary"
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_loop.py -v
```

Expected: ImportError.

**Step 3: Implement the happy path of agent/loop.py**

`agent/loop.py`:
```python
"""Agent loop — the heart of the agent.

See docs/plans/2026-04-07-phase1-skeleton-design.md, Section 2.

This file implements ONLY the happy path and max_turns exit. Continue sites
(model fallback, output recovery) and tool execution come in tasks 9 and 10.
"""
from dataclasses import replace
from typing import Callable, Optional

from agent.types import State, AgentResult, Message
from agent.tools import Tool
from agent.api import build_assistant_message, build_tool_result_block, is_recoverable


def run_agent_loop(
    client,
    initial_messages: tuple[Message, ...],
    tools: list[Tool],
    system_prompt: list[dict],
    max_turns: int = 25,
    primary_model: str = "claude-opus-4-6",
    fallback_model: str = "claude-sonnet-4-6",
    on_api_response: Optional[Callable] = None,
) -> AgentResult:
    """Run the agent loop until completion, max_turns, or unrecoverable error.

    Returns AgentResult — never raises (all errors caught and wrapped).
    """
    state = State(
        messages=initial_messages,
        turn=1,
        fallback_model_used=False,
        output_retries=0,
        transition_reason="initial",
    )

    while True:
        # Exit condition 2: max_turns
        if state.turn > max_turns:
            return AgentResult(
                status="max_turns",
                messages=state.messages,
                reason=f"reached max turns ({max_turns})",
            )

        # Call the model (Phase 1 = batch, not streaming)
        model_to_use = primary_model if not state.fallback_model_used else fallback_model
        try:
            response = client.messages.create(
                model=model_to_use,
                messages=list(state.messages),
                system=system_prompt,
                tools=[t.to_anthropic_schema() for t in tools],
                max_tokens=8192,
            )
        except Exception as e:
            # Continue site 1 (model fallback) — implemented in Task 9
            return AgentResult(
                status="model_error",
                messages=state.messages,
                reason=str(e),
            )

        if on_api_response is not None:
            on_api_response(getattr(response, "usage", None))

        # Append assistant message
        assistant_message = build_assistant_message(response)
        new_messages = state.messages + (assistant_message,)

        # Detect tool_use via content blocks (NEVER trust stop_reason)
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        # Exit condition 1: completed (no tool_use)
        if not tool_use_blocks:
            return AgentResult(
                status="completed",
                messages=new_messages,
                reason="model finished naturally",
            )

        # Tool execution — Task 10 will fill this in with permission checks.
        # For now, naive execution to make Task 8 tests pass.
        tool_results = []
        for tool_use in tool_use_blocks:
            tool = next((t for t in tools if t.name == tool_use.name), None)
            if tool is None:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Unknown tool: {tool_use.name}", is_error=True,
                ))
                continue
            try:
                validated = tool.input_model(**tool_use.input)
                result = tool.execute(validated)
                tool_results.append(build_tool_result_block(
                    tool_use.id, result.output, is_error=result.is_error,
                ))
            except Exception as e:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Execution error: {e}", is_error=True,
                ))

        tool_result_message = {"role": "user", "content": tool_results}
        new_messages = new_messages + (tool_result_message,)

        # Build next state — full reconstruction, no mutation
        state = State(
            messages=new_messages,
            turn=state.turn + 1,
            fallback_model_used=state.fallback_model_used,
            output_retries=0,
            transition_reason="tool_use",
        )
        # Implicit continue → top of while
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_loop.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add agent/loop.py tests/test_loop.py
git commit -m "feat(agent): add agent loop happy path and max_turns exit"
```

---

## Task 9: Continue Sites — Model Fallback + Output Recovery

**Files:**
- Modify: `agent/loop.py`
- Modify: `tests/test_loop.py` (append)

**Step 1: Append failing tests**

Append to `tests/test_loop.py`:
```python
# ============================================================================
# Continue site 1: model fallback (withheld errors)
# ============================================================================


class _RecoverableError(Exception):
    status_code = 503


class _UnrecoverableError(Exception):
    status_code = 401


class _FakeMessagesWithErrors:
    """Fake messages.create that raises on first call(s) then returns canned response."""

    def __init__(self, errors_then_responses):
        self._sequence = list(errors_then_responses)
        self.calls = []

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({"model": model})
        item = self._sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClientWithErrors:
    def __init__(self, sequence):
        self.messages = _FakeMessagesWithErrors(sequence)


def test_loop_falls_back_to_secondary_model_on_recoverable_error():
    client = _FakeClientWithErrors([
        _RecoverableError(),  # primary model fails
        _FakeResponse(content=[_FakeBlock(type="text", text="ok via fallback")]),  # fallback succeeds
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # First call used primary, second used fallback
    assert client.messages.calls[0]["model"] == "primary"
    assert client.messages.calls[1]["model"] == "fallback"


def test_loop_does_not_fall_back_on_unrecoverable_error():
    client = _FakeClientWithErrors([
        _UnrecoverableError(),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "model_error"
    # Only one call attempted (primary), no fallback
    assert len(client.messages.calls) == 1


def test_loop_returns_model_error_when_fallback_also_fails():
    client = _FakeClientWithErrors([
        _RecoverableError(),
        _RecoverableError(),  # fallback also fails
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "model_error"


# ============================================================================
# Continue site 2: output token recovery
# ============================================================================


def test_loop_recovers_from_max_tokens_truncation():
    """If response stops with max_tokens AND no tool_use, request continuation."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="text", text="part 1...")],
            stop_reason="max_tokens",
        ),
        _FakeResponse(
            content=[_FakeBlock(type="text", text="...part 2 final")],
            stop_reason="end_turn",
        ),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("write a long response"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=10,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # Both partial responses preserved
    assert len(result.messages) == 3  # user + assistant(part 1) + assistant(part 2)


def test_loop_output_recovery_capped_at_3_retries():
    """Output recovery should not loop forever."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(content=[_FakeBlock(type="text", text=f"part {i}")], stop_reason="max_tokens")
        for i in range(10)
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=20,
        primary_model="primary",
        fallback_model="fallback",
    )
    # After 3 retries, loop returns completed (gives up on recovery)
    assert result.status == "completed"
    # 1 initial call + 3 retries = 4 API calls max
    assert len(client.messages.calls) <= 4
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_loop.py -v
```

Expected: 4 new tests fail (the existing 4 still pass).

**Step 3: Implement continue sites in agent/loop.py**

Edit `agent/loop.py` — replace the existing `try/except` and the "no tool_use" return block:

```python
        # Call the model (Phase 1 = batch, not streaming)
        model_to_use = primary_model if not state.fallback_model_used else fallback_model
        try:
            response = client.messages.create(
                model=model_to_use,
                messages=list(state.messages),
                system=system_prompt,
                tools=[t.to_anthropic_schema() for t in tools],
                max_tokens=8192,
            )
        except Exception as e:
            # Continue site 1: model fallback (withheld error)
            if not state.fallback_model_used and is_recoverable(e):
                state = replace(
                    state,
                    fallback_model_used=True,
                    transition_reason="model_fallback",
                )
                continue
            return AgentResult(
                status="model_error",
                messages=state.messages,
                reason=str(e),
            )
```

And replace the "no tool_use" exit block with:

```python
        # Exit condition 1: completed (no tool_use)
        if not tool_use_blocks:
            # Continue site 2: output token recovery
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "max_tokens" and state.output_retries < 3:
                state = State(
                    messages=new_messages,
                    turn=state.turn + 1,
                    fallback_model_used=state.fallback_model_used,
                    output_retries=state.output_retries + 1,
                    transition_reason="output_recovery",
                )
                continue

            return AgentResult(
                status="completed",
                messages=new_messages,
                reason="model finished naturally",
            )
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_loop.py -v
```

Expected: all 8 tests pass.

**Step 5: Commit**

```bash
git add agent/loop.py tests/test_loop.py
git commit -m "feat(agent): add model fallback and output recovery continue sites"
```

---

## Task 10: Tool Execution with Permission Checks

**Files:**
- Modify: `agent/loop.py`
- Modify: `tests/test_loop.py` (append)

**Step 1: Append failing tests**

Append to `tests/test_loop.py`:
```python
# ============================================================================
# Tool execution with permission checks
# ============================================================================
from agent.tools import PermissionDecision


class _AlwaysDenyTool(Tool):
    name = "denied_tool"

    def description(self) -> str:
        return "Always denied."

    @property
    def input_model(self):
        return _EchoInput

    def check_permissions(self, input):
        return PermissionDecision.DENY

    def execute(self, input):
        raise AssertionError("Should never execute when denied")


class _AlwaysRaiseTool(Tool):
    name = "raise_tool"

    def description(self) -> str:
        return "Always raises."

    @property
    def input_model(self):
        return _EchoInput

    def execute(self, input):
        raise RuntimeError("kaboom")


def test_loop_denies_tool_returns_error_to_model():
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="denied_tool", input={"text": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="ok i won't")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AlwaysDenyTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    # The tool_result message should contain the denial
    tool_result = result.messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result.get("is_error") is True
    assert "denied" in tool_result["content"].lower() or "permission" in tool_result["content"].lower()


def test_loop_tool_exception_returns_error_to_model_not_raised():
    """A raising tool must NOT crash the loop. Error goes back as tool_result."""
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="raise_tool", input={"text": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="that failed")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AlwaysRaiseTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    tool_result = result.messages[2]["content"][0]
    assert tool_result.get("is_error") is True
    assert "kaboom" in tool_result["content"]


def test_loop_invalid_tool_input_returns_validation_error():
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="echo", input={"WRONG_FIELD": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    tool_result = result.messages[2]["content"][0]
    assert tool_result.get("is_error") is True


def test_loop_unknown_tool_name_returns_error():
    client = FakeAnthropicClient(responses=[
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="ghost_tool", input={})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="oh")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_EchoTool()],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="primary",
        fallback_model="fallback",
    )
    assert result.status == "completed"
    tool_result = result.messages[2]["content"][0]
    assert tool_result.get("is_error") is True
    assert "ghost_tool" in tool_result["content"] or "unknown" in tool_result["content"].lower()
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_loop.py -v
```

Expected: most pass already (because Task 8 added a naive version), but `test_loop_denies_tool_returns_error_to_model` fails because the loop never calls `check_permissions`.

**Step 3: Add permission check in agent/loop.py**

Replace the tool execution loop in `agent/loop.py` (the `for tool_use in tool_use_blocks:` block) with:

```python
        # Tool execution — Step 1: validate, Step 2: check permissions, Step 3: execute
        from agent.tools import PermissionDecision  # local import to avoid circular at module load

        tool_results = []
        for tool_use in tool_use_blocks:
            tool = next((t for t in tools if t.name == tool_use.name), None)
            if tool is None:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Unknown tool: {tool_use.name}", is_error=True,
                ))
                continue

            # Step 1: validate input via Pydantic
            try:
                validated = tool.input_model(**tool_use.input)
            except Exception as e:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Invalid input: {e}", is_error=True,
                ))
                continue

            # Step 2: permission check
            decision = tool.check_permissions(validated)
            if decision == PermissionDecision.DENY:
                tool_results.append(build_tool_result_block(
                    tool_use.id,
                    f"Permission denied: tool={tool.name}",
                    is_error=True,
                ))
                continue

            # Step 3: execute
            try:
                result = tool.execute(validated)
                tool_results.append(build_tool_result_block(
                    tool_use.id, result.output, is_error=result.is_error,
                ))
            except Exception as e:
                tool_results.append(build_tool_result_block(
                    tool_use.id, f"Execution error: {e}", is_error=True,
                ))
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_loop.py -v
```

Expected: all 12 loop tests pass.

**Step 5: Run the full test suite**

Run:
```bash
python -m pytest tests/ -v
```

Expected: everything green.

**Step 6: Commit**

```bash
git add agent/loop.py tests/test_loop.py
git commit -m "feat(agent): add validate→permission→execute tool pipeline in loop"
```

---

## Task 11: CLI REPL (main.py)

**Files:**
- Create: `agent/main.py`
- Create: `tests/test_main.py`

**Step 1: Write failing tests for the testable helpers**

`tests/test_main.py`:
```python
"""Tests for agent.main — only the testable helpers, not the REPL itself."""
from agent.main import get_tools, extract_final_text


def test_get_tools_returns_three_tools():
    tools = get_tools()
    assert len(tools) == 3


def test_get_tools_sorted_alphabetically():
    """CRITICAL: tools must be in stable order for prompt cache."""
    tools = get_tools()
    names = [t.name for t in tools]
    assert names == sorted(names)


def test_get_tools_includes_expected_tools():
    tools = get_tools()
    names = {t.name for t in tools}
    assert names == {"bash", "grep", "read_file"}


def test_extract_final_text_from_assistant_message():
    messages = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello there"}]},
    )
    assert extract_final_text(messages) == "hello there"


def test_extract_final_text_skips_tool_use_blocks():
    messages = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "the answer is x"}]},
    )
    # Should return the LAST assistant message's text
    assert extract_final_text(messages) == "the answer is x"


def test_extract_final_text_no_assistant_message():
    messages = (
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    )
    assert "no response" in extract_final_text(messages).lower()
```

**Step 2: Run tests to verify they fail**

Run:
```bash
python -m pytest tests/test_main.py -v
```

Expected: ImportError.

**Step 3: Implement agent/main.py**

`agent/main.py`:
```python
"""CLI REPL — the lungs of the agent.

Manages session-level conversation history and delegates each turn to
the agent loop.
"""
import os
import sys
import datetime
import platform

from anthropic import Anthropic

from agent.loop import run_agent_loop
from agent.tools import Tool, ReadFileTool, GrepTool, BashTool
from agent.prompt import build_system_prompt


PRIMARY_MODEL = "claude-opus-4-6"
FALLBACK_MODEL = "claude-sonnet-4-6"
MAX_TURNS_PER_QUERY = 25


def get_tools() -> list[Tool]:
    """Tool registry. Sorted alphabetically — order MUST be stable for cache."""
    return sorted(
        [BashTool(), GrepTool(), ReadFileTool()],
        key=lambda t: t.name,
    )


def init_client() -> Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: please set ANTHROPIC_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    return Anthropic(api_key=api_key)


def print_cache_stats(usage) -> None:
    """Print cache hit info so the user can see prompt cache working."""
    if usage is None:
        return
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    total_cached = cache_read + cache_create
    hit_rate = (cache_read / total_cached * 100) if total_cached > 0 else 0
    print(
        f"  [tokens] in={input_tokens} cache_read={cache_read} "
        f"cache_create={cache_create} out={output_tokens} hit={hit_rate:.0f}%"
    )


def extract_final_text(messages: tuple) -> str:
    """Pull the text content from the LAST assistant message."""
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text_parts = [
                block["text"]
                for block in msg["content"]
                if block.get("type") == "text"
            ]
            if text_parts:
                return "\n".join(text_parts)
    return "(no response)"


def repl():
    client = init_client()
    tools = get_tools()

    print("Code Repo Assistant (Phase 1)")
    print("Type your question. /exit to quit, /reset to clear history.\n")

    conversation_history: tuple = ()

    while True:
        # 1. Read input
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not user_input:
            continue

        # 2. Slash commands
        if user_input == "/exit":
            print("bye.")
            break
        if user_input == "/reset":
            conversation_history = ()
            print("(history cleared)")
            continue

        # 3. Append user message
        new_user_message = {
            "role": "user",
            "content": [{"type": "text", "text": user_input}],
        }
        turn_messages = conversation_history + (new_user_message,)

        # 4. Build system prompt (static text identical every call → cache hits)
        system_prompt = build_system_prompt(
            cwd=os.getcwd(),
            os_name=platform.system(),
            today=datetime.date.today().isoformat(),
        )

        # 5. Run loop
        try:
            result = run_agent_loop(
                client=client,
                initial_messages=turn_messages,
                tools=tools,
                system_prompt=system_prompt,
                max_turns=MAX_TURNS_PER_QUERY,
                primary_model=PRIMARY_MODEL,
                fallback_model=FALLBACK_MODEL,
                on_api_response=print_cache_stats,
            )
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:
            print(f"\n[unexpected error] {type(e).__name__}: {e}")
            print("(history kept; you can /reset to start over)")
            continue

        # 6. Handle result
        if result.status == "completed":
            conversation_history = result.messages
            print(f"\n{extract_final_text(result.messages)}\n")
        elif result.status == "max_turns":
            conversation_history = result.messages
            print(f"\n[reached max turns: {MAX_TURNS_PER_QUERY}]")
            print("(partial result above, /reset to start over)\n")
        elif result.status == "model_error":
            # Do NOT update history — keeps conversation clean for retry
            print(f"\n[model error] {result.reason}")
            print("(history unchanged, you can retry)\n")


if __name__ == "__main__":
    repl()
```

**Step 4: Run tests to verify they pass**

Run:
```bash
python -m pytest tests/test_main.py -v
```

Expected: 6 passed.

**Step 5: Run full test suite as a sanity check**

Run:
```bash
python -m pytest tests/ -v
```

Expected: all green.

**Step 6: Commit**

```bash
git add agent/main.py tests/test_main.py
git commit -m "feat(agent): add CLI REPL with session history and cache stats"
```

---

## Task 12: End-to-End Manual Verification + README

**Files:**
- Create: `agent/README.md`

**Step 1: Verify ANTHROPIC_API_KEY is set**

Run:
```bash
echo $ANTHROPIC_API_KEY | head -c 10
```

Expected: prints the first 10 chars of the key. If empty, set it before continuing.

**Step 2: Run the REPL and execute manual test scenarios**

Run:
```bash
cd D:/tools/claude-code-source && python -m agent.main
```

Then perform each of the following manually. Each scenario corresponds to a row in the verification checklist (design doc Section 5.5).

**Scenario 1 — Basic file read:**
```
> Read the file CLAUDE.md and tell me what this project is about.
```
Expected: model uses `read_file` tool, returns a summary. Cache stats line printed for each API call.

**Scenario 2 — Search:**
```
> Search for all uses of "TODO" under the docs/ directory.
```
Expected: model uses `grep`, returns matching lines.

**Scenario 3 — Multi-step:**
```
> Find all files in agent/ that define a class, then read tools.py and list the class names.
```
Expected: at least 2 tool calls in sequence.

**Scenario 4 — Missing file (error handling):**
```
> Read the file /tmp/this/does/not/exist.txt
```
Expected: model gracefully reports the file is missing. REPL does not crash.

**Scenario 5 — Permission denial (.env):**
```
> Read the file .env
```
Expected: tool returns "Permission denied". Model relays this to user.

**Scenario 6 — Permission denial (rm -rf /):**
```
> Run the command "rm -rf /" using bash
```
Expected: bash tool denies. Model relays denial to user.

**Scenario 7 — Cache verification:**
After scenario 1, run scenario 2 immediately. Look at the `[tokens]` line for the second call:
- Expected: `cache_read` > 0 (the static system prompt block is reused)
- Expected: `hit` percentage > 0%

**Scenario 8 — Slash commands:**
```
> /reset
> /exit
```
Expected: `/reset` clears history, `/exit` exits cleanly.

**Step 3: Document any issues found**

If any scenario fails, fix the underlying code and re-run. Do NOT commit broken code.

**Step 4: Write agent/README.md**

`agent/README.md`:
```markdown
# Phase 1 Code Repo Assistant Agent

Minimal agent built atop the lessons learned from Claude Code v2.1.88 source.

## Architecture

- `main.py` — CLI REPL, session history
- `loop.py` — Agent state machine (2 continue sites, 3 exit conditions)
- `tools.py` — Tool ABC + read_file, grep, bash
- `prompt.py` — Static/Dynamic prompt builder for cache friendliness
- `api.py` — Anthropic SDK wrapper
- `types.py` — Immutable State and AgentResult

See `docs/plans/2026-04-07-phase1-skeleton-design.md` for the full design doc.

## Setup

```bash
pip install -r agent/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

External dependency: `ripgrep` (rg) for the grep tool. Install from
https://github.com/BurntSushi/ripgrep.

## Run

```bash
python -m agent.main
```

REPL commands:
- `/reset` — clear conversation history
- `/exit` — quit

## Test

```bash
python -m pytest tests/ -v
```

All tests are unit tests with a fake Anthropic client. No real API calls in
the test suite.

## Cache stats

Every API call prints a `[tokens]` line showing cache_read / cache_create.
On the second turn within 5 minutes, expect `cache_read > 0`.
```

**Step 5: Commit README**

```bash
git add agent/README.md
git commit -m "docs(agent): add Phase 1 README with setup and usage"
```

---

## Task 13: Architecture Compliance Audit

This is a final review pass — no new code, just verification that the 5 ironclad rules and 13 architecture-discipline checks from the design doc all hold.

**Step 1: Rule 1 — content block tool detection (no stop_reason)**

Run:
```bash
cd D:/tools/claude-code-source && python -m grep_tool stop_reason agent/loop.py
```

Or use ripgrep manually:
```bash
rg "stop_reason" agent/loop.py
```

Expected: only ONE occurrence of `stop_reason`, in the output recovery branch (Continue site 2). Tool detection itself uses `b.type == "tool_use"`.

**Step 2: Rule 3 — no f-strings or conditionals in static prompt**

Run:
```bash
rg -n "f['\"]|if .*:" agent/prompt.py
```

Expected: matches only inside `build_dynamic_*` functions, NOT inside `STATIC_*` constants. Manually scan the constants — they should be plain strings.

**Step 3: State frozen check**

Run:
```bash
rg "frozen=True" agent/types.py
```

Expected: at least 2 matches (State and AgentResult).

**Step 4: Tool error handling check**

Run:
```bash
rg "raise " agent/tools.py
```

Expected: no `raise` statements inside `execute()` methods. Errors are returned as `ToolResult(is_error=True)`.

**Step 5: Tools sorted check**

Run:
```bash
rg "sorted" agent/main.py
```

Expected: `get_tools()` uses `sorted(...)`.

**Step 6: Run full test suite one last time**

```bash
python -m pytest tests/ -v
```

Expected: ALL tests pass.

**Step 7: Mark Phase 1 complete**

If everything above passes, Phase 1 is done. Commit a marker:

```bash
git commit --allow-empty -m "chore(agent): Phase 1 skeleton complete

- 4 core files (loop, tools, prompt, main) + 2 support files (types, api)
- 2 continue sites (model fallback + output recovery)
- 3 exit conditions (completed / max_turns / model_error)
- 3 tools (read_file, grep, bash) with fail-closed defaults
- Static/Dynamic prompt with cache_control breakpoint
- Immutable State, content-block tool detection
- ~37 unit tests, all green
- Manual end-to-end verification per design doc Section 5.5"
```

---

## Summary

| Task | Files touched | Approximate LoC | Tests |
|------|--------------|----------------|-------|
| 0 | bootstrap | 0 | 0 |
| 1 | types.py | ~25 | 4 |
| 2 | tools.py (ABC) | ~70 | 6 |
| 3 | tools.py (read_file) | ~60 | 6 |
| 4 | tools.py (grep) | ~55 | 4 |
| 5 | tools.py (bash) | ~75 | 9 |
| 6 | prompt.py | ~80 | 7 |
| 7 | api.py | ~50 | 10 |
| 8 | loop.py (happy path) | ~80 | 4 |
| 9 | loop.py (continue sites) | ~25 | 4 |
| 10 | loop.py (tool exec) | ~20 | 4 |
| 11 | main.py | ~110 | 6 |
| 12 | README + manual test | ~30 | 0 |
| 13 | audit | 0 | 0 |
| **Total** | | **~680** | **64** |

All tests run without any real API calls. End-to-end validation in Task 12 is the only place real network/API is used.

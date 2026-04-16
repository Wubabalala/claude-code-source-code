"""Tests for agent.permissions + Phase 3 integration in agent.tools / agent.loop.

Testing strategy:
  - is_hard_denied / prompt_user_for_permission: exercised directly with
    monkeypatch (no network, no subprocess).
  - BashTool contract C: exercised through BashTool.check_permissions directly.
  - GrepTool contract E: mocks subprocess.run to capture rg flags, plus one
    real-rg integration test behind a _HAS_RG skip.
  - Loop integration: reuses FakeAnthropicClient pattern from test_loop.py.
"""
import os
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.permissions import (
    GREP_EXCLUDE_GLOBS,
    HARD_DENY_DIR_COMPONENTS,
    is_hard_denied,
    prompt_user_for_permission,
)
from agent.tools import (
    BashInput,
    BashTool,
    GrepInput,
    GrepTool,
    PermissionDecision,
    PermissionOutcome,
    ReadFileInput,
    ReadFileTool,
    Tool,
    ToolResult,
)


_HAS_RG = shutil.which("rg") is not None


# ---------------------------------------------------------------------------
# is_hard_denied
# ---------------------------------------------------------------------------


def test_is_hard_denied_dir_component_git():
    assert is_hard_denied("/abs/path/.git/config") is True


def test_is_hard_denied_dir_component_ssh():
    assert is_hard_denied("/home/user/.ssh/id_rsa") is True


def test_is_hard_denied_case_insensitive(tmp_path):
    # Windows-style uppercase
    assert is_hard_denied(str(tmp_path / ".SSH" / "config")) is True


def test_is_hard_denied_filename_bashrc():
    assert is_hard_denied("~/.bashrc") is True


def test_is_hard_denied_dotenv():
    assert is_hard_denied(".env") is True
    assert is_hard_denied(".env.local") is True
    assert is_hard_denied(".env.production") is True


def test_is_hard_denied_secrets_patterns():
    for p in [
        "foo.pem",
        "deploy.KEY",
        "id_rsa",
        "id_ed25519.pub",
        "my_credentials.json",
        "AWS_Credentials.yaml",
    ]:
        assert is_hard_denied(p) is True, f"should deny: {p}"


def test_is_hard_denied_benign_paths(tmp_path):
    # Put these under a real tmp dir so resolve() doesn't land in some
    # denied zone like a user's .ssh on CI hosts.
    (tmp_path / "main.py").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("x")
    assert is_hard_denied(tmp_path / "main.py") is False
    assert is_hard_denied(tmp_path / "sub" / "a.py") is False


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires admin on Windows")
def test_is_hard_denied_symlink_into_git(tmp_path):
    real_git = tmp_path / "repo" / ".git"
    real_git.mkdir(parents=True)
    (real_git / "config").write_text("content")
    link = tmp_path / "link_to_git_config"
    link.symlink_to(real_git / "config")
    assert is_hard_denied(link) is True


def test_is_hard_denied_failclosed_on_oserror(monkeypatch):
    def boom(self, strict=False):
        raise OSError("cannot resolve")
    monkeypatch.setattr("pathlib.Path.resolve", boom)
    assert is_hard_denied("/any/path") is True


# ---------------------------------------------------------------------------
# prompt_user_for_permission
# ---------------------------------------------------------------------------


def _outcome_ask(tool="bash", target="rm x", op="write", risk="r"):
    return PermissionOutcome(
        decision=PermissionDecision.ASK,
        tool_name=tool,
        target=target,
        op_type=op,
        risk=risk,
    )


def test_prompt_returns_allow_on_y(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.ALLOW


def test_prompt_returns_allow_on_yes_uppercase(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "YES")
    assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.ALLOW


def test_prompt_returns_deny_on_n(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.DENY


def test_prompt_returns_deny_on_empty_or_gibberish(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    for answer in ["", "maybe", "allow", "ok", "1"]:
        monkeypatch.setattr("builtins.input", lambda _="", a=answer: a)
        assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.DENY


def test_prompt_returns_deny_on_eoferror(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def raise_eof(_=""):
        raise EOFError()
    monkeypatch.setattr("builtins.input", raise_eof)
    assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.DENY


def test_prompt_returns_deny_on_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def raise_ki(_=""):
        raise KeyboardInterrupt()
    monkeypatch.setattr("builtins.input", raise_ki)
    assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.DENY


def test_prompt_returns_deny_when_not_tty_without_calling_input(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    called = {"n": 0}
    def tracker(_=""):
        called["n"] += 1
        return "y"  # even if somehow called, returns ALLOW-worthy
    monkeypatch.setattr("builtins.input", tracker)
    assert prompt_user_for_permission(_outcome_ask()) == PermissionDecision.DENY
    assert called["n"] == 0, "input() must not be invoked when stdin is not a TTY"


# ---------------------------------------------------------------------------
# Tool metadata contract
# ---------------------------------------------------------------------------


def test_tool_without_metadata_cannot_instantiate():
    """New Tool subclass missing static metadata properties must fail at
    instantiation time (fail-closed contract)."""

    class _InIn(BaseModel):
        pass

    class Missing(Tool):
        name = "missing"

        def description(self):
            return "x"

        @property
        def input_model(self):
            return _InIn

        # reads_from_filesystem / writes_to_filesystem / destroys_data NOT declared

        def execute(self, input):
            return ToolResult(output="")

    with pytest.raises(TypeError):
        Missing()


def test_existing_tools_metadata_values():
    rf = ReadFileTool()
    assert rf.reads_from_filesystem is True
    assert rf.writes_to_filesystem is False
    assert rf.destroys_data is False

    g = GrepTool()
    assert g.reads_from_filesystem is True
    assert g.writes_to_filesystem is False
    assert g.destroys_data is False

    b = BashTool()
    assert b.reads_from_filesystem is True
    assert b.writes_to_filesystem is True
    assert b.destroys_data is True


def test_base_check_permissions_default_writes_to_ask():
    """A minimal Tool that declares writes=True but doesn't override
    check_permissions must land in ASK by default (fail-closed contract B)."""

    class _InIn(BaseModel):
        x: str

    class _WriteOnly(Tool):
        name = "wo"
        reads_from_filesystem = False
        writes_to_filesystem = True
        destroys_data = False

        def description(self):
            return "x"

        @property
        def input_model(self):
            return _InIn

        def execute(self, input):
            return ToolResult(output="")

    outcome = _WriteOnly().check_permissions(_InIn(x="y"))
    assert outcome.decision == PermissionDecision.ASK
    assert outcome.tool_name == "wo"
    assert outcome.op_type == "write"


def test_base_check_permissions_pure_read_is_allow():
    """Pure-read tool with no overridden check_permissions → ALLOW."""

    class _InIn(BaseModel):
        x: str

    class _PureRead(Tool):
        name = "pr"
        reads_from_filesystem = True
        writes_to_filesystem = False
        destroys_data = False

        def description(self):
            return "x"

        @property
        def input_model(self):
            return _InIn

        def execute(self, input):
            return ToolResult(output="")

    outcome = _PureRead().check_permissions(_InIn(x="y"))
    assert outcome.decision == PermissionDecision.ALLOW


# ---------------------------------------------------------------------------
# BashTool contract C
# ---------------------------------------------------------------------------


def _decision(cmd: str) -> PermissionOutcome:
    return BashTool().check_permissions(BashInput(command=cmd))


def test_bash_readonly_allows():
    assert _decision("ls /tmp").decision == PermissionDecision.ALLOW
    assert _decision("pwd").decision == PermissionDecision.ALLOW
    assert _decision("whoami").decision == PermissionDecision.ALLOW


def test_bash_git_readonly_subcommands_allow():
    assert _decision("git log --oneline").decision == PermissionDecision.ALLOW
    assert _decision("git diff HEAD").decision == PermissionDecision.ALLOW
    assert _decision("git status").decision == PermissionDecision.ALLOW


def test_bash_unknown_asks():
    assert _decision("docker ps").decision == PermissionDecision.ASK
    assert _decision("npm install").decision == PermissionDecision.ASK


def test_bash_write_cmd_without_dangerous_path_asks():
    assert _decision("touch /tmp/ok").decision == PermissionDecision.ASK
    assert _decision("mkdir /tmp/work").decision == PermissionDecision.ASK


def test_bash_write_to_hard_denied_denies():
    outcome = _decision("echo pwn > ~/.bashrc")
    # ~/.bashrc will match HARD_DENY_FILENAMES
    assert outcome.decision == PermissionDecision.DENY, outcome
    assert "hard-denied" in outcome.risk


def test_bash_rm_hard_denied_denies():
    outcome = _decision("rm -rf ~/.ssh")
    assert outcome.decision == PermissionDecision.DENY


def test_bash_readonly_with_hard_denied_path_denies():
    """Rule 5: even a read-only command touching a hard-denied path is DENY."""
    outcome = _decision("cat ~/.ssh/id_rsa")
    assert outcome.decision == PermissionDecision.DENY


def test_bash_shell_metacharacters_ask():
    for cmd in ["ls ; pwd", "a && b", "a || b", "echo $(whoami)", "echo `date`"]:
        assert _decision(cmd).decision == PermissionDecision.ASK, f"should ASK: {cmd}"


def test_bash_nested_shell_denied():
    """Contract C rule 3: nested interpreters are hard-denied."""
    for cmd in [
        "bash -c 'echo hi'",
        "sh -c 'ls'",
        "python -c 'pass'",
        "python3 -c 'print(1)'",
        "node -e '1'",
        "perl -e 'print 1'",
        "eval echo hi",
    ]:
        outcome = _decision(cmd)
        assert outcome.decision == PermissionDecision.DENY, f"should DENY: {cmd}"
        assert "nested interpreter" in outcome.risk.lower() or "hard-denied" in outcome.risk.lower()


def test_bash_find_with_exec_asks():
    """find -exec / -delete are side-effecting → not in the read-only path."""
    outcome = _decision("find . -exec rm {} \\;")
    assert outcome.decision == PermissionDecision.ASK


def test_bash_find_plain_allows():
    outcome = _decision("find . -name '*.py'")
    assert outcome.decision == PermissionDecision.ALLOW


def test_bash_unparseable_command_asks():
    # Unmatched quote → shlex.ValueError
    outcome = _decision("echo 'unterminated")
    assert outcome.decision == PermissionDecision.ASK


# --- Regression: hard-deny must beat metachars and key=value syntax ---


def test_bash_metachar_plus_hard_denied_path_still_denies():
    """Even when shell metacharacters are present, if any token maps to a
    hard-denied path, the decision must be DENY (not ASK).

    The metachar ASK must not override the hard-deny floor."""
    for cmd in [
        "echo x | tee ~/.bashrc",
        "cat ~/.ssh/id_rsa | wc -c",
        "ls -la && rm ~/.bashrc",
        "touch /tmp/ok ; echo x > ~/.bashrc",
    ]:
        outcome = _decision(cmd)
        assert outcome.decision == PermissionDecision.DENY, (
            f"metachar + hard-denied must DENY: {cmd!r} got {outcome}"
        )


def test_bash_dd_of_key_equals_hard_denied_denies():
    """dd uses `of=PATH` / `if=PATH` syntax; path extraction must unwrap the
    value after `=` or hard-denied writes slip through as ASK."""
    for cmd in [
        "dd of=.env if=/tmp/src",
        "dd if=/tmp/src of=.env bs=4096",
        "dd if=~/.ssh/id_rsa of=/tmp/stolen",
    ]:
        outcome = _decision(cmd)
        assert outcome.decision == PermissionDecision.DENY, (
            f"dd key=value hard-deny must DENY: {cmd!r} got {outcome}"
        )


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")
def test_grep_excludes_uppercase_secrets_variants(tmp_path):
    """Real rg integration: uppercase / mixed-case credentials / key files
    are still excluded because we use --iglob."""
    (tmp_path / "foo.py").write_text("MARKER visible")
    (tmp_path / "AWS_Credentials.yaml").write_text("MARKER hidden-in-yaml")
    (tmp_path / "DEPLOY.KEY").write_text("MARKER hidden-in-key")
    (tmp_path / "Cert.PEM").write_text("MARKER hidden-in-pem")

    result = GrepTool().execute(GrepInput(
        pattern="MARKER",
        path=str(tmp_path),
    ))
    assert "foo.py" in result.output
    assert "AWS_Credentials.yaml" not in result.output
    assert "DEPLOY.KEY" not in result.output
    assert "Cert.PEM" not in result.output


# ---------------------------------------------------------------------------
# GrepTool contract E
# ---------------------------------------------------------------------------


def test_grep_denies_hard_denied_search_root():
    outcome = GrepTool().check_permissions(GrepInput(pattern="x", path="~/.ssh"))
    assert outcome.decision == PermissionDecision.DENY


def test_grep_injects_case_insensitive_exclude_globs(monkeypatch):
    """Each denylist glob is passed via `--iglob` (case-insensitive) so
    uppercase variants like AWS_Credentials.yaml are also excluded."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class R:
            returncode = 1
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    GrepTool().execute(GrepInput(pattern="x", path="."))
    cmd = captured["cmd"]
    for g in GREP_EXCLUDE_GLOBS:
        assert g in cmd, f"missing exclude glob: {g}"
    # Every exclude glob must be preceded by --iglob (not --glob)
    for i, tok in enumerate(cmd):
        if tok in GREP_EXCLUDE_GLOBS:
            assert cmd[i - 1] == "--iglob", (
                f"exclude glob {tok!r} must use --iglob for case-insensitive match"
            )


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")
def test_grep_excludes_secrets_in_wide_dir_search(tmp_path):
    """Integration test: run real rg and ensure credentials file contents
    do not leak into the output even when searching the whole tmp dir."""
    (tmp_path / "foo.py").write_text("SECRET_MARKER_1234 visible")
    (tmp_path / "credentials.json").write_text(
        '{"SECRET_MARKER_1234": "hidden-in-secrets"}'
    )
    (tmp_path / "config.env").write_text("SECRET_MARKER_1234=x")
    # .env exact name — tests the pattern
    (tmp_path / ".env").write_text("SECRET_MARKER_1234=y")

    result = GrepTool().execute(GrepInput(
        pattern="SECRET_MARKER_1234",
        path=str(tmp_path),
    ))
    # Should find the one in foo.py
    assert "foo.py" in result.output
    # Must NOT find anything in credentials.json or .env
    assert "credentials.json" not in result.output
    assert ".env" not in result.output


# ---------------------------------------------------------------------------
# Loop integration
# ---------------------------------------------------------------------------


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
        self.calls = []

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({"model": model})
        return self._queue.popleft()


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(deque(responses))


def _user_msg(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _stub_sys():
    return [{"type": "text", "text": "sys"}]


class _InModel(BaseModel):
    cmd: str = "x"


class _AskTool(Tool):
    """Always returns ASK via base default implementation."""
    name = "ask_tool"
    reads_from_filesystem = False
    writes_to_filesystem = True
    destroys_data = False

    def description(self):
        return "d"

    @property
    def input_model(self):
        return _InModel

    def execute(self, input):
        return ToolResult(output="executed")


class _HardDenyTool(Tool):
    """Always returns DENY directly (hard-deny simulation)."""
    name = "deny_tool"
    reads_from_filesystem = True
    writes_to_filesystem = False
    destroys_data = False

    def description(self):
        return "d"

    @property
    def input_model(self):
        return _InModel

    def check_permissions(self, input):
        return PermissionOutcome(
            decision=PermissionDecision.DENY,
            tool_name=self.name,
            target=input.cmd,
            op_type="read",
            risk="simulated hard-deny",
        )

    def execute(self, input):  # pragma: no cover — must never be called
        raise AssertionError("execute must not be called for DENY tool")


def test_loop_hard_deny_does_not_call_prompt(monkeypatch):
    """Contract D: when check_permissions directly returns DENY, the loop
    must NOT call prompt_user_for_permission."""
    from agent.loop import run_agent_loop

    prompt_calls = {"n": 0}
    def tracker(outcome, **kwargs):
        prompt_calls["n"] += 1
        return PermissionDecision.ALLOW  # shouldn't be called

    monkeypatch.setattr("agent.permissions.prompt_user_for_permission", tracker)

    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="deny_tool", input={"cmd": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_HardDenyTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    assert result.status == "completed"
    assert prompt_calls["n"] == 0
    # Tool result should carry the denial
    tool_result_msg = result.messages[2]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["is_error"] is True


def test_loop_ask_allow_executes_tool(monkeypatch):
    from agent.loop import run_agent_loop

    monkeypatch.setattr(
        "agent.permissions.prompt_user_for_permission",
        lambda outcome, **kw: PermissionDecision.ALLOW,
    )

    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="ask_tool", input={"cmd": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AskTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    assert result.status == "completed"
    tool_result_msg = result.messages[2]
    assert "executed" in tool_result_msg["content"][0]["content"]
    assert tool_result_msg["content"][0].get("is_error") is not True


def test_loop_ask_deny_blocks_tool(monkeypatch):
    from agent.loop import run_agent_loop

    monkeypatch.setattr(
        "agent.permissions.prompt_user_for_permission",
        lambda outcome, **kw: PermissionDecision.DENY,
    )

    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="ask_tool", input={"cmd": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="acknowledged")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AskTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    assert result.status == "completed"
    tool_result_msg = result.messages[2]
    assert tool_result_msg["content"][0]["is_error"] is True
    assert "User denied" in tool_result_msg["content"][0]["content"]


def test_loop_permission_outcome_all_four_fields_shown(monkeypatch):
    """The outcome passed to prompt_user_for_permission must carry
    tool_name, target, op_type, risk — not just decision."""
    from agent.loop import run_agent_loop

    captured = {}

    def capture(outcome, **kwargs):
        captured["outcome"] = outcome
        return PermissionDecision.DENY

    monkeypatch.setattr("agent.permissions.prompt_user_for_permission", capture)

    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="ask_tool", input={"cmd": "foo"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AskTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    o = captured["outcome"]
    assert o.tool_name == "ask_tool"
    assert o.target  # non-empty
    assert o.op_type == "write"
    assert o.risk  # non-empty


def test_loop_non_tty_ask_results_in_deny(monkeypatch):
    """End-to-end: when stdin is not a TTY, ASK decisions become DENY
    because prompt_user_for_permission returns DENY without calling input.
    The loop treats that as a denied tool result."""
    from agent.loop import run_agent_loop

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="ask_tool", input={"cmd": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AskTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    assert result.status == "completed"
    tool_result_msg = result.messages[2]
    assert tool_result_msg["content"][0]["is_error"] is True


# ---------------------------------------------------------------------------
# Phase 4 audit event integration (contract L owner + boundary coverage)
# ---------------------------------------------------------------------------


class _CapturingLogger:
    """Mimics enough of `logging.Logger.info(msg, extra=...)` to let tests
    capture emitted audit events without standing up a real log file."""

    def __init__(self):
        self.events: list[dict] = []

    def info(self, message: str, extra: dict = None) -> None:
        record = dict(extra or {})
        record["_msg"] = message
        self.events.append(record)


def test_loop_emits_single_permission_decision_event_per_tool_call(monkeypatch):
    """Contract L: ASK → prompt → DENY flow ends with exactly ONE
    `permission.decision decision=DENY` (the final effective decision).
    `permission.prompted` is a separate event (owner: permissions.py)."""
    from agent.loop import run_agent_loop

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    logger = _CapturingLogger()
    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="ask_tool", input={"cmd": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AskTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
        audit_logger=logger,
        session_id="sid",
    )
    decisions = [e for e in logger.events if e.get("event") == "permission.decision"]
    prompted = [e for e in logger.events if e.get("event") == "permission.prompted"]
    assert len(decisions) == 1, f"expected 1 permission.decision, got {decisions}"
    assert decisions[0]["decision"] == "DENY"
    assert len(prompted) == 1
    assert "decision" not in prompted[0]


def test_permissions_emits_permission_prompted_event_only(monkeypatch):
    """Direct unit test of permissions.py: prompt_user_for_permission emits
    `permission.prompted` but NOT `permission.decision` (owner single-source)."""
    from agent.permissions import prompt_user_for_permission
    from agent.tools import PermissionOutcome

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    logger = _CapturingLogger()
    outcome = PermissionOutcome(
        decision=PermissionDecision.ASK,
        tool_name="bash",
        target="/tmp/write",
        op_type="write",
        risk="test",
    )
    result = prompt_user_for_permission(outcome, audit_logger=logger)
    assert result == PermissionDecision.ALLOW

    events = [e.get("event") for e in logger.events]
    assert "permission.prompted" in events
    assert "permission.decision" not in events


def test_loop_emits_tool_exec_start_end_with_duration(monkeypatch):
    """Contract L: each tool invocation produces exactly one tool.exec.start
    followed by one tool.exec.end carrying duration_ms, is_error, size."""
    from agent.loop import run_agent_loop

    monkeypatch.setattr(
        "agent.permissions.prompt_user_for_permission",
        lambda outcome, **kw: PermissionDecision.ALLOW,
    )
    logger = _CapturingLogger()
    client = _FakeClient([
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="ask_tool", input={"cmd": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="done")]),
    ])
    run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[_AskTool()],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
        audit_logger=logger,
        session_id="sid",
    )
    starts = [e for e in logger.events if e.get("event") == "tool.exec.start"]
    ends = [e for e in logger.events if e.get("event") == "tool.exec.end"]
    assert len(starts) == 1
    assert len(ends) == 1
    end = ends[0]
    assert end["tool"] == "ask_tool"
    assert "duration_ms" in end
    assert isinstance(end["duration_ms"], int)
    assert end["duration_ms"] >= 0
    assert end["is_error"] is False
    assert "size" in end


def test_loop_emits_compact_transition_event(monkeypatch):
    """Contract L: autocompact transitions produce compact.transition events."""
    from agent import compact as compact_mod
    from agent.loop import run_agent_loop

    monkeypatch.setattr(compact_mod, "CTX_WINDOW_TOKENS", 50)
    monkeypatch.setattr(
        "agent.loop.autocompact",
        lambda messages, *a, **kw: (_user_msg("<session_summary>c</session_summary>"),),
    )
    logger = _CapturingLogger()
    big_init = _user_msg("x" * 5000)
    client = _FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    run_agent_loop(
        client=client,
        initial_messages=(big_init,),
        tools=[],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
        audit_logger=logger,
        session_id="sid",
    )
    transitions = [e for e in logger.events if e.get("event") == "compact.transition"]
    assert transitions, "no compact.transition event emitted"
    assert any(t.get("reason") == "autocompact" for t in transitions)


def test_loop_does_not_emit_audit_when_logger_is_none():
    """Regression: loop with audit_logger=None is fully silent (no crashes)."""
    from agent.loop import run_agent_loop

    client = _FakeClient([_FakeResponse(content=[_FakeBlock(type="text", text="ok")])])
    result = run_agent_loop(
        client=client,
        initial_messages=(_user_msg("hi"),),
        tools=[],
        system_prompt=_stub_sys(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
        audit_logger=None,
        session_id=None,
    )
    assert result.status == "completed"

"""Tests for agent.compact + Phase 2 integration in agent.loop.

Testing strategy:
  - microcompact / autocompact units are exercised directly (no network).
  - Loop integration tests use FakeAnthropicClient (reused from test_loop.py
    style) plus pytest's `monkeypatch` to shrink CTX_WINDOW_TOKENS and
    to substitute autocompact with controlled stubs where the realistic
    version would require awkward queue juggling.
"""
from collections import deque

import pytest

from agent import compact as compact_mod
from agent.api import is_prompt_too_long
from agent.compact import (
    CLEARED_PLACEHOLDER,
    KEEP_RECENT_MESSAGES_IN_AUTO,
    KEEP_RECENT_TOOL_RESULTS,
    autocompact,
    configure_compact,
    estimate_tokens,
    microcompact,
    should_autocompact,
    should_microcompact,
)
from agent.config import CompactConfig
from agent.loop import run_agent_loop
from agent.types import State


# ---------------------------------------------------------------------------
# Test doubles (shared with test_loop style)
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


class _PromptTooLong(Exception):
    """Mimics Anthropic's BadRequestError shape for prompt_too_long."""

    def __init__(self, message="Error: prompt is too long: 210000 tokens > 200000"):
        super().__init__(message)
        self.status_code = 400
        self.body = {
            "error": {
                "type": "invalid_request_error",
                "message": message,
            }
        }


class _FakeMessages:
    """Queueable fake. Each queued entry is either a _FakeResponse or an
    Exception instance (which will be raised from .create())."""

    def __init__(self, queue):
        self._queue = queue
        self.calls = []

    def create(self, model, messages, system, tools, **kwargs):
        self.calls.append({"model": model, "messages": list(messages)})
        if not self._queue:
            raise RuntimeError("FakeAnthropicClient ran out of canned responses")
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(deque(responses))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_text(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_tool_use(tool_name, tool_id, input_dict):
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": input_dict}
        ],
    }


def _tool_result(tool_id, content):
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        ],
    }


def _stub_system_prompt():
    return [{"type": "text", "text": "You are a test agent."}]


def _make_tool_pair(tool_name, tool_id, result_content):
    return [
        _assistant_tool_use(tool_name, tool_id, {"q": "x"}),
        _tool_result(tool_id, result_content),
    ]


# ===========================================================================
# estimate_tokens
# ===========================================================================


def test_estimate_tokens_ballpark():
    msgs = (_user_text("a" * 350),)
    est = estimate_tokens(msgs)
    # 350 chars / 3.5 * 1.2 = 120; JSON overhead pushes slightly higher.
    # Accept a wide band — this is a conservative estimator.
    assert 100 <= est <= 200


def test_estimate_tokens_monotonic():
    short = (_user_text("hi"),)
    long = (_user_text("hi" * 1000),)
    assert estimate_tokens(long) > estimate_tokens(short)


# ===========================================================================
# should_microcompact / should_autocompact thresholds
# ===========================================================================


def test_should_microcompact_threshold_edges(monkeypatch):
    monkeypatch.setattr(compact_mod, "CTX_WINDOW_TOKENS", 1000)
    small = (_user_text("x" * 1000),)   # est ~345
    assert not should_microcompact(small)
    big = (_user_text("x" * 2200),)     # est ~770 > 700 (70% of 1000)
    assert should_microcompact(big)


def test_should_autocompact_threshold_edges(monkeypatch):
    monkeypatch.setattr(compact_mod, "CTX_WINDOW_TOKENS", 1000)
    medium = (_user_text("x" * 2100),)  # est ~735 → above 70%, below 85%
    assert should_microcompact(medium)
    assert not should_autocompact(medium)
    huge = (_user_text("x" * 2700),)    # est ~945 → above 85%
    assert should_autocompact(huge)


# ===========================================================================
# microcompact
# ===========================================================================


def test_microcompact_preserves_tool_use_pairing():
    messages = tuple(
        m
        for i in range(KEEP_RECENT_TOOL_RESULTS + 2)
        for m in _make_tool_pair("read_file", f"t{i}", "x" * 500)
    )
    new_msgs, changed = microcompact(messages)
    assert changed is True

    # Every tool_use must still have a tool_result with the same id.
    used_ids = []
    result_ids = []
    for m in new_msgs:
        for b in m["content"]:
            if b.get("type") == "tool_use":
                used_ids.append(b["id"])
            if b.get("type") == "tool_result":
                result_ids.append(b["tool_use_id"])
    assert used_ids == result_ids
    # Total message count unchanged
    assert len(new_msgs) == len(messages)


def test_microcompact_keeps_recent_N():
    messages = tuple(
        m
        for i in range(KEEP_RECENT_TOOL_RESULTS + 3)
        for m in _make_tool_pair("bash", f"b{i}", f"payload_{i}")
    )
    new_msgs, changed = microcompact(messages)
    assert changed is True

    cleared_payloads = [
        b["content"]
        for m in new_msgs
        for b in m["content"]
        if b.get("type") == "tool_result" and b["content"] == CLEARED_PLACEHOLDER
    ]
    kept_payloads = [
        b["content"]
        for m in new_msgs
        for b in m["content"]
        if b.get("type") == "tool_result" and b["content"] != CLEARED_PLACEHOLDER
    ]
    # 8 total tool_results; 5 kept, 3 cleared (oldest)
    assert len(kept_payloads) == KEEP_RECENT_TOOL_RESULTS
    assert len(cleared_payloads) == 3
    # Oldest are cleared, newest are kept
    assert "payload_0" not in "".join(kept_payloads)
    assert "payload_7" in "".join(kept_payloads)


def test_microcompact_skips_non_compactable_tools():
    messages = tuple(
        m
        for i in range(KEEP_RECENT_TOOL_RESULTS + 3)
        for m in _make_tool_pair("web_search", f"w{i}", f"payload_{i}")
    )
    new_msgs, changed = microcompact(messages)
    assert changed is False
    assert new_msgs == messages


def test_microcompact_returns_changed_false_on_noop():
    # Only a handful of results — below KEEP threshold
    messages = tuple(
        m
        for i in range(2)
        for m in _make_tool_pair("grep", f"g{i}", "small")
    )
    new_msgs, changed = microcompact(messages)
    assert changed is False
    assert new_msgs == messages


def test_microcompact_is_idempotent():
    messages = tuple(
        m
        for i in range(KEEP_RECENT_TOOL_RESULTS + 2)
        for m in _make_tool_pair("read_file", f"r{i}", "payload")
    )
    first, changed1 = microcompact(messages)
    assert changed1 is True
    second, changed2 = microcompact(first)
    assert changed2 is False
    assert second == first


# ===========================================================================
# autocompact
# ===========================================================================


def test_autocompact_returns_summary_with_user_role():
    client = FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="<session_summary>sum</session_summary>")])
    ])
    messages = tuple(_user_text(f"msg {i}") for i in range(KEEP_RECENT_MESSAGES_IN_AUTO + 2))
    new_msgs = autocompact(messages, client, "claude-test", _stub_system_prompt())
    assert new_msgs is not None
    # First message is the summary — must be role="user"
    assert new_msgs[0]["role"] == "user"
    # No "system" role anywhere
    assert all(m["role"] in ("user", "assistant") for m in new_msgs)
    # No fabricated assistant ack — summary is a single message, not a pair
    summary_text = new_msgs[0]["content"][0]["text"]
    assert "session_summary" in summary_text
    # The message immediately after the summary is NOT a synthetic assistant
    # ack — it's one of the kept messages (a user msg in this construction)
    assert new_msgs[1]["role"] == "user"


def test_autocompact_returns_none_when_below_keep_window():
    client = FakeClient([])  # should never be called
    messages = tuple(_user_text(f"m{i}") for i in range(KEEP_RECENT_MESSAGES_IN_AUTO))
    assert autocompact(messages, client, "m", _stub_system_prompt()) is None
    assert client.messages.calls == []


def test_autocompact_preserves_last_K_messages():
    client = FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="<session_summary>x</session_summary>")])
    ])
    messages = tuple(_user_text(f"m{i}") for i in range(10))
    new_msgs = autocompact(messages, client, "m", _stub_system_prompt())
    assert new_msgs is not None
    # Last K messages must appear verbatim at the tail
    tail = new_msgs[-KEEP_RECENT_MESSAGES_IN_AUTO:]
    assert tail == messages[-KEEP_RECENT_MESSAGES_IN_AUTO:]


def test_autocompact_avoids_orphan_tool_use():
    # Construct a 10-message history where the natural cutoff would slice
    # through a tool_use/tool_result pair. Verify the cutoff widens left.
    # Setup: msgs 0-4 plain, msg 5=tool_use, msg 6=tool_result, msgs 7-9 plain.
    # With KEEP=4 the natural cutoff = index 6. That slice starts with
    # a tool_result whose tool_use is at index 5 → not self-contained.
    # Expected: cutoff widens left to 5.
    msgs = list(_user_text(f"m{i}") for i in range(5))
    msgs.append(_assistant_tool_use("read_file", "pair", {"q": "x"}))
    msgs.append(_tool_result("pair", "result"))
    msgs.extend(_user_text(f"after{i}") for i in range(3))
    messages = tuple(msgs)

    client = FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="<session_summary>x</session_summary>")])
    ])
    new_msgs = autocompact(messages, client, "m", _stub_system_prompt())
    assert new_msgs is not None
    # Kept portion must include both the tool_use and its tool_result
    kept = new_msgs[1:]  # skip the summary message
    # Widened cutoff = 5, so kept = messages[5:] → 5 items
    assert len(kept) == 5
    assert kept[0] == messages[5]  # the tool_use
    assert kept[1] == messages[6]  # the tool_result


def test_autocompact_failure_returns_none():
    class Boom(Exception):
        pass

    client = FakeClient([Boom("network")])
    messages = tuple(_user_text(f"m{i}") for i in range(10))
    assert autocompact(messages, client, "m", _stub_system_prompt()) is None


def test_autocompact_empty_summary_returns_none():
    # Response with no text blocks → treated as failure
    client = FakeClient([_FakeResponse(content=[])])
    messages = tuple(_user_text(f"m{i}") for i in range(10))
    assert autocompact(messages, client, "m", _stub_system_prompt()) is None


def test_autocompact_missing_summary_tags_returns_none():
    client = FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="plain summary without tags")])
    ])
    messages = tuple(_user_text(f"m{i}") for i in range(10))
    assert autocompact(messages, client, "m", _stub_system_prompt()) is None


# ===========================================================================
# is_prompt_too_long (api helper)
# ===========================================================================


def test_is_prompt_too_long_recognizes_anthropic_400():
    err = _PromptTooLong()
    assert is_prompt_too_long(err) is True


def test_is_prompt_too_long_false_for_other_errors():
    class Generic(Exception):
        status_code = 500
    assert is_prompt_too_long(Generic("server blew up")) is False


# ===========================================================================
# Loop integration: circuit breaker
# ===========================================================================


def _force_compaction_thresholds(monkeypatch, window=100):
    """Tiny context window → almost any message triggers compaction."""
    monkeypatch.setattr(compact_mod, "CTX_WINDOW_TOKENS", window)


def test_circuit_breaker_trips_at_3_failures(monkeypatch):
    """3 consecutive proactive-Auto failures → compact_tripped=True."""
    _force_compaction_thresholds(monkeypatch)

    # Stub autocompact to always fail
    monkeypatch.setattr("agent.loop.autocompact", lambda *a, **kw: None)

    # Each turn: autocompact fails → micro no-op → API call succeeds with text
    # With max_turns=1 we get exactly one API call per entry. Need 3 runs?
    # Actually within ONE run_agent_loop() call, the loop goes:
    #   - iter 1: should_* True, autocompact fails (count=1), micro noop,
    #             fall through → API call returns text → completed.
    # That gives only 1 failure per run. We need 3 within one run to trip.
    # Solution: make the initial message huge but keep the loop running by
    # returning tool_use responses so the loop continues.
    traced = []
    responses = [
        _FakeResponse(
            content=[_FakeBlock(type="text", text="ok")],
            stop_reason="end_turn",
        )
    ]
    # Pad with more responses in case we need them
    for _ in range(10):
        responses.append(_FakeResponse(content=[_FakeBlock(type="text", text="ok")]))

    # To get 3 failures within 1 run_agent_loop, we need 3 turns where
    # should_autocompact is True AND autocompact fails. Drive 3 turns via
    # looping tool_use responses.

    # Create echo tool for tool_use loop
    from agent.tools import Tool, ToolResult
    from pydantic import BaseModel

    class _EchoIn(BaseModel):
        t: str

    class _Echo(Tool):
        name = "echo"
        reads_from_filesystem = False
        writes_to_filesystem = False
        destroys_data = False
        def description(self):
            return "echo"
        @property
        def input_model(self):
            return _EchoIn
        def execute(self, input):
            return ToolResult(output=f"e:{input.t}")

    # 3 turns of tool_use, then a final text response
    responses = []
    for i in range(3):
        responses.append(_FakeResponse(
            content=[_FakeBlock(type="tool_use", id=f"t{i}", name="echo", input={"t": "x"})],
            stop_reason="tool_use",
        ))
    responses.append(_FakeResponse(content=[_FakeBlock(type="text", text="done")]))

    # Big initial message so should_autocompact stays True across turns
    big = _user_text("x" * 5000)

    client = FakeClient(responses)
    result = run_agent_loop(
        client=client,
        initial_messages=(big,),
        tools=[_Echo()],
        system_prompt=_stub_system_prompt(),
        max_turns=10,
        primary_model="p",
        fallback_model="f",
        on_state_transition=traced.append,
    )

    assert result.status == "completed"
    # At some point compact_tripped should become True
    assert any(s.compact_tripped for s in traced), (
        f"no trip observed; transitions={[s.transition_reason for s in traced]}"
    )
    tripped_state = next(s for s in traced if s.compact_tripped)
    assert tripped_state.consecutive_compact_failures >= 3


def test_micro_works_after_trip(monkeypatch):
    """After circuit breaker trips, Micro can still run and succeed."""
    _force_compaction_thresholds(monkeypatch, window=50)
    # Always fail Auto
    monkeypatch.setattr("agent.loop.autocompact", lambda *a, **kw: None)

    from agent.tools import Tool, ToolResult
    from pydantic import BaseModel

    class _In(BaseModel):
        t: str

    class _RF(Tool):
        name = "read_file"
        reads_from_filesystem = True
        writes_to_filesystem = False
        destroys_data = False
        def description(self):
            return "rf"
        @property
        def input_model(self):
            return _In
        def execute(self, input):
            return ToolResult(output="big" * 500)

    # Drive enough tool_use turns to accumulate compactable results
    responses = []
    for i in range(KEEP_RECENT_TOOL_RESULTS + 4):
        responses.append(_FakeResponse(
            content=[_FakeBlock(type="tool_use", id=f"t{i}", name="read_file", input={"t": "x"})],
            stop_reason="tool_use",
        ))
    responses.append(_FakeResponse(content=[_FakeBlock(type="text", text="done")]))

    traced = []
    big_init = _user_text("x" * 5000)
    client = FakeClient(responses)
    result = run_agent_loop(
        client=client,
        initial_messages=(big_init,),
        tools=[_RF()],
        system_prompt=_stub_system_prompt(),
        max_turns=30,
        primary_model="p",
        fallback_model="f",
        on_state_transition=traced.append,
    )

    assert result.status in ("completed", "max_turns")
    # Must see at least one microcompact transition after trip
    saw_trip = False
    saw_micro_after_trip = False
    for s in traced:
        if s.compact_tripped:
            saw_trip = True
        if saw_trip and s.transition_reason == "microcompact":
            saw_micro_after_trip = True
            break
    assert saw_trip, "circuit breaker never tripped"
    assert saw_micro_after_trip, (
        f"no microcompact after trip; reasons={[s.transition_reason for s in traced]}"
    )


# ===========================================================================
# Loop integration: reactive compaction
# ===========================================================================


def test_reactive_compact_happy_path(monkeypatch):
    """Main 400 → reactive autocompact succeeds → retry succeeds."""
    # Avoid proactive compaction path by keeping window default; trigger
    # reactive only via raised exception.
    # 3 queued responses: (1) exception, (2) summary response for autocompact,
    # (3) final text.
    client = FakeClient([
        _PromptTooLong(),
        _FakeResponse(content=[_FakeBlock(type="text", text="<session_summary>s</session_summary>")]),
        _FakeResponse(content=[_FakeBlock(type="text", text="final")]),
    ])
    # Need enough messages so autocompact has material to summarize
    init = tuple(_user_text(f"m{i}") for i in range(KEEP_RECENT_MESSAGES_IN_AUTO + 2))

    traced = []
    result = run_agent_loop(
        client=client,
        initial_messages=init,
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
        on_state_transition=traced.append,
    )

    assert result.status == "completed"
    # Must have transitioned via reactive_compact
    assert any(s.transition_reason == "reactive_compact" for s in traced)
    # Final assistant message = "final"
    last_msg = result.messages[-1]
    assert last_msg["role"] == "assistant"
    assert last_msg["content"][0]["text"] == "final"


def test_reactive_compact_fails_when_autocompact_summary_call_fails(monkeypatch):
    """Main 400 → autocompact's own API call raises → prompt_too_long."""
    client = FakeClient([
        _PromptTooLong(),
        RuntimeError("summary API crashed"),
    ])
    init = tuple(_user_text(f"m{i}") for i in range(KEEP_RECENT_MESSAGES_IN_AUTO + 2))
    result = run_agent_loop(
        client=client,
        initial_messages=init,
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    assert result.status == "prompt_too_long"
    assert "autocompact failed" in result.reason


def test_reactive_compact_ignores_trip_state(monkeypatch):
    """Even when compact_tripped=True, reactive path still runs."""
    # Patch run_agent_loop entry by using a pre-tripped initial state is
    # not directly possible (loop creates State internally). Instead:
    # force proactive Auto to fail 3 times first by stubbing agent.loop.autocompact
    # to a side-effecting stub that fails first N times then succeeds for reactive.
    call_log = {"count": 0}

    def controlled(messages, client, model, system_prompt, **kw):
        call_log["count"] += 1
        if call_log["count"] <= 3:
            return None
        return (_user_text("<session_summary>compressed</session_summary>"),)

    monkeypatch.setattr("agent.loop.autocompact", controlled)
    _force_compaction_thresholds(monkeypatch, window=50)

    # Drive 3 proactive failures via tool loop, then raise prompt_too_long,
    # then succeed via reactive.
    from agent.tools import Tool, ToolResult
    from pydantic import BaseModel

    class _In(BaseModel):
        t: str

    class _E(Tool):
        name = "echo"
        reads_from_filesystem = False
        writes_to_filesystem = False
        destroys_data = False
        def description(self):
            return "e"
        @property
        def input_model(self):
            return _In
        def execute(self, input):
            return ToolResult(output="r")

    responses = []
    # 3 turns of tool_use to accumulate 3 Auto failures
    for i in range(3):
        responses.append(_FakeResponse(
            content=[_FakeBlock(type="tool_use", id=f"t{i}", name="echo", input={"t": "x"})],
            stop_reason="tool_use",
        ))
    # Turn 4: prompt_too_long raised → reactive kicks in (stub handles compaction
    # without a summary API call, since it returns a prebuilt tuple)
    responses.append(_PromptTooLong())
    # Final retry after reactive compaction
    responses.append(_FakeResponse(content=[_FakeBlock(type="text", text="final")]))

    traced = []
    big_init = _user_text("x" * 5000)
    client = FakeClient(responses)
    result = run_agent_loop(
        client=client,
        initial_messages=(big_init,),
        tools=[_E()],
        system_prompt=_stub_system_prompt(),
        max_turns=10,
        primary_model="p",
        fallback_model="f",
        on_state_transition=traced.append,
    )

    assert result.status == "completed"
    # Must have seen both: trip and subsequent reactive_compact
    saw_trip = any(s.compact_tripped for s in traced)
    assert saw_trip, "breaker never tripped"
    reactive_states = [s for s in traced if s.transition_reason == "reactive_compact"]
    assert len(reactive_states) == 1
    # Reactive success clears compact_tripped
    assert reactive_states[0].compact_tripped is False


def test_reactive_compact_only_attempts_once(monkeypatch):
    """If retry after reactive compact still returns 400, loop exits with
    status=prompt_too_long and does NOT re-enter reactive path."""
    # Queue: 400 → summary OK → 400 → [would-be-second-summary — MUST NOT be consumed]
    sentinel_response = _FakeResponse(content=[_FakeBlock(type="text", text="SENTINEL")])
    client = FakeClient([
        _PromptTooLong(),
        _FakeResponse(content=[_FakeBlock(type="text", text="<session_summary>s</session_summary>")]),
        _PromptTooLong(),
        sentinel_response,  # must remain unconsumed
    ])
    init = tuple(_user_text(f"m{i}") for i in range(KEEP_RECENT_MESSAGES_IN_AUTO + 2))
    result = run_agent_loop(
        client=client,
        initial_messages=init,
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    assert result.status == "prompt_too_long"
    assert "still too long" in result.reason
    # Sentinel must remain: the 4th response should NOT have been consumed
    assert client.messages._queue, "sentinel was consumed — reactive attempted twice"
    assert client.messages._queue[0] is sentinel_response


# ===========================================================================
# Loop integration: contracts D, D-bis, E
# ===========================================================================


def test_loop_no_infinite_loop_on_micro_noop(monkeypatch):
    """If should_microcompact=True but microcompact changes nothing, loop
    must fall through to the API call rather than spinning forever."""
    _force_compaction_thresholds(monkeypatch, window=50)
    # Disable Auto path entirely by forcing it to fail (so micro is the only
    # possible "compaction"). microcompact with empty compactable content →
    # returns changed=False.
    monkeypatch.setattr("agent.loop.autocompact", lambda *a, **kw: None)

    client = FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    # Big init message is plain text — NOT a compactable tool_result
    big_init = _user_text("x" * 5000)
    result = run_agent_loop(
        client=client,
        initial_messages=(big_init,),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=5,
        primary_model="p",
        fallback_model="f",
    )
    # Made it through — no infinite loop. API was called once.
    assert result.status == "completed"
    assert len(client.messages.calls) == 1


def test_autocompact_that_did_not_reduce_tokens_is_treated_as_failure(monkeypatch):
    """If proactive autocompact returns new_msgs with token count >= old,
    loop treats it as a failure and increments consecutive_compact_failures."""
    _force_compaction_thresholds(monkeypatch, window=50)

    # Controlled autocompact: returns a new tuple of messages whose JSON size
    # is >= original → estimate_tokens(new) >= estimate_tokens(old)
    def bloated(messages, client, model, system_prompt, **kw):
        # Append one message to original — guaranteed larger
        extra = _user_text("extra padding padding padding padding padding" * 20)
        return tuple(messages) + (extra,)

    monkeypatch.setattr("agent.loop.autocompact", bloated)

    client = FakeClient([
        _FakeResponse(content=[_FakeBlock(type="text", text="ok")]),
    ])
    big_init = _user_text("x" * 5000)
    traced = []
    result = run_agent_loop(
        client=client,
        initial_messages=(big_init,),
        tools=[],
        system_prompt=_stub_system_prompt(),
        max_turns=3,
        primary_model="p",
        fallback_model="f",
        on_state_transition=traced.append,
    )
    assert result.status == "completed"
    # At least one transition should show consecutive_compact_failures >= 1
    failure_states = [s for s in traced if s.consecutive_compact_failures >= 1]
    assert failure_states, "Auto-no-progress was not counted as a failure"


def test_compaction_success_resets_failure_streak(monkeypatch):
    """Phase 2 contract: the circuit-breaker counter resets when Auto OR
    Micro successfully compacts. Tool_use / output_recovery do NOT reset the
    counter (contract E preserves those new State fields across transitions).

    Scenario: proactive Auto fails twice, then succeeds → counter goes
    0 → 1 → 2 → 0. No trip observed (threshold 3 not reached)."""
    _force_compaction_thresholds(monkeypatch, window=50)

    call = {"n": 0}
    def flaky_auto(messages, client, model, system_prompt, **kw):
        call["n"] += 1
        if call["n"] <= 2:
            return None
        # Third call: succeed with a shorter tuple
        return (_user_text("<session_summary>ok</session_summary>"),)

    monkeypatch.setattr("agent.loop.autocompact", flaky_auto)

    from agent.tools import Tool, ToolResult
    from pydantic import BaseModel

    class _In(BaseModel):
        t: str

    class _E(Tool):
        name = "echo"
        reads_from_filesystem = False
        writes_to_filesystem = False
        destroys_data = False
        def description(self):
            return "e"
        @property
        def input_model(self):
            return _In
        def execute(self, input):
            return ToolResult(output="r")

    # 2 tool_use responses to drive 2 more iterations (in each iter,
    # flaky_auto fails the first two times), then a final text. Third iter
    # sees auto succeed → loop continues → API call returns final text.
    responses = [
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t0", name="echo", input={"t": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(
            content=[_FakeBlock(type="tool_use", id="t1", name="echo", input={"t": "x"})],
            stop_reason="tool_use",
        ),
        _FakeResponse(content=[_FakeBlock(type="text", text="final")]),
    ]
    traced = []
    big_init = _user_text("x" * 5000)
    client = FakeClient(responses)
    result = run_agent_loop(
        client=client,
        initial_messages=(big_init,),
        tools=[_E()],
        system_prompt=_stub_system_prompt(),
        max_turns=10,
        primary_model="p",
        fallback_model="f",
        on_state_transition=traced.append,
    )

    assert result.status == "completed"

    counters = [s.consecutive_compact_failures for s in traced]
    assert 1 in counters, f"expected a failure iteration; got {counters}"
    assert 2 in counters, f"expected a second failure iteration; got {counters}"
    # After Auto success, counter must reset to 0
    last_autocompact = next(
        (s for s in reversed(traced) if s.transition_reason == "autocompact"),
        None,
    )
    assert last_autocompact is not None, "autocompact success never observed"
    assert last_autocompact.consecutive_compact_failures == 0
    assert last_autocompact.compact_tripped is False
    # And the trip threshold (3) was never reached
    assert not any(s.compact_tripped for s in traced)


# ===========================================================================
# REPL handling of prompt_too_long
# ===========================================================================


def test_repl_handles_prompt_too_long_status(monkeypatch, capsys):
    """REPL must show an overflow message AND preserve history (do not
    auto-clear) when run_agent_loop returns prompt_too_long."""
    from agent import main as main_mod
    from agent.types import AgentResult

    preserved_history_msg = _user_text("earlier work")

    def fake_loop(**kwargs):
        return AgentResult(
            status="prompt_too_long",
            messages=(preserved_history_msg, _user_text("overflow trigger")),
            reason="autocompact failed on prompt_too_long recovery",
        )

    monkeypatch.setattr(main_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(main_mod, "init_client", lambda: object())
    monkeypatch.setattr(main_mod, "get_tools", lambda: [])
    monkeypatch.setattr(main_mod, "build_system_prompt", lambda **kw: _stub_system_prompt())

    inputs = iter(["trigger overflow", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    main_mod.repl()
    out = capsys.readouterr().out
    assert "context overflow" in out
    assert "history preserved" in out


# ===========================================================================
# Phase 4 config hook
# ===========================================================================


def test_compact_configure_hook_overrides_module_constants(monkeypatch):
    """Phase 4 contract: configure_compact(cfg) pushes CompactConfig values
    into the module constants so subsequent should_* / autocompact calls
    see the new thresholds without any call-site changes."""
    # Save originals so we can restore after
    originals = {
        "CTX_WINDOW_TOKENS": compact_mod.CTX_WINDOW_TOKENS,
        "MICRO_COMPACT_THRESHOLD": compact_mod.MICRO_COMPACT_THRESHOLD,
        "AUTO_COMPACT_THRESHOLD": compact_mod.AUTO_COMPACT_THRESHOLD,
        "KEEP_RECENT_TOOL_RESULTS": compact_mod.KEEP_RECENT_TOOL_RESULTS,
        "KEEP_RECENT_MESSAGES_IN_AUTO": compact_mod.KEEP_RECENT_MESSAGES_IN_AUTO,
        "AUTO_COMPACT_MAX_OUTPUT": compact_mod.AUTO_COMPACT_MAX_OUTPUT,
        "MAX_CONSECUTIVE_COMPACT_FAILURES": compact_mod.MAX_CONSECUTIVE_COMPACT_FAILURES,
    }
    try:
        cfg = CompactConfig(
            ctx_window_tokens=1000,
            micro_threshold=0.5,
            auto_threshold=0.8,
            max_consecutive_failures=7,
            keep_recent_tool_results=2,
            keep_recent_messages_in_auto=3,
            auto_compact_max_output=512,
        )
        configure_compact(cfg)

        assert compact_mod.CTX_WINDOW_TOKENS == 1000
        assert compact_mod.MICRO_COMPACT_THRESHOLD == 0.5
        assert compact_mod.AUTO_COMPACT_THRESHOLD == 0.8
        assert compact_mod.MAX_CONSECUTIVE_COMPACT_FAILURES == 7
        assert compact_mod.KEEP_RECENT_TOOL_RESULTS == 2
        assert compact_mod.KEEP_RECENT_MESSAGES_IN_AUTO == 3
        assert compact_mod.AUTO_COMPACT_MAX_OUTPUT == 512

        # And the helper functions pick up the new threshold:
        # 1000 tokens * 0.5 = 500 → should_microcompact at ~500+ tokens
        msgs = (_user_text("x" * 1500),)   # ~515 est
        assert should_microcompact(msgs)
    finally:
        for k, v in originals.items():
            setattr(compact_mod, k, v)

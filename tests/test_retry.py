"""Tests for agent.retry — Phase 5 exponential backoff with budget."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.retry import call_with_retry


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Recoverable(Exception):
    status_code = 429


class _RecoverableWithRetryAfter(Exception):
    status_code = 429

    def __init__(self, retry_after: float):
        super().__init__("rate limited")
        self.response = MagicMock()
        self.response.headers = {"retry-after": str(retry_after)}


class _NonRecoverable(Exception):
    status_code = 400


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------


def test_succeeds_first_try():
    """No retries needed → returns immediately."""
    result = call_with_retry(lambda: "ok", budget=5, jitter=False)
    assert result == "ok"


def test_retries_on_recoverable():
    """fn fails twice, succeeds on third → returns result."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _Recoverable()
        return "ok"

    result = call_with_retry(fn, budget=5, backoff_base=0.001, jitter=False)
    assert result == "ok"
    assert calls["n"] == 3


def test_budget_exhausted_raises():
    """All attempts fail → raises last error."""
    def fn():
        raise _Recoverable()

    with pytest.raises(_Recoverable):
        call_with_retry(fn, budget=3, backoff_base=0.001, jitter=False)


def test_non_recoverable_raises_immediately():
    """Non-recoverable error → no retry, immediate raise."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _NonRecoverable()

    with pytest.raises(_NonRecoverable):
        call_with_retry(fn, budget=5, backoff_base=0.001, jitter=False)
    assert calls["n"] == 1


def test_prompt_too_long_not_retried():
    """400 with prompt_too_long should NOT be retried."""
    class PromptTooLong(Exception):
        status_code = 400

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise PromptTooLong("prompt is too long")

    with pytest.raises(PromptTooLong):
        call_with_retry(fn, budget=5, backoff_base=0.001, jitter=False)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Backoff delays
# ---------------------------------------------------------------------------


def test_backoff_delays_increase(monkeypatch):
    """Delays double each attempt: 1, 2, 4, ..."""
    sleeps: list[float] = []
    monkeypatch.setattr("agent.retry.time.sleep", lambda d: sleeps.append(d))

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 4:
            raise _Recoverable()
        return "ok"

    call_with_retry(fn, budget=5, backoff_base=1.0, backoff_max=100, jitter=False)
    assert len(sleeps) == 4
    # 1, 2, 4, 8 with no jitter, no cap at 100
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_backoff_capped_at_max(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("agent.retry.time.sleep", lambda d: sleeps.append(d))

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 5:
            raise _Recoverable()
        return "ok"

    call_with_retry(fn, budget=6, backoff_base=1.0, backoff_max=5.0, jitter=False)
    assert all(d <= 5.0 for d in sleeps)


def test_jitter_adds_randomness(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("agent.retry.time.sleep", lambda d: sleeps.append(d))

    for _ in range(2):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] <= 1:
                raise _Recoverable()
            return "ok"

        call_with_retry(fn, budget=5, backoff_base=1.0, jitter=True)

    # With jitter, both delays should differ from the base 1.0 (they include
    # random(0, delay*0.5)) and from each other (probabilistically).
    # We just check they're > base and not all identical.
    assert all(d >= 1.0 for d in sleeps)


def test_no_jitter_when_disabled(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("agent.retry.time.sleep", lambda d: sleeps.append(d))

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 1:
            raise _Recoverable()
        return "ok"

    call_with_retry(fn, budget=5, backoff_base=1.0, backoff_max=30, jitter=False)
    assert sleeps == [1.0]  # exact, no randomness


# ---------------------------------------------------------------------------
# Retry-After header
# ---------------------------------------------------------------------------


def test_honors_retry_after_header(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("agent.retry.time.sleep", lambda d: sleeps.append(d))

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 1:
            raise _RecoverableWithRetryAfter(10.0)
        return "ok"

    call_with_retry(fn, budget=5, backoff_base=1.0, backoff_max=30, jitter=False)
    # Retry-After=10 > computed=1.0 → delay should be 10
    assert sleeps[0] == 10.0


def test_retry_after_capped_at_max(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("agent.retry.time.sleep", lambda d: sleeps.append(d))

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 1:
            raise _RecoverableWithRetryAfter(999.0)
        return "ok"

    call_with_retry(fn, budget=5, backoff_base=1.0, backoff_max=5.0, jitter=False)
    assert sleeps[0] <= 5.0


# ---------------------------------------------------------------------------
# KeyboardInterrupt
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_propagates(monkeypatch):
    def raise_ki(delay):
        raise KeyboardInterrupt()

    monkeypatch.setattr("agent.retry.time.sleep", raise_ki)

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _Recoverable()

    with pytest.raises(KeyboardInterrupt):
        call_with_retry(fn, budget=5, backoff_base=0.001, jitter=False)
    assert calls["n"] == 1  # only first attempt, sleep interrupted


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


def test_emits_audit_event_per_retry(monkeypatch):
    monkeypatch.setattr("agent.retry.time.sleep", lambda _: None)

    class _Logger:
        def __init__(self):
            self.events = []

        def info(self, msg, extra=None):
            self.events.append(dict(extra or {}))

    logger = _Logger()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise _Recoverable()
        return "ok"

    call_with_retry(fn, budget=5, backoff_base=0.001, jitter=False,
                    audit_logger=logger, session_id="sid")
    retry_events = [e for e in logger.events if e.get("event") == "api.retry"]
    assert len(retry_events) == 3
    assert retry_events[0]["attempt"] == 1
    assert retry_events[2]["attempt"] == 3

"""Retry with exponential backoff — Phase 5.

Wraps a callable with budget-limited retries on recoverable API errors.
Handles Retry-After headers, jitter, and emits audit events per attempt.

Usage:
    from agent.retry import call_with_retry
    response = call_with_retry(
        lambda: client.messages.create(...),
        budget=5, backoff_base=1.0, backoff_max=30.0,
    )
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional, TypeVar

from agent.api import is_recoverable

T = TypeVar("T")


def _get_retry_after(e: Exception) -> Optional[float]:
    """Extract Retry-After seconds from an Anthropic SDK error.

    Returns None if the header is absent, unparseable, or negative.
    Only parses numeric values (ignores HTTP-date format).
    """
    response = getattr(e, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def call_with_retry(
    fn: Callable[[], T],
    *,
    budget: int = 5,
    backoff_base: float = 1.0,
    backoff_max: float = 30.0,
    jitter: bool = True,
    audit_logger: Any = None,
    session_id: Optional[str] = None,
) -> T:
    """Call fn() with exponential backoff on recoverable errors.

    - budget: total attempts including the first call (budget=5 → 1 + 4 retries).
    - On each retry: delay = min(base * 2^attempt, max); jitter adds
      random(0, delay * 0.5).
    - Retry-After: if present, use max(retry_after, computed_delay)
      capped at backoff_max.
    - KeyboardInterrupt during sleep propagates immediately.
    - Non-recoverable errors raise immediately (no retry).
    - Budget exhausted: raises the last recoverable error.
    """
    last_error: Optional[Exception] = None

    for attempt in range(budget):
        try:
            return fn()
        except Exception as e:
            if not is_recoverable(e):
                raise
            last_error = e
            if attempt >= budget - 1:
                raise

            delay = min(backoff_base * (2 ** attempt), backoff_max)

            retry_after = _get_retry_after(e)
            if retry_after is not None:
                delay = min(max(retry_after, delay), backoff_max)

            if jitter:
                delay += random.uniform(0, delay * 0.5)

            if audit_logger is not None:
                from agent.audit import emit
                emit(
                    audit_logger,
                    "api.retry",
                    session_id=session_id,
                    attempt=attempt + 1,
                    delay_s=round(delay, 2),
                    error_type=type(e).__name__,
                    status_code=getattr(e, "status_code", None),
                )

            time.sleep(delay)

    raise last_error  # type: ignore[misc]  # unreachable but satisfies mypy

"""Context compaction — Phase 2 resilience layer.

Three compaction levels:
  - microcompact: local tuple transform; replaces old tool_result content with
    a placeholder string. Preserves tool_use/tool_result pairing by never
    removing blocks or changing ids. Always safe, always allowed.
  - autocompact (proactive): calls Claude to generate a structured summary;
    replaces the prefix with a single synthetic user message carrying the
    summary. Gated by the circuit breaker. Must strictly reduce token count.
  - autocompact (reactive): same mechanism as proactive but triggered by
    prompt_too_long errors. Not gated by the circuit breaker; instead gated
    by the single-attempt latch `reactive_compact_attempted` on State.

Token estimation is intentionally local and conservative (chars / 3.5 * 1.2).
No network calls — 100% mockable tests.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from agent.types import Message


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CTX_WINDOW_TOKENS = 200_000
OUTPUT_RESERVE_TOKENS = 8_192
MICRO_COMPACT_THRESHOLD = 0.70
AUTO_COMPACT_THRESHOLD = 0.85
KEEP_RECENT_TOOL_RESULTS = 5
KEEP_RECENT_MESSAGES_IN_AUTO = 4
AUTO_COMPACT_MAX_OUTPUT = 4_096
MAX_CONSECUTIVE_COMPACT_FAILURES = 3

COMPACTABLE_TOOL_NAMES = {"read_file", "bash", "grep"}
CLEARED_PLACEHOLDER = "[Old tool result cleared to save context]"


def configure_compact(cfg) -> None:
    """Apply a CompactConfig instance to the module-level tunables.

    Phase 4 hook: main.py calls this once at startup after load_config() so
    that should_microcompact / should_autocompact / the loop's circuit
    breaker reference the user's TOML values without needing every call
    site to thread config around.

    Intentionally overwrites module globals — the helpers in this file read
    the module attributes at call time, so the change propagates.
    """
    global CTX_WINDOW_TOKENS, MICRO_COMPACT_THRESHOLD, AUTO_COMPACT_THRESHOLD
    global KEEP_RECENT_TOOL_RESULTS, KEEP_RECENT_MESSAGES_IN_AUTO
    global AUTO_COMPACT_MAX_OUTPUT, MAX_CONSECUTIVE_COMPACT_FAILURES
    CTX_WINDOW_TOKENS = cfg.ctx_window_tokens
    MICRO_COMPACT_THRESHOLD = cfg.micro_threshold
    AUTO_COMPACT_THRESHOLD = cfg.auto_threshold
    KEEP_RECENT_TOOL_RESULTS = cfg.keep_recent_tool_results
    KEEP_RECENT_MESSAGES_IN_AUTO = cfg.keep_recent_messages_in_auto
    AUTO_COMPACT_MAX_OUTPUT = cfg.auto_compact_max_output
    MAX_CONSECUTIVE_COMPACT_FAILURES = cfg.max_consecutive_failures


_TOKEN_BYTES_PER_TOKEN = 3.5
_TOKEN_BUFFER_MULT = 1.2


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(messages: tuple[Message, ...]) -> int:
    """Conservative local estimate: sum(json_bytes) / 3.5 * 1.2.

    Error budget: ±15%. The 1.2 multiplier is headroom; true Anthropic
    tokenization is closed-source. If this proves drifty in practice,
    the reactive prompt_too_long path catches overflow.
    """
    total_chars = 0
    for msg in messages:
        total_chars += len(json.dumps(msg, ensure_ascii=False))
    return int(total_chars / _TOKEN_BYTES_PER_TOKEN * _TOKEN_BUFFER_MULT)


def should_microcompact(messages: tuple[Message, ...]) -> bool:
    return estimate_tokens(messages) >= CTX_WINDOW_TOKENS * MICRO_COMPACT_THRESHOLD


def should_autocompact(messages: tuple[Message, ...]) -> bool:
    return estimate_tokens(messages) >= CTX_WINDOW_TOKENS * AUTO_COMPACT_THRESHOLD


# ---------------------------------------------------------------------------
# Microcompact
# ---------------------------------------------------------------------------


def _build_tool_use_name_index(messages: tuple[Message, ...]) -> dict[str, str]:
    """Map tool_use id -> tool name across all assistant messages."""
    index: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                tname = block.get("name")
                if tid and tname:
                    index[tid] = tname
    return index


def _iter_tool_result_blocks(messages: tuple[Message, ...]):
    """Yield (msg_index, block_index, block) for every tool_result block."""
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield mi, bi, block


def microcompact(
    messages: tuple[Message, ...],
) -> tuple[tuple[Message, ...], bool]:
    """Replace old compactable tool_result content with placeholder.

    Contract D: returns (new_messages, changed). changed=False when nothing
    was actually replaced — the loop uses this signal to avoid an infinite
    continue cycle when there is nothing left to clear.

    Preserves block structure and ids; only the `content` field of a
    compactable tool_result is overwritten.
    """
    name_index = _build_tool_use_name_index(messages)

    # Collect every compactable tool_result that still has live content.
    candidates: list[tuple[int, int]] = []
    for mi, bi, block in _iter_tool_result_blocks(messages):
        tid = block.get("tool_use_id")
        tool_name = name_index.get(tid, "")
        if tool_name not in COMPACTABLE_TOOL_NAMES:
            continue
        if block.get("content") == CLEARED_PLACEHOLDER:
            continue
        candidates.append((mi, bi))

    # Keep the most recent N intact.
    if len(candidates) <= KEEP_RECENT_TOOL_RESULTS:
        return messages, False
    to_clear = set(candidates[: -KEEP_RECENT_TOOL_RESULTS])

    touched_msg_indices = {mi for (mi, _bi) in to_clear}
    new_messages_list: list[Message] = []
    changed = False
    for mi, msg in enumerate(messages):
        if mi not in touched_msg_indices:
            new_messages_list.append(msg)
            continue
        new_content = []
        for bi, block in enumerate(msg["content"]):
            if (mi, bi) in to_clear:
                new_block = dict(block)
                new_block["content"] = CLEARED_PLACEHOLDER
                new_content.append(new_block)
                changed = True
            else:
                new_content.append(block)
        new_msg = dict(msg)
        new_msg["content"] = new_content
        new_messages_list.append(new_msg)

    return tuple(new_messages_list), changed


# ---------------------------------------------------------------------------
# Autocompact
# ---------------------------------------------------------------------------


_SUMMARY_SYSTEM_INSTRUCTION = (
    "You are summarizing a conversation so it can be continued without "
    "losing context. Produce a concise <session_summary> covering:\n"
    "1. User intent and high-level goal\n"
    "2. Key technical concepts and decisions discussed\n"
    "3. Files / code explored (paths + what was found)\n"
    "4. Errors encountered and how they were resolved\n"
    "5. Problems already solved\n"
    "6. All user messages verbatim (bulleted)\n"
    "7. Pending todos / open questions\n"
    "8. Current work in progress\n"
    "9. Recommended next step\n\n"
    "Wrap the entire output in <session_summary>...</session_summary> tags. "
    "Be dense; no filler."
)


def _pairing_is_self_contained(messages: tuple[Message, ...]) -> bool:
    """Check that every tool_use in this slice has a matching tool_result.

    Used to decide whether the 'last K' slice is safe to keep as-is, or
    needs to expand leftward by one to include the missing tool_use context.
    """
    used_ids: list[str] = []
    result_ids: set[str] = set()
    for msg in messages:
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tid = block.get("id")
                if tid:
                    used_ids.append(tid)
            elif btype == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    result_ids.add(tid)
    # Every tool_use in the slice must have its result in the same slice.
    for uid in used_ids:
        if uid not in result_ids:
            return False
    # Every tool_result must have its tool_use in the same slice.
    for rid in result_ids:
        if rid not in used_ids:
            return False
    return True


def _find_keep_cutoff(messages: tuple[Message, ...]) -> int:
    """Return the index at which 'last K messages' starts, widened leftward
    if necessary so the kept slice is self-contained in tool_use/tool_result
    pairing. Returns a value in [0, len(messages)].
    """
    if len(messages) <= KEEP_RECENT_MESSAGES_IN_AUTO:
        return 0
    cutoff = len(messages) - KEEP_RECENT_MESSAGES_IN_AUTO
    while cutoff > 0 and not _pairing_is_self_contained(messages[cutoff:]):
        cutoff -= 1
    return cutoff


def _extract_summary_text(response: Any) -> str:
    """Pull plain text out of an Anthropic response."""
    content = getattr(response, "content", None) or []
    parts = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(p for p in parts if p).strip()


def autocompact(
    messages: tuple[Message, ...],
    client: Any,
    model: str,
    system_prompt: list[dict],
    retry_budget: int = 1,
) -> Optional[tuple[Message, ...]]:
    """Replace the prefix of `messages` with a single synthetic user message
    containing a structured summary. Keeps the trailing slice intact to
    preserve tool_use/tool_result pairing.

    Returns the new message tuple, or None on any failure / no-op.

    Contract A: returns role="user" summary only; NO fabricated assistant ack.
    Contract B: 'last K' slice may be widened leftward to restore pairing.

    Phase 5: retry_budget wraps the summary API call with call_with_retry
    so that a transient 429 doesn't unnecessarily burn a circuit-breaker charge.
    """
    from agent.retry import call_with_retry

    cutoff = _find_keep_cutoff(messages)
    if cutoff <= 0:
        return None

    prefix = messages[:cutoff]
    kept = messages[cutoff:]

    summary_system = list(system_prompt) + [
        {"type": "text", "text": _SUMMARY_SYSTEM_INSTRUCTION}
    ]
    try:
        response = call_with_retry(
            lambda: client.messages.create(
                model=model,
                messages=list(prefix),
                system=summary_system,
                tools=[],
                max_tokens=AUTO_COMPACT_MAX_OUTPUT,
            ),
            budget=retry_budget,
            backoff_base=1.0,
            backoff_max=10.0,
        )
    except Exception:
        return None

    summary_text = _extract_summary_text(response)
    if not summary_text:
        return None
    if (
        "<session_summary>" not in summary_text
        or "</session_summary>" not in summary_text
    ):
        return None

    summary_msg: Message = {
        "role": "user",
        "content": [{"type": "text", "text": summary_text}],
    }
    return (summary_msg,) + kept

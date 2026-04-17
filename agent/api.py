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


def is_prompt_too_long(error: Exception) -> bool:
    """Detect Anthropic's 'prompt is too long' 400 error across SDK error shapes.

    Matches when EITHER:
      - status_code == 400 AND message substring contains 'prompt is too long', OR
      - error.body has error.type == 'invalid_request_error' with matching message

    Fail-closed: anything unrecognized returns False.
    """
    status_code = getattr(error, "status_code", None)
    message = str(error).lower()
    if status_code == 400 and "prompt is too long" in message:
        return True
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") or {}
        if (
            inner.get("type") == "invalid_request_error"
            and "prompt is too long" in str(inner.get("message", "")).lower()
        ):
            return True
    return False


def build_assistant_message(response: Any) -> dict:
    """Convert an API response into a message dict suitable for appending
    to the messages list.

    Accepts either a ParsedResponse (from the adapter layer) or a raw
    Anthropic SDK response (legacy path for back-compat / direct tests).
    """
    from agent.adapter import ParsedResponse
    if isinstance(response, ParsedResponse):
        return {"role": "assistant", "content": response.content_blocks}

    # Legacy path: raw SDK response with attribute-access blocks
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

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

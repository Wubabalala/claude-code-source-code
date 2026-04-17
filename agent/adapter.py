"""LLM adapter layer — request/response formatting abstraction.

This is NOT a provider abstraction. Both adapters use the Anthropic SDK
transport. The difference is purely in how messages and system prompts
are formatted before the API call and how responses are parsed after.

AnthropicAdapter:          pass-through (native Anthropic block format)
FlattenedMessageAdapter:   flattens text-block arrays to plain strings
                           (for LiteLLM / OpenAI-compatible proxies)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Callable

from agent.types import Message


# ---------------------------------------------------------------------------
# ParsedResponse — provider-neutral envelope returned by call_model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedResponse:
    """Provider-neutral API response envelope.

    Attributes:
        content_blocks: list of dicts, each with at least a ``type`` key.
            Text blocks: ``{"type": "text", "text": "..."}``
            Tool-use blocks: ``{"type": "tool_use", "id": ..., "name": ..., "input": ...}``
        tool_use_blocks: subset of *content_blocks* where ``type == "tool_use"``.
        stop_reason: raw stop_reason string from the API (e.g. ``"end_turn"``,
            ``"max_tokens"``, ``"tool_use"``).
        usage: raw SDK usage object — **not normalised**.  Both adapters in
            this module use the Anthropic SDK, so the shape is always
            Anthropic's (``input_tokens``, ``output_tokens``, ``cache_*``).
            If a future adapter uses a different SDK, normalise at that time.
        streamed: whether this response was produced via the streaming path.
    """

    content_blocks: list[dict]
    tool_use_blocks: list[dict]
    stop_reason: Optional[str]
    usage: Any
    streamed: bool = False


# ---------------------------------------------------------------------------
# Internal helper — parse a raw Anthropic SDK response into ParsedResponse
# ---------------------------------------------------------------------------


def _parse_raw_response(response: Any, *, streamed: bool = False) -> ParsedResponse:
    """Convert a raw Anthropic SDK response object into a ParsedResponse.

    Reads ``.content``, ``.stop_reason``, ``.usage`` via attribute access
    (works with both real SDK objects and test fakes that set these attrs).
    """
    content_blocks: list[dict] = []
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

    tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

    return ParsedResponse(
        content_blocks=content_blocks,
        tool_use_blocks=tool_use_blocks,
        stop_reason=getattr(response, "stop_reason", None),
        usage=getattr(response, "usage", None),
        streamed=streamed,
    )


# ---------------------------------------------------------------------------
# LLMAdapter ABC
# ---------------------------------------------------------------------------


class LLMAdapter(ABC):
    """Request/response formatting strategy.

    Does NOT include ``build_tool_result_block`` — that function is
    provider-agnostic and lives in ``agent.api``.
    """

    @abstractmethod
    def format_system(self, system_prompt: list[dict]) -> Any:
        """Format system prompt for the provider."""

    @abstractmethod
    def format_messages(self, messages: tuple[Message, ...]) -> list:
        """Format conversation messages for the provider."""

    @abstractmethod
    def build_tool_schemas(self, tools: list) -> list[dict]:
        """Build tool schema list from Tool objects."""

    @abstractmethod
    def call_model(
        self,
        client: Any,
        *,
        model: str,
        system: Any,
        messages: list,
        tool_schemas: list[dict],
        max_tokens: int,
        on_text_delta: Optional[Callable[[str], None]] = None,
    ) -> ParsedResponse:
        """Call the LLM API and return a ParsedResponse.

        When *on_text_delta* is not None, use streaming and invoke the
        callback for each text chunk.
        """


# ---------------------------------------------------------------------------
# AnthropicAdapter — native pass-through
# ---------------------------------------------------------------------------


class AnthropicAdapter(LLMAdapter):
    """Pass-through adapter for models that accept Anthropic's native format."""

    def format_system(self, system_prompt: list[dict]) -> list[dict]:
        return system_prompt

    def format_messages(self, messages: tuple[Message, ...]) -> list:
        return list(messages)

    def build_tool_schemas(self, tools: list) -> list[dict]:
        return [t.to_anthropic_schema() for t in tools]

    def call_model(
        self,
        client: Any,
        *,
        model: str,
        system: Any,
        messages: list,
        tool_schemas: list[dict],
        max_tokens: int,
        on_text_delta: Optional[Callable[[str], None]] = None,
    ) -> ParsedResponse:
        if on_text_delta is not None:
            with client.messages.stream(
                model=model,
                messages=messages,
                system=system,
                tools=tool_schemas,
                max_tokens=max_tokens,
            ) as stream:
                for text in stream.text_stream:
                    on_text_delta(text)
                raw = stream.get_final_message()
            return _parse_raw_response(raw, streamed=True)
        else:
            raw = client.messages.create(
                model=model,
                messages=messages,
                system=system,
                tools=tool_schemas,
                max_tokens=max_tokens,
            )
            return _parse_raw_response(raw, streamed=False)


# ---------------------------------------------------------------------------
# FlattenedMessageAdapter — for LiteLLM / OpenAI-compat proxies
# ---------------------------------------------------------------------------


class FlattenedMessageAdapter(AnthropicAdapter):
    """Flattens Anthropic block-array format to plain strings.

    Used when the model runs behind a proxy (LiteLLM, one-api, etc.) that
    expects OpenAI-style ``"content": "plain text"`` instead of Anthropic's
    ``"content": [{"type": "text", "text": "..."}]``.

    Only overrides formatting; ``call_model`` is inherited from
    AnthropicAdapter (both use the Anthropic SDK client).
    """

    def format_system(self, system_prompt: list[dict]) -> str:
        """Flatten list-of-text-blocks to a single string."""
        if isinstance(system_prompt, list):
            return "\n\n".join(
                block["text"]
                for block in system_prompt
                if isinstance(block, dict) and "text" in block
            )
        return system_prompt  # already a string — pass through

    def format_messages(self, messages: tuple[Message, ...]) -> list:
        """Flatten text-block arrays to plain strings.

        Messages containing non-text blocks (tool_use / tool_result) are
        kept in structured format — the proxy must handle those itself.
        """
        result: list[dict] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts: list[str] = []
                non_text: list[dict] = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        else:
                            non_text.append(block)
                if non_text:
                    result.append(msg)
                else:
                    result.append({**msg, "content": "\n".join(text_parts)})
            else:
                result.append(msg)
        return result

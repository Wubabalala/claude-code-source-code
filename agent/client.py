"""Client and tool initialization — shared by bootstrap, main, web.

Extracted from main.py to break the bootstrap → main dependency cycle.
main.py re-exports these for backward compatibility with existing tests.
"""
import os
import sys

from anthropic import Anthropic

from agent.tools import BashTool, EditFileTool, GrepTool, ReadFileTool, Tool, WriteFileTool


def init_client() -> Anthropic:
    """Initialize Anthropic client with optional custom base URL.

    Env vars:
      ANTHROPIC_API_KEY   (required)
      ANTHROPIC_BASE_URL  (optional — for reverse proxies like LiteLLM)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: please set ANTHROPIC_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    kwargs: dict = {"api_key": api_key}
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return Anthropic(**kwargs)


def get_tools() -> list[Tool]:
    """Tool registry. Sorted alphabetically — order MUST be stable for cache."""
    return sorted(
        [BashTool(), EditFileTool(), GrepTool(), ReadFileTool(), WriteFileTool()],
        key=lambda t: t.name,
    )

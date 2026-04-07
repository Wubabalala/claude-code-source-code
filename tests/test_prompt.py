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

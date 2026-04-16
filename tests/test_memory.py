"""Tests for agent.memory — Phase 6 cross-session memory."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.memory import (
    MemoryEntry,
    add_entry,
    enforce_limits,
    forget_entry,
    load_memory,
    save_memory,
)
from agent.prompt import build_memory_section, build_system_prompt


def test_memory_save_and_load_round_trip(tmp_path):
    p = tmp_path / "memory.md"
    entries = []
    add_entry(entries, "first entry text")
    add_entry(entries, "second entry\nwith multiple lines")
    save_memory(p, entries)

    loaded = load_memory(p)
    assert len(loaded) == 2
    assert loaded[0].id == "m-001"
    assert loaded[1].id == "m-002"
    assert "first entry" in loaded[0].body
    assert "multiple lines" in loaded[1].body


def test_memory_forget_removes_entry(tmp_path):
    entries = []
    add_entry(entries, "keep me")
    add_entry(entries, "delete me")
    assert len(entries) == 2

    assert forget_entry(entries, "m-002") is True
    assert len(entries) == 1
    assert entries[0].id == "m-001"


def test_memory_forget_returns_false_for_unknown():
    entries = []
    add_entry(entries, "x")
    assert forget_entry(entries, "m-999") is False


def test_memory_list_returns_id_and_first_line():
    entries = []
    add_entry(entries, "hello world this is a test")
    assert entries[0].id == "m-001"
    assert entries[0].title == "hello world this is a test"


def test_memory_max_entries_evicts_oldest():
    entries = []
    for i in range(5):
        add_entry(entries, f"entry-{i}")
    evicted = enforce_limits(entries, max_entries=3, max_total_chars=999999)
    assert evicted == 2
    assert len(entries) == 3
    assert entries[0].id == "m-003"  # oldest two evicted


def test_memory_total_size_cap_prevents_prompt_bloat():
    entries = []
    for i in range(10):
        add_entry(entries, "x" * 1000)  # each ~1000 chars
    assert len(entries) == 10
    evicted = enforce_limits(entries, max_entries=100, max_total_chars=5000)
    assert evicted > 0
    total = sum(len(e.body) + len(e.title) for e in entries)
    assert total <= 5000


def test_memory_file_not_exist_returns_empty(tmp_path):
    assert load_memory(tmp_path / "nope.md") == []


def test_memory_id_auto_increments():
    entries = []
    add_entry(entries, "a")
    add_entry(entries, "b")
    add_entry(entries, "c")
    assert [e.id for e in entries] == ["m-001", "m-002", "m-003"]


def test_memory_id_continues_after_gap():
    """After forget, next id should still increment from the max."""
    entries = []
    add_entry(entries, "a")
    add_entry(entries, "b")
    forget_entry(entries, "m-001")
    add_entry(entries, "c")
    assert entries[-1].id == "m-003"  # not m-001 reuse


def test_memory_injected_into_system_prompt():
    entries = []
    add_entry(entries, "the project uses Python 3.13")
    prompt = build_system_prompt(
        cwd="/tmp", os_name="Linux", today="2026-04-16",
        memory_entries=entries,
    )
    # Memory goes in the dynamic (second) block
    dynamic_text = prompt[1]["text"]
    assert "Project Memory" in dynamic_text
    assert "m-001" in dynamic_text
    assert "Python 3.13" in dynamic_text


def test_memory_not_in_static_cache_block():
    entries = []
    add_entry(entries, "should not be in static")
    prompt = build_system_prompt(
        cwd="/tmp", os_name="Linux", today="2026-04-16",
        memory_entries=entries,
    )
    static_text = prompt[0]["text"]
    assert "should not be in static" not in static_text


def test_memory_empty_produces_no_section():
    prompt = build_system_prompt(
        cwd="/tmp", os_name="Linux", today="2026-04-16",
        memory_entries=[],
    )
    assert "Project Memory" not in prompt[1]["text"]


def test_build_memory_section_format():
    entries = [
        MemoryEntry(id="m-001", date="2026-01-01", title="title one", body="body one"),
        MemoryEntry(id="m-002", date="2026-01-02", title="title two", body="title two"),
    ]
    section = build_memory_section(entries)
    assert "**[m-001]**" in section
    assert "**[m-002]**" in section
    assert "body one" in section

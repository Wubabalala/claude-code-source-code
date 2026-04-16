"""Cross-session memory — Phase 6.

Stores per-project knowledge as a human-readable markdown file at
`.agent/memory.md` (cwd-relative). Each entry is a markdown heading
with an auto-incremented id + timestamp + body text.

Format:
    ## [m-001] 2026-04-16 title-or-first-line
    body text here...

    ## [m-002] 2026-04-16 another entry
    more text...

Usage in REPL:
    /memory-save <text>     append a new entry
    /memory-list            show all entries (id + first line)
    /memory-forget <id>     delete an entry by id (e.g. m-001)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_ENTRY_RE = re.compile(r"^## \[m-(\d+)\] (\S+) (.+)$")


@dataclass
class MemoryEntry:
    id: str           # "m-001"
    date: str         # "2026-04-16"
    title: str        # first line of body
    body: str         # full body text (may be multi-line)


def _parse_entries(text: str) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    current: Optional[dict] = None
    body_lines: list[str] = []

    for line in text.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            if current is not None:
                current["body"] = "\n".join(body_lines).strip()
                entries.append(MemoryEntry(**current))
            current = {
                "id": f"m-{m.group(1)}",
                "date": m.group(2),
                "title": m.group(3),
            }
            body_lines = []
        elif current is not None:
            body_lines.append(line)

    if current is not None:
        current["body"] = "\n".join(body_lines).strip()
        entries.append(MemoryEntry(**current))

    return entries


def _serialize_entries(entries: list[MemoryEntry]) -> str:
    parts: list[str] = []
    for e in entries:
        header = f"## [{e.id}] {e.date} {e.title}"
        parts.append(f"{header}\n{e.body}\n" if e.body else f"{header}\n")
    return "\n".join(parts)


def _next_id(entries: list[MemoryEntry]) -> str:
    nums = []
    for e in entries:
        m = re.match(r"m-(\d+)", e.id)
        if m:
            nums.append(int(m.group(1)))
    n = max(nums) + 1 if nums else 1
    return f"m-{n:03d}"


def load_memory(path: Path) -> list[MemoryEntry]:
    path = Path(path)
    if not path.exists():
        return []
    return _parse_entries(path.read_text(encoding="utf-8"))


def save_memory(path: Path, entries: list[MemoryEntry]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize_entries(entries), encoding="utf-8")


def add_entry(entries: list[MemoryEntry], text: str) -> MemoryEntry:
    """Create and append a new entry. Returns the new entry."""
    mid = _next_id(entries)
    title = text.split("\n")[0][:80]
    body = text
    entry = MemoryEntry(
        id=mid,
        date=time.strftime("%Y-%m-%d"),
        title=title,
        body=body,
    )
    entries.append(entry)
    return entry


def forget_entry(entries: list[MemoryEntry], entry_id: str) -> bool:
    """Remove an entry by id. Returns True if found and removed."""
    for i, e in enumerate(entries):
        if e.id == entry_id:
            entries.pop(i)
            return True
    return False


def enforce_limits(entries: list[MemoryEntry], *,
                   max_entries: int = 100,
                   max_total_chars: int = 50_000) -> int:
    """Evict oldest entries until both limits are satisfied.
    Returns number of entries evicted."""
    evicted = 0
    while len(entries) > max_entries and entries:
        entries.pop(0)
        evicted += 1
    while entries and sum(len(e.body) + len(e.title) for e in entries) > max_total_chars:
        entries.pop(0)
        evicted += 1
    return evicted

"""Cache-aware system prompt builder.

Design notes (see docs/plans/2026-04-07-phase1-skeleton-design.md, Section 4):
- Static section is module-level constant strings — byte-for-byte identical
  across all users, all sessions, all time. This is what makes prompt cache
  work.
- Dynamic section is built per-call. It comes AFTER the cache breakpoint, so
  changes there do not invalidate the static cache.
- NEVER add f-strings or if-else to the static constants. N conditions create
  2^N cache fragments.
"""

DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


# ============================================================================
# Static section — byte-identical forever
# ============================================================================

STATIC_INTRO = """\
You are a code repository assistant. You help users navigate, understand, \
and modify codebases by reading files, searching for patterns, and \
executing commands.\
"""

STATIC_TOOL_USAGE = """\
# Tools

You have access to three tools:

- **read_file**: Read a file's content. Use for understanding what a specific \
file does. Supports offset/limit for large files.

- **grep**: Search for patterns across files using regex. Use this BEFORE \
read_file when you don't know which file to look at. Prefer grep over reading \
many files individually.

- **bash**: Execute shell commands. Use sparingly. Prefer read_file and grep \
when possible. Never use destructive commands without asking the user first.

# Tool usage rules

1. When you need information, prefer searching (grep) over guessing.
2. When the user asks "where is X", use grep first, then read_file.
3. When the user asks to modify code, read first, then propose a diff, then \
ask for confirmation before writing.
4. Do not run commands that modify the filesystem without user approval.\
"""

STATIC_BEHAVIOR = """\
# Behavior

- Be concise. Lead with the answer, not the reasoning.
- When you don't know something, say so. Do not invent file paths, function \
names, or APIs.
- When referencing code, use the format `file_path:line_number` so the user \
can navigate to it.
- If a tool fails, try a different approach. Do not retry the same failing \
call repeatedly.\
"""


# ============================================================================
# Dynamic section — built per call
# ============================================================================


def build_dynamic_environment(cwd: str, os_name: str) -> str:
    return f"""\
# Environment

- Working directory: {cwd}
- Operating system: {os_name}\
"""


def build_dynamic_date(today: str) -> str:
    return f"Today's date is {today}."


# ============================================================================
# Main entry — assembles the full system prompt
# ============================================================================


def build_system_prompt(cwd: str, os_name: str, today: str) -> list[dict]:
    """Returns the Anthropic Messages API `system` field as content blocks.

    The first block carries the cache_control breakpoint. The second is
    dynamic and never cached.
    """
    static_text = "\n\n".join([STATIC_INTRO, STATIC_TOOL_USAGE, STATIC_BEHAVIOR])
    dynamic_text = "\n\n".join([
        build_dynamic_date(today),
        build_dynamic_environment(cwd, os_name),
    ])
    return [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": dynamic_text,
        },
    ]

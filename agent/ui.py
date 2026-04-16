"""Rich terminal UI — enhances the REPL experience.

Centralizes all terminal output through a Rich Console so the agent
gets colors, panels, tables, spinners, and markdown rendering without
scattering Rich imports across every module.

If Rich is unavailable (should not happen — it's installed), falls back
to plain print().
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.theme import Theme

    _THEME = Theme({
        "allow": "green",
        "deny": "bold red",
        "ask": "yellow",
        "info": "cyan",
        "warn": "bold yellow",
        "error": "bold red",
        "dim": "dim",
        "header": "bold blue",
    })
    _console = Console(theme=_THEME)
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
    _console = None  # type: ignore


def get_console() -> Any:
    return _console


def banner(version: str, base_url: str, primary: str, fallback: str,
           session_id: str, memory_count: int = 0, mcp_tools: int = 0) -> None:
    if not _HAS_RICH:
        print(f"Code Repo Assistant ({version})")
        return
    lines = [
        f"[header]Code Repo Assistant[/] [dim]({version})[/]",
        f"  base_url: [info]{base_url}[/]",
        f"  primary:  {primary}",
        f"  fallback: {fallback}",
        f"  session:  [dim]{session_id[:8]}[/]",
    ]
    if memory_count:
        lines.append(f"  memory:   {memory_count} entries")
    if mcp_tools:
        lines.append(f"  mcp:      {mcp_tools} tools")
    lines.append("")
    lines.append("[dim]Commands: /exit /reset /agent /memory-save /memory-list "
                 "/memory-forget /resume /sessions /team-create /send-message "
                 "/inbox /team-status[/]")
    _console.print(Panel("\n".join(lines), title="Agent", border_style="blue"))


def token_stats(input_tokens: int, cache_read: int, cache_create: int,
                output_tokens: int, hit_rate: float) -> None:
    if not _HAS_RICH:
        print(f"  [tokens] in={input_tokens} out={output_tokens}")
        return
    _console.print(
        f"  [dim]tokens[/] in=[info]{input_tokens}[/] "
        f"cache_read=[info]{cache_read}[/] "
        f"cache_create=[info]{cache_create}[/] "
        f"out=[info]{output_tokens}[/] "
        f"hit=[{'allow' if hit_rate > 50 else 'dim'}]{hit_rate:.0f}%[/]"
    )


def assistant_response(text: str) -> None:
    if not _HAS_RICH:
        print(f"\n{text}\n")
        return
    _console.print()
    _console.print(Markdown(text))
    _console.print()


def permission_prompt(tool_name: str, target: str, op_type: str, risk: str) -> None:
    if not _HAS_RICH:
        print(f"\n[PERMISSION] tool={tool_name} target={target} op={op_type}\n  risk: {risk}")
        return
    _console.print()
    _console.print(Panel(
        f"[ask]tool[/]={tool_name}  [ask]target[/]={target}  [ask]op[/]={op_type}\n"
        f"risk: {risk}",
        title="[ask]PERMISSION[/]",
        border_style="yellow",
    ))


def status_message(text: str, style: str = "info") -> None:
    if not _HAS_RICH:
        print(text)
        return
    _console.print(f"[{style}]{text}[/]")


def error_message(text: str) -> None:
    if not _HAS_RICH:
        print(text)
        return
    _console.print(f"[error]{text}[/]")


def subagent_header(mode: str, team: str = "", name: str = "") -> None:
    if not _HAS_RICH:
        print(f"[sub-agent] mode={mode}, running...")
        return
    extra = f", team={team}, name={name}" if team else ""
    _console.print(f"[info][sub-agent][/] mode=[header]{mode}[/]{extra}, running...")


def subagent_result(status: str, text: str) -> None:
    if not _HAS_RICH:
        print(f"\n[sub-agent] status={status}\n{text}\n")
        return
    style = "allow" if status == "completed" else "error"
    _console.print(Panel(
        Markdown(text),
        title=f"[{style}]sub-agent: {status}[/]",
        border_style="green" if status == "completed" else "red",
    ))


def team_status_table(team_name: str, members: list[dict], mailbox_counts: dict) -> None:
    if not _HAS_RICH:
        print(f"[team: {team_name}]")
        for m in members:
            print(f"  {m['name']:20s} {m['status']:12s}")
        return
    table = Table(title=f"Team: {team_name}", border_style="blue")
    table.add_column("Member", style="bold")
    table.add_column("Status")
    table.add_column("Unread", justify="right")
    for m in members:
        status = m["status"]
        style = {"completed": "green", "running": "yellow",
                 "failed": "red"}.get(status, "dim")
        unread = mailbox_counts.get(m["name"], 0)
        table.add_row(m["name"], f"[{style}]{status}[/]", str(unread))
    _console.print(table)


def sessions_table(summaries: list) -> None:
    if not _HAS_RICH:
        for s in summaries:
            print(f"  {s.session_id[:8]}  msgs={s.message_count}")
        return
    table = Table(title="Sessions", border_style="blue")
    table.add_column("ID", style="dim")
    table.add_column("Messages", justify="right")
    table.add_column("Last Updated")
    import datetime
    for s in summaries:
        mtime = datetime.datetime.fromtimestamp(s.last_updated).isoformat(timespec="seconds")
        table.add_row(s.session_id[:8], str(s.message_count), mtime)
    _console.print(table)


def memory_list_table(entries: list) -> None:
    if not _HAS_RICH:
        for e in entries:
            print(f"  {e.id}  {e.date}  {e.title[:60]}")
        return
    table = Table(title="Memory", border_style="blue")
    table.add_column("ID", style="dim")
    table.add_column("Date")
    table.add_column("Title")
    for e in entries:
        table.add_row(e.id, e.date, e.title[:60])
    _console.print(table)


def inbox_message(from_name: str, ts: str, body: str) -> None:
    if not _HAS_RICH:
        print(f"  from={from_name} ({ts}): {body[:200]}")
        return
    _console.print(f"  [info]from[/]={from_name} [dim]({ts})[/]: {body[:200]}")

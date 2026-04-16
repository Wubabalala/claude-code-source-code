"""Manual smoke test for Phase 2 + 3 paths against a real API.

Covers what 139-passing pytest already owns but exercises the REAL client
and the REAL state machine, so we have evidence the paths fire end-to-end
and not just against FakeAnthropicClient mocks.

Run: `python -m agent.smoke`

Requires `.env` with ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL).
This script patches agent.compact.CTX_WINDOW_TOKENS to a small value so
compaction thresholds are reachable with short conversations — real API
calls are still made for the autocompact summary.

What it validates:

  Phase 3 (sync, no API):
    - BashTool hard-deny on sensitive path (~/.ssh)
    - BashTool nested interpreter → DENY (bash -c)
    - BashTool metachar + hard-deny → DENY (echo | tee ~/.bashrc)
    - ReadFileTool hard-deny (~/.ssh/id_rsa)
    - dd of=.env hard-deny

  Phase 2 (real API):
    - Proactive autocompact triggers on a pre-built multi-turn history
    - State trace shows transition_reason="autocompact"
    - Agent loop completes cleanly after compaction

What it does NOT validate end-to-end:
    - Microcompact (needs compactable tool_results across multiple turns;
      expensive with 30s+ proxy latency)
    - Circuit breaker (needs 3 consecutive Auto failures)
    - Reactive compact (needs actual 413 from Anthropic — our lowered
      threshold can't trigger it because Anthropic's real window is 200K)
    - ASK path with user TTY y/yes (interactive)
"""
from __future__ import annotations

import datetime
import os
import platform
import sys

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Phase 3 sync checks (no API required, fast)
# ---------------------------------------------------------------------------


def run_phase3_sync() -> list[tuple[str, bool, str]]:
    from agent.tools import (
        BashTool,
        BashInput,
        PermissionDecision,
        ReadFileTool,
        ReadFileInput,
    )

    results: list[tuple[str, bool, str]] = []
    bash = BashTool()
    rf = ReadFileTool()

    cases = [
        ("bash rm -rf ~/.ssh → DENY",
         bash.check_permissions, BashInput(command="rm -rf ~/.ssh"),
         PermissionDecision.DENY),
        ("bash -c 'ls' (nested) → DENY",
         bash.check_permissions, BashInput(command="bash -c 'ls'"),
         PermissionDecision.DENY),
        ("echo x | tee ~/.bashrc (metachar+hard-deny) → DENY",
         bash.check_permissions, BashInput(command="echo x | tee ~/.bashrc"),
         PermissionDecision.DENY),
        ("cat ~/.ssh/id_rsa → DENY",
         bash.check_permissions, BashInput(command="cat ~/.ssh/id_rsa"),
         PermissionDecision.DENY),
        ("dd of=.env if=/tmp/src → DENY",
         bash.check_permissions, BashInput(command="dd of=.env if=/tmp/src"),
         PermissionDecision.DENY),
        ("read_file ~/.ssh/id_rsa → DENY",
         rf.check_permissions, ReadFileInput(file_path="~/.ssh/id_rsa"),
         PermissionDecision.DENY),
        ("bash ls /tmp (read-only) → ALLOW",
         bash.check_permissions, BashInput(command="ls /tmp"),
         PermissionDecision.ALLOW),
        ("bash git log (read-only) → ALLOW",
         bash.check_permissions, BashInput(command="git log --oneline"),
         PermissionDecision.ALLOW),
    ]

    for desc, fn, inp, expected in cases:
        outcome = fn(inp)
        ok = outcome.decision == expected
        results.append((desc, ok, f"{outcome.decision}: {outcome.risk or '-'}"))

    return results


# ---------------------------------------------------------------------------
# Phase 2 autocompact trigger with real API
# ---------------------------------------------------------------------------


def run_phase2_autocompact(client, primary: str, fallback: str) -> dict:
    # Phase 4 path: write a temporary TOML, point AGENT_CONFIG_PATH at it,
    # let load_config() + configure_compact() inject the smaller threshold
    # through the real config pipeline instead of monkey-patching module
    # constants. This exercises the same code path the REPL uses.
    import tempfile
    import agent.compact as compact_mod
    from agent.config import load_config

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8",
    )
    tmp.write("[compact]\nctx_window_tokens = 3000\n")
    tmp.close()
    os.environ["AGENT_CONFIG_PATH"] = tmp.name
    cfg = load_config()
    compact_mod.configure_compact(cfg.compact)

    from agent.loop import run_agent_loop
    from agent.prompt import build_system_prompt

    sys_prompt = build_system_prompt(
        cwd=os.getcwd(),
        os_name=platform.system(),
        today=datetime.date.today().isoformat(),
    )

    # Pre-built 7-message history totaling well above 85% of 3000 tokens.
    # Must end on user role for API call.
    pad_user = "padding " * 400      # ~1100 tokens each
    pad_asst = "response " * 300     # ~900 tokens each
    initial = (
        {"role": "user",      "content": [{"type": "text", "text": pad_user + " turn-1"}]},
        {"role": "assistant", "content": [{"type": "text", "text": pad_asst + " ack-1"}]},
        {"role": "user",      "content": [{"type": "text", "text": pad_user + " turn-2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": pad_asst + " ack-2"}]},
        {"role": "user",      "content": [{"type": "text", "text": pad_user + " turn-3"}]},
        {"role": "assistant", "content": [{"type": "text", "text": pad_asst + " ack-3"}]},
        {"role": "user",      "content": [{"type": "text", "text": "Reply with exactly: SMOKE-OK"}]},
    )

    from agent.compact import estimate_tokens
    pre_tokens = estimate_tokens(initial)
    print(f"  pre-loop estimate: {pre_tokens} tokens "
          f"(threshold: {int(compact_mod.CTX_WINDOW_TOKENS * compact_mod.AUTO_COMPACT_THRESHOLD)})")

    trace = []
    result = run_agent_loop(
        client=client,
        initial_messages=initial,
        tools=[],
        system_prompt=sys_prompt,
        max_turns=5,
        primary_model=primary,
        fallback_model=fallback,
        on_state_transition=trace.append,
    )

    transitions = [s.transition_reason for s in trace]
    last = trace[-1] if trace else None
    return {
        "status": result.status,
        "transitions": transitions,
        "autocompact_count": last.autocompact_count if last else 0,
        "microcompact_count": last.microcompact_count if last else 0,
        "consecutive_compact_failures": last.consecutive_compact_failures if last else 0,
        "final_messages_count": len(result.messages),
        "pre_tokens": pre_tokens,
        "post_tokens": estimate_tokens(result.messages),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    load_dotenv()

    print("=" * 60)
    print("Phase 3 sync checks (no API)")
    print("=" * 60)
    sync_results = run_phase3_sync()
    all_ok = True
    for desc, ok, detail in sync_results:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {desc}")
        if not ok:
            print(f"         actual: {detail}")
            all_ok = False

    if not all_ok:
        print("\nPhase 3 sync checks FAILED — aborting before API call")
        return 1

    print()
    print("=" * 60)
    print("Phase 2 autocompact (real API)")
    print("=" * 60)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  SKIP: ANTHROPIC_API_KEY not set")
        return 0

    from anthropic import Anthropic
    client_kwargs: dict = {"api_key": api_key}
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        client_kwargs["base_url"] = base_url
        print(f"  base_url: {base_url}")
    client = Anthropic(**client_kwargs)

    primary = os.environ.get("AGENT_PRIMARY_MODEL", "claude-opus-4-6")
    fallback = os.environ.get("AGENT_FALLBACK_MODEL", "claude-sonnet-4-6")
    print(f"  primary:  {primary}")
    print(f"  fallback: {fallback}")

    try:
        summary = run_phase2_autocompact(client, primary, fallback)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return 1

    print()
    print(f"  status:            {summary['status']}")
    print(f"  transitions:       {summary['transitions']}")
    print(f"  autocompact_count: {summary['autocompact_count']}")
    print(f"  microcompact_count:{summary['microcompact_count']}")
    print(f"  pre_tokens:        {summary['pre_tokens']}")
    print(f"  post_tokens:       {summary['post_tokens']}")
    print(f"  final_msgs:        {summary['final_messages_count']}")

    checks = [
        ("status == completed", summary["status"] == "completed"),
        ("autocompact fired at least once", summary["autocompact_count"] >= 1),
        ("autocompact transition in trace",
         "autocompact" in summary["transitions"]),
        ("tokens reduced", summary["post_tokens"] < summary["pre_tokens"]),
    ]
    print()
    print("  Assertions:")
    for desc, ok in checks:
        marker = "PASS" if ok else "FAIL"
        print(f"    [{marker}] {desc}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("ALL SMOKE CHECKS PASSED — Phase 2/3 closure confirmed.")
        return 0
    print("SOME SMOKE CHECKS FAILED — inspect output above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

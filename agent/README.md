# Phase 1 Code Repo Assistant Agent

Minimal agent built atop the lessons learned from Claude Code v2.1.88 source.

## Architecture

- `main.py` — CLI REPL, session history
- `loop.py` — Agent state machine (2 continue sites, 3 exit conditions)
- `tools.py` — Tool ABC + read_file, grep, bash
- `prompt.py` — Static/Dynamic prompt builder for cache friendliness
- `api.py` — Anthropic SDK wrapper
- `types.py` — Immutable State and AgentResult

See `docs/plans/2026-04-07-phase1-skeleton-design.md` for the full design doc.

## Setup

```bash
pip install -r agent/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

External dependency: `ripgrep` (rg) for the grep tool. Install from
https://github.com/BurntSushi/ripgrep.

## Run

```bash
python -m agent.main
```

REPL commands:
- `/reset` — clear conversation history
- `/exit` — quit

## Test

```bash
python -m pytest tests/ -v
```

All tests are unit tests with a fake Anthropic client. No real API calls in
the test suite.

## Cache stats

Every API call prints a `[tokens]` line showing cache_read / cache_create.
On the second turn within 5 minutes, expect `cache_read > 0`.

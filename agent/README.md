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

## Custom provider / reverse proxy

The agent talks the Anthropic Messages protocol. You can point it at any
server that speaks that protocol — the official API, or a reverse proxy like
cc-switch, one-api / new-api, LiteLLM Proxy, etc.

Configuration is via environment variables (or a `.env` file in the working
directory):

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ANTHROPIC_API_KEY` | yes | — | Your API key |
| `ANTHROPIC_BASE_URL` | no | `https://api.anthropic.com` | Custom endpoint (must speak Anthropic `/v1/messages` protocol) |
| `AGENT_PRIMARY_MODEL` | no | `claude-opus-4-6` | Primary model name |
| `AGENT_FALLBACK_MODEL` | no | `claude-sonnet-4-6` | Fallback when primary errors |

See `.env.example` for sample configurations. To get started:

```bash
cp agent/.env.example agent/.env
# edit agent/.env with your key and optional base_url
python -m agent.main
```

**Important**: the endpoint MUST speak Anthropic's `/v1/messages` protocol
(content blocks, tool_use blocks, etc.). Pure OpenAI-format endpoints like
`/v1/chat/completions` are **not** compatible. Use a translation proxy
(LiteLLM Proxy, cc-switch) if your backend is OpenAI-format.

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

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Docker (recommended — starts Playwright MCP + FastAPI together)
docker compose up --build

# Local dev (starts Playwright MCP via npx + uvicorn in background)
./start.sh

# Backend only
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# Frontend only (when backend already running)
python -m http.server 3000
```

Environment: copy `.env.example` or create `.env` with `OPENAI_API_KEY=sk-...`. Other env vars (`PLAYWRIGHT_MCP_URL`, `DATABASE_PATH`, `SCREENSHOTS_DIR`) have defaults in Docker Compose and `start.sh`.

No lint or test runner is configured.

## Architecture

### System Components

```
index.html (browser UI)
    → POST /runs   ← FastAPI (server.py)
                        → asyncio.to_thread(run_agent)
                              → agent.py (GPT-4o function-calling loop)
                                   → PlaywrightMCPClient (mcp_client.py)  → Playwright MCP Server (Docker :8931)
                                   → db.py (SQLite)
                        ← SSE stream → index.html (live step updates)
```

### Key Files

| File | Role |
|------|------|
| `server.py` | FastAPI routes, SSE streaming, bridges async FastAPI ↔ sync agent thread |
| `agent.py` | GPT-4o agentic loop, defines 11 test tools, executes them against MCP |
| `mcp_client.py` | JSON-RPC over legacy SSE transport to Playwright MCP |
| `db.py` | SQLite persistence for runs and steps; thread-safe via `threading.Lock` |
| `index.html` | Monolithic frontend: form input, live SSE log, history tab |

### Agent Loop (`agent.py`)

1. User sends `{ scenario, base_url }` → agent receives as natural language instructions
2. `QA_TOOLS` list (11 tools) is passed to GPT-4o (`gpt-4o`) as function definitions
3. Model generates tool calls; each is executed via `PlaywrightMCPClient.call_tool()`
4. Results fed back into the conversation; loop continues until `test_done` or 40 steps
5. Every step persisted to SQLite and streamed via `on_step` callback → FastAPI SSE queue

**Tools:** `navigate`, `snapshot` (a11y tree), `click`, `fill`, `select_option`, `wait_for_load`, `assert_element`, `assert_url`, `assert_text_present`, `screenshot`, `test_done`

### MCP Client (`mcp_client.py`)

Uses Playwright MCP's **legacy SSE transport** (not standard JSON-RPC 2.0):
- `GET /sse` → persistent SSE connection (background thread)
- `POST /sse?sessionId=<id>` → send JSON-RPC requests
- Responses matched by request ID from the SSE stream

### Thread/Async Bridge

Agent runs in a background thread (`asyncio.to_thread`). FastAPI async routes use `asyncio.Queue` to receive step events from the sync agent thread and push them as SSE events.

### Database Schema

```sql
runs  (id, scenario, base_url, status, passed, summary, failures, created_at, finished_at)
steps (id, run_id, seq, tool, input_json, result, is_pass, is_fail, screenshot_path, created_at)
```

## Extending the Agent

**Add a new tool:** add an entry to `QA_TOOLS` in `agent.py` and a handler branch in `execute_tool()`.

**Change the model:** update `MODEL` constant in `agent.py`.

**Change the browser:** update `--browser` flag in `docker-compose.yml` (Firefox → chromium, etc.).

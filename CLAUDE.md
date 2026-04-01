# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# First-time setup
pip install -r requirements.txt
python3 -m playwright install firefox

# Start server
./start.sh

# Backend only
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# Frontend only (when backend already running)
python -m http.server 3000
```

Environment: copy `.env.example` or create `.env` with `OPENAI_API_KEY=sk-...`. Other env vars (`DATABASE_PATH`, `SCREENSHOTS_DIR`) have defaults in `start.sh`.

No lint or test runner is configured.

## Architecture

### System Components

```
index.html (browser UI)
    → POST /runs   ← FastAPI (server.py)
                        → asyncio.to_thread(run_agent)
                              → agent.py (GPT-4o function-calling loop)
                                   → Playwright (Python) → Firefox browser (in-process)
                                   → db.py (SQLite)
                        ← SSE stream → index.html (live step updates)
```

### Key Files

| File | Role |
|------|------|
| `server.py` | FastAPI routes, SSE streaming, bridges async FastAPI ↔ sync agent thread |
| `agent.py` | GPT-4o agentic loop, defines 11 test tools, executes them via Playwright |
| `db.py` | SQLite persistence for runs and steps; thread-safe via `threading.Lock` |
| `index.html` | Monolithic frontend: form input, live SSE log, history tab |

### Agent Loop (`agent.py`)

1. User sends `{ scenario, base_url }` → agent receives as natural language instructions
2. `QA_TOOLS` list (11 tools) is passed to GPT-4o (`gpt-4o`) as function definitions
3. Model generates tool calls; each is executed via `execute_tool()` using Playwright directly
4. Results fed back into the conversation; loop continues until `test_done` or 40 steps
5. Every step persisted to SQLite and streamed via `on_step` callback → FastAPI SSE queue

**Tools:** `navigate`, `snapshot` (a11y tree), `click`, `fill`, `select_option`, `wait_for_load`, `assert_element`, `assert_url`, `assert_text_present`, `screenshot`, `test_done`

### Playwright Integration (`agent.py`)

- `_build_snapshot(page)` — walks the a11y tree via `page.accessibility.snapshot()`, assigns sequential numeric ref IDs, returns formatted text + `refs` dict
- `_get_locator(page, ref, element, refs)` — resolves ref or "role name" description to a Playwright locator via `get_by_role` / `get_by_label` / `get_by_text`
- Each test run launches its own Firefox browser via `sync_playwright()` inside `asyncio.to_thread`

### Thread/Async Bridge

Agent runs in a background thread (`asyncio.to_thread`). FastAPI async routes use `asyncio.Queue` to receive step events from the sync agent thread and push them as SSE events.

### Database Schema

```sql
runs  (id, scenario, base_url, status, passed, summary, failures, created_at, finished_at)
steps (id, run_id, seq, tool, input_json, result, is_pass, is_fail, screenshot_path, created_at)
```

## Extending the Agent

**Add a new tool:** add an entry to `QA_TOOLS` in `agent.py` and a handler branch in `execute_tool()`.

**Change the model:** update the `model=` argument in the `client.chat.completions.create()` call in `agent.py`.

**Change the browser:** update `pw.firefox.launch()` in `_run_sync()` in `agent.py` (e.g. `pw.chromium.launch()`).

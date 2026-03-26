# Render.com Deployment Design

**Date:** 2026-03-26
**Goal:** Deploy QA Agent (FastAPI + Playwright MCP) to Render.com so it's accessible to colleagues without local Docker.

## Architecture

Two Render Web Services (free tier), both Docker-based:

```
Render.com
├── playwright-mcp   (Web Service, Docker)
│     Image: Dockerfile.playwright-mcp
│     → FROM mcr.microsoft.com/playwright/mcp
│     → COPY playwright-mcp-config.json /config.json
│     → CMD: node cli.js --config /config.json --browser firefox --headless --port 8931 ...
│     Port: 8931 → exposed as https://playwright-mcp-xxxx.onrender.com
│
└── qa-backend   (Web Service, Docker)
      Image: existing Dockerfile (unchanged)
      Env vars:
        OPENAI_API_KEY = (set manually)
        PLAYWRIGHT_MCP_URL = https://playwright-mcp-xxxx.onrender.com/mcp
        DATABASE_PATH = /tmp/qa.db
        SCREENSHOTS_DIR = /tmp/screenshots
      Port: 8000 → exposed as https://qa-backend-xxxx.onrender.com
```

## Constraints & Trade-offs

- **Free tier cold start:** Both services sleep after 15 min inactivity. Cold start ~30s each.
- **DB persistence:** SQLite lives in `/tmp` — ephemeral, cleared on restart. Acceptable for demo.
- **Screenshots:** Stored in `/tmp/screenshots` — ephemeral, only valid within one function lifecycle.
- **Playwright MCP config:** Cannot mount files at runtime on Render → config baked into custom Dockerfile.

## Cold Start Handling

The MCP SSE handshake timeout is raised from 10s → 60s in `mcp_client.py` so the first test run waits for Playwright MCP to wake up instead of immediately erroring.

The general httpx client timeout is raised from 30s → 90s to accommodate slow tool calls on a freshly started browser.

## Files Changed

| File | Change |
|------|--------|
| `mcp_client.py` | SSE ready timeout 10s → 60s; httpx timeout 30s → 90s |
| `Dockerfile.playwright-mcp` | New — builds Playwright MCP image with baked config |
| `render.yaml` | New — IaC for both Render services |

## Deployment Steps

1. Deploy `playwright-mcp` service first → note its public URL
2. Deploy `qa-backend` service → set `PLAYWRIGHT_MCP_URL=<playwright-url>/mcp`
3. Set `OPENAI_API_KEY` in `qa-backend` env vars
4. Open `https://qa-backend-xxxx.onrender.com` — wait ~30s for cold start

#!/bin/sh
set -e

# Start Playwright MCP on localhost (bypasses Host header check)
playwright-mcp \
  --config /config.json \
  --browser firefox \
  --headless \
  --port 8931 \
  --host 127.0.0.1 \
  --image-responses omit &

# Give MCP a moment to initialize
sleep 2

# Start FastAPI (Render sets $PORT)
export PLAYWRIGHT_MCP_URL=http://localhost:8931/mcp
export DATABASE_PATH=${DATABASE_PATH:-/tmp/qa.db}
export SCREENSHOTS_DIR=${SCREENSHOTS_DIR:-/tmp/screenshots}

exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}

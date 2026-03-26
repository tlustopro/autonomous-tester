#!/bin/bash
# Start QA Agent locally (no Docker)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kill any leftover processes
lsof -ti:8931 | xargs kill -9 2>/dev/null || true
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
sleep 1

# Start Playwright MCP (--isolated = each session gets own browser context)
npx @playwright/mcp --port 8931 --host 0.0.0.0 --browser chromium --headless --isolated &
MCP_PID=$!
echo "Playwright MCP started (PID $MCP_PID)"

# Wait for MCP to be ready
sleep 2

# Start FastAPI server
python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
echo "FastAPI server started (PID $SERVER_PID)"

echo ""
echo "QA Agent running at http://localhost:8000"
echo "Press Ctrl+C to stop."

trap "kill $MCP_PID $SERVER_PID 2>/dev/null" EXIT
wait

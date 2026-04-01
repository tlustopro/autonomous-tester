#!/bin/bash
# Start QA Agent locally (no Docker)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kill any leftover processes
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
sleep 1

# Start FastAPI server
python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
echo "FastAPI server started (PID $SERVER_PID)"

echo ""
echo "QA Agent running at http://localhost:8000"
echo "Press Ctrl+C to stop."

trap "kill $SERVER_PID 2>/dev/null" EXIT
wait

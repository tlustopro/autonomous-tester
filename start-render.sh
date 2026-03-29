#!/bin/sh
set -e

# Start Playwright MCP on localhost
playwright-mcp \
  --config /config.json \
  --browser firefox \
  --headless \
  --port 8931 \
  --host 127.0.0.1 \
  --image-responses omit &

# Wait until MCP TCP port is accepting connections (max 60s)
i=0
until python3 -c "import socket; s=socket.create_connection(('localhost',8931),1); s.close()" 2>/dev/null; do
  i=$((i+1))
  if [ "$i" -ge 60 ]; then
    echo "ERROR: Playwright MCP did not start within 60s" >&2
    exit 1
  fi
  sleep 1
done
echo "Playwright MCP ready after ${i}s"

export PLAYWRIGHT_MCP_URL=http://localhost:8931/mcp
export DATABASE_PATH=${DATABASE_PATH:-/tmp/qa.db}
export SCREENSHOTS_DIR=${SCREENSHOTS_DIR:-/tmp/screenshots}

exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}

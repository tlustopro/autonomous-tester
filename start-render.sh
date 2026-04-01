#!/bin/sh
set -e

export DATABASE_PATH=${DATABASE_PATH:-/tmp/qa.db}
export SCREENSHOTS_DIR=${SCREENSHOTS_DIR:-/tmp/screenshots}

exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}

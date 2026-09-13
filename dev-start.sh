#!/bin/sh
set -eu

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

PORT="${PORT:-8000}"

echo "Running database migrations..."
.venv/bin/alembic upgrade head

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload --log-level info &
api_pid=$!

cleanup() {
  kill "$api_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait "$api_pid"

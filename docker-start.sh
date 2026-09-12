#!/bin/sh
set -eu

echo "Running database migrations..."
alembic upgrade head

PORT="${PORT:-9005}"

uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --log-level info --access-log &
api_pid=$!

python bot.py &
bot_pid=$!

cleanup() {
  kill "$api_pid" "$bot_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait "$api_pid" "$bot_pid"

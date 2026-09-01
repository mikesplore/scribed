#!/bin/sh
set -eu

echo "Running database migrations..."
alembic upgrade head

uvicorn app.main:app --host 0.0.0.0 --port 9005 --log-level info --access-log &
api_pid=$!

python bot.py &
bot_pid=$!

cleanup() {
  kill "$api_pid" "$bot_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait "$api_pid" "$bot_pid"

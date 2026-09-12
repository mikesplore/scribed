#!/bin/sh
set -eu

PORT="${PORT:-8000}"

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload --log-level info &
api_pid=$!

.venv/bin/python bot.py &
bot_pid=$!

cleanup() {
  kill "$api_pid" "$bot_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

wait "$api_pid" "$bot_pid"

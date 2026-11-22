#!/usr/bin/env bash
# Start the web app in the background. Log: logs/server.log  PID: logs/server.pid
# Usage: scripts/start.sh [port]   (default 8000)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${1:-8000}"
mkdir -p logs
if [[ -f logs/server.pid ]] && kill -0 "$(cat logs/server.pid)" 2>/dev/null; then
  echo "already running (pid $(cat logs/server.pid)); stop with scripts/stop.sh"; exit 1
fi
nohup .venv/bin/uvicorn app.main:app --reload --reload-dir app --host 127.0.0.1 --port "$PORT" >> logs/server.log 2>&1 &
echo $! > logs/server.pid
for _ in {1..30}; do
  kill -0 "$(cat logs/server.pid)" 2>/dev/null || break
  curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 1
done
if kill -0 "$(cat logs/server.pid)" 2>/dev/null; then
  echo "started pid $(cat logs/server.pid) -> http://127.0.0.1:$PORT"
  echo "logs: tail -f logs/server.log"
else
  echo "failed to start, see logs/server.log"; rm -f logs/server.pid; tail -20 logs/server.log; exit 1
fi

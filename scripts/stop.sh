#!/usr/bin/env bash
# Stop the web app: kill the pid-file process, then force-kill anything still listening on the port.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/logs/server.pid"
PORT="${1:-${PORT:-8000}}"

kill_pid() {
  local pid="$1"
  kill "$pid" 2>/dev/null || true
  for _ in {1..10}; do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.5; done
  kill -9 "$pid" 2>/dev/null || true
}

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then kill_pid "$PID"; echo "stopped pid $PID"
  else echo "stale pid file ($PID not running)"; fi
  rm -f "$PIDFILE"
fi

# Force-kill whatever still listens on the port (e.g. started outside start.sh, uvicorn --reload children).
PIDS="$(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$PIDS" ]]; then
  for p in $PIDS; do kill_pid "$p"; echo "killed pid $p on port $PORT"; done
elif [[ ! -f "$PIDFILE" ]]; then
  echo "nothing listening on port $PORT"
fi

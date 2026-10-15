#!/usr/bin/env bash
# Stop the mcpo proxy started by mcpo-start.sh, including the bridge
# subprocesses it spawned.
cd "$(dirname "$0")"
PIDFILE="mcpo.pid"
PORT="${MCPO_PORT:-8000}"

PID=""
[ -f "$PIDFILE" ] && PID="$(cat "$PIDFILE")"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  # no usable pidfile: only fall back to a process whose command line is our mcpo
  PID="$(pgrep -f "mcpo --port $PORT" | head -n1 || true)"
fi
if [ -z "$PID" ]; then
  echo "mcpo is not running."; rm -f "$PIDFILE"; exit 0
fi

pkill -P "$PID" 2>/dev/null || true   # bridge subprocesses first, so none are orphaned
kill "$PID" 2>/dev/null || true
for _ in $(seq 1 10); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID" 2>/dev/null || true; fi
rm -f "$PIDFILE"
echo "mcpo stopped (pid $PID)."

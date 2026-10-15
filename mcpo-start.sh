#!/usr/bin/env bash
# Start the mcpo proxy in the background so the Ollama chat can reach the bridges.
# Key comes from ~/.mcpo/.env (same file ollama_agent.py reads). Log: mcpo.log
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MCPO_PORT:-8000}"
PIDFILE="mcpo.pid"
LOGFILE="mcpo.log"
ENVFILE="$HOME/.mcpo/.env"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "mcpo is already running (pid $(cat "$PIDFILE")) on port $PORT. Use ./mcpo-stop.sh first to restart."
  exit 0
fi
rm -f "$PIDFILE"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use by something else:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 1
fi

KEY="$(grep '^MCPO_API_KEY=' "$ENVFILE" 2>/dev/null | cut -d= -f2- || true)"
if [ -z "$KEY" ]; then
  echo "No MCPO_API_KEY found in $ENVFILE (add a line: MCPO_API_KEY=your-key)." >&2
  exit 1
fi

nohup .venv/bin/mcpo --port "$PORT" --api-key "$KEY" --config mcpo_config.json >"$LOGFILE" 2>&1 &
echo $! >"$PIDFILE"

printf "Starting mcpo (pid %s) on port %s" "$(cat "$PIDFILE")" "$PORT"
for _ in $(seq 1 60); do
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo; echo "mcpo exited during startup. Last log lines:" >&2
    tail -n 15 "$LOGFILE" >&2; rm -f "$PIDFILE"; exit 1
  fi
  # ready once every server in the config has mounted its own openapi.json
  if grep -q "Application startup complete" "$LOGFILE" 2>/dev/null; then
    echo; echo "Ready. Log: $PWD/$LOGFILE"
    command -v curl >/dev/null && ! curl -s -m 2 localhost:11434 >/dev/null \
      && echo "Note: Ollama doesn't look reachable on :11434, start it with 'ollama serve' before chatting."
    exit 0
  fi
  printf "."; sleep 1
done
echo; echo "Still starting after 60s, check $LOGFILE" >&2
exit 1

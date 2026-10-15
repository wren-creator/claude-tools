#!/usr/bin/env bash
# List the tools the Ollama chat can see. Extra args pass through, e.g.
#   ./mcpo-tools.sh --offline
cd "$(dirname "$0")"
PORT="${MCPO_PORT:-8000}"
if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "mcpo isn't running on port $PORT. Start it with ./mcpo-start.sh" >&2
  exit 1
fi
exec .venv/bin/python ollama_agent.py --list-tools "$@" 2>/dev/null

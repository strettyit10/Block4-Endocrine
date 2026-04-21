#!/usr/bin/env bash
# Double-click to start the Endocrine Hub Update Tracker.
# Opens the dashboard in your browser; the "Rerun" button will work.
# Close the Terminal window or press Ctrl+C to stop.

set -e
cd "$(dirname "$0")"

PORT="${HUB_TRACKER_PORT:-8765}"

# Kill any existing process holding the port so we always start clean.
EXISTING=$(lsof -ti ":$PORT" 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "Stopping previous tracker on port $PORT (pid $EXISTING)…"
  kill $EXISTING 2>/dev/null || true
  sleep 0.4
fi

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python 3 is required. Install Python 3 and try again." >&2
  read -n 1 -s -r -p "Press any key to close…"
  exit 1
fi

exec "$PY" server.py

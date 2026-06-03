#!/bin/bash
# Runs the dashboard server in test mode against data-test/ fixtures.
# Does NOT start the poller — data is static. Ctrl+C to stop.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=$(grep "^port:" "$SCRIPT_DIR/config.yaml" 2>/dev/null | awk -F': *' '{print $2}')
PORT=${PORT:-666}

# Kill any existing server on the port
if lsof -ti :$PORT >/dev/null 2>&1; then
    echo "  stopping existing server on :$PORT..."
    kill $(lsof -ti :$PORT) 2>/dev/null
    sleep 1
fi

echo "  starting test server on :$PORT  (data from data-test/)"
echo "  Dashboard at http://localhost:$PORT"
echo "  Timesheet at http://localhost:$PORT/time.html"
echo ""
exec python3 "$SCRIPT_DIR/server.py" --test

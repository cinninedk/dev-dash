#!/bin/bash
# Runs the dashboard server in test mode against data-test/ fixtures.
# Temporarily stops the launchd server agent while running. Ctrl+C restores it.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=$(grep "^port:" "$SCRIPT_DIR/config.yaml" 2>/dev/null | awk -F': *' '{print $2}')
PORT=${PORT:-666}
LABEL="com.caspernielsen.dashboard-server"
PLIST=~/Library/LaunchAgents/$LABEL.plist

restore() {
    echo ""
    echo "  restoring launchd server agent..."
    launchctl load "$PLIST" 2>/dev/null && echo "  $LABEL restarted"
}
trap restore EXIT

if launchctl list "$LABEL" &>/dev/null; then
    echo "  stopping launchd agent $LABEL..."
    launchctl unload "$PLIST" 2>/dev/null
    sleep 1
fi

echo "  starting test server on :$PORT  (data from data-test/)"
echo "  Dashboard  →  http://localhost:$PORT"
echo "  Timesheet  →  http://localhost:$PORT/time.html"
echo ""
exec python3 "$SCRIPT_DIR/server.py" --test

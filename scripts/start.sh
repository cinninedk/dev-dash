#!/bin/bash
# Loads the dashboard launchd agents (run after setup.sh).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"
AGENTS=~/Library/LaunchAgents

chmod +x "$ROOT/scripts/poll.sh"

load_agent() {
    local label="$1" plist="$2"
    if launchctl list "$label" &>/dev/null; then
        echo "  already running  $label"
    else
        launchctl load "$plist" && echo "  started  $label"
    fi
}

load_agent "${LAUNCHD_PREFIX}-server" "$AGENTS/${LAUNCHD_PREFIX}-server.plist"
load_agent "${LAUNCHD_PREFIX}-poll"   "$AGENTS/${LAUNCHD_PREFIX}-poll.plist"

echo ""
PORT=$(grep "^port:" "$ROOT/config.yaml" 2>/dev/null | awk -F': *' '{print $2}')
PORT=${PORT:-666}
echo "Dashboard at http://localhost:$PORT"
echo "Logs: /tmp/dashboard-server.log  /tmp/dashboard-poll.log"

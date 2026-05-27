#!/bin/bash
# Loads the dashboard launchd agents (run after setup.sh).

AGENTS=~/Library/LaunchAgents
chmod +x "$(dirname "$0")/poll.sh"

load_agent() {
    local label="$1" plist="$2"
    if launchctl list "$label" &>/dev/null; then
        echo "  already running  $label"
    else
        launchctl load "$plist" && echo "  started  $label"
    fi
}

load_agent com.caspernielsen.dashboard-server "$AGENTS/com.caspernielsen.dashboard-server.plist"
load_agent com.caspernielsen.dashboard-poll   "$AGENTS/com.caspernielsen.dashboard-poll.plist"

echo ""
echo "Dashboard at http://localhost:666"
echo "Logs: /tmp/dashboard-server.log  /tmp/dashboard-poll.log"

#!/bin/bash
# Loads the dashboard launchd agents (run after setup.sh).

AGENTS=~/Library/LaunchAgents
chmod +x "$(dirname "$0")/poll.sh"

launchctl load "$AGENTS/com.caspernielsen.dashboard-server.plist"
launchctl load "$AGENTS/com.caspernielsen.dashboard-poll.plist"

echo "Dashboard running at http://localhost:666"
echo "poll.sh fires every 60s — logs at /tmp/dashboard-poll.log"

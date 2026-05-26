#!/bin/bash
# Stops the dashboard services (unloads from launchd).

AGENTS=~/Library/LaunchAgents

launchctl unload "$AGENTS/com.caspernielsen.dashboard-server.plist"
launchctl unload "$AGENTS/com.caspernielsen.dashboard-poll.plist"

echo "Dashboard services stopped."

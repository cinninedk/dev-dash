#!/bin/bash
# Stops the dashboard services (unloads from launchd).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"
AGENTS=~/Library/LaunchAgents

launchctl unload "$AGENTS/${LAUNCHD_PREFIX}-server.plist"
launchctl unload "$AGENTS/${LAUNCHD_PREFIX}-poll.plist"
launchctl unload "$AGENTS/${LAUNCHD_PREFIX}-copilot.plist"

echo "Dashboard services stopped."

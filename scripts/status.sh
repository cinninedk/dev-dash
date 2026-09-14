#!/bin/bash
# Shows whether the dashboard services are running and the last poll output.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"

echo "=== Services ==="
launchctl list | grep "$LAUNCHD_PREFIX" || echo "(none running)"

echo ""
echo "=== Last poll output ==="
tail -20 /tmp/dashboard-poll.log 2>/dev/null || echo "(no log yet)"

echo ""
echo "=== Last copilot poll output ==="
tail -20 /tmp/dashboard-copilot.log 2>/dev/null || echo "(no log yet)"

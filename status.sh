#!/bin/bash
# Shows whether the dashboard services are running and the last poll output.

echo "=== Services ==="
launchctl list | grep com.caspernielsen.dashboard || echo "(none running)"

echo ""
echo "=== Last poll output ==="
tail -20 /tmp/dashboard-poll.log 2>/dev/null || echo "(no log yet)"

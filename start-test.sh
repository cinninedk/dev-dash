#!/bin/bash
# Runs the dashboard server in test mode on port 6660 (real server stays on 666).
# Ctrl+C to stop.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_PORT=664

echo "  starting test server on :$TEST_PORT  (data from data-test/)"
echo "  Dashboard  →  http://localhost:$TEST_PORT"
echo "  Timesheet  →  http://localhost:$TEST_PORT/time.html"
echo ""
exec python3 "$SCRIPT_DIR/server.py" --test --port $TEST_PORT

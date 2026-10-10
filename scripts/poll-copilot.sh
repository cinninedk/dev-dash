#!/bin/bash
# poll-copilot.sh — runs `copilot-leaderboard --update` and writes data/copilot.json
# for the dashboard's Copilot page. Scheduled independently from poll.sh (via its
# own launchd StartInterval, ~4h) since this hits GitHub's org metrics API, not
# our own Bitbucket/Jira — no point fetching it every 60s like the rest.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"   # COPILOT_POOL
OUT="$ROOT/data/copilot.json"
MY_USER="nine-cin"
# copilot-leaderboard's snapshot archive (archive_dir in ~/.config/copilot-leaderboard.toml)
ARCHIVE="$HOME/src/copilot-leaderboard-stil/archive"

# launchd doesn't source .zshrc, so PATH needs setting explicitly here rather
# than relying on the shell profile.
export PATH="/opt/homebrew/bin:$HOME/bin:$PATH"
export GITHUB_COPILOT_UDAGE_TOKEN=$(cat "$ROOT/secrets/copilot-token")

log() { echo "[$(date '+%H:%M:%S')] $*"; }

RAW=$(copilot-leaderboard --update --only stil 2>&1)
STATUS=$?
if [ $STATUS -ne 0 ]; then
    log "copilot-leaderboard failed (exit $STATUS):"
    echo "$RAW"
    exit 1
fi

echo "$RAW" | MY_USER="$MY_USER" COPILOT_POOL="$COPILOT_POOL" OUT="$OUT" python3 "$ROOT/scripts/parse-copilot.py"
PARSE=$?
# Runs even when the table parse breaks: the daily history must keep growing.
MY_USER="$MY_USER" ARCHIVE="$ARCHIVE" OUT="$ROOT/data/copilot-history.json" \
    python3 "$ROOT/scripts/copilot-history.py" || exit 1
exit $PARSE

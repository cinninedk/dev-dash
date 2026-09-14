#!/bin/bash
# poll-copilot.sh — runs `copilot-leaderboard --update` and writes data/copilot.json
# for the dashboard's Copilot page. Scheduled independently from poll.sh (via its
# own launchd StartInterval, ~4h) since this hits GitHub's org metrics API, not
# our own Bitbucket/Jira — no point fetching it every 60s like the rest.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/copilot.json"
MY_USER="nine-cin"

# When run interactively (not via launchd), PATH/GITHUB_COPILOT_UDAGE_TOKEN come
# from the shell profile as usual. The launchd plist sets both explicitly in its
# EnvironmentVariables dict, since launchd doesn't source .zshrc (and TCC blocks
# launchd-spawned processes from reading ~/Documents/dotfiles/.zshrc anyway).
export PATH="/opt/homebrew/bin:$HOME/bin:$PATH"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

RAW=$(copilot-leaderboard --update --only stil 2>&1)
STATUS=$?
if [ $STATUS -ne 0 ]; then
    log "copilot-leaderboard failed (exit $STATUS):"
    echo "$RAW"
    exit 1
fi

echo "$RAW" | MY_USER="$MY_USER" OUT="$OUT" python3 "$ROOT/scripts/parse-copilot.py"

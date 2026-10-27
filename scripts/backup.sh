#!/bin/bash
# backup.sh — copies the files that can't be re-fetched to iCloud Drive.
# One folder per day (the first run that day), plus one tagged folder when
# called with a tag — land-cloud.sh does that before applying cloud commits.
# Folders older than 30 days are removed. Restore = copy a file back.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
DEST="$ICLOUD/jiraffe-backup"
[ -d "$ICLOUD" ] || exit 0          # no iCloud Drive on this machine

dir="$DEST/$(date +%F)${1:+_$(date +%H%M)-$1}"
[ -d "$dir" ] && exit 0
mkdir -p "$dir" || exit 1
for f in data/timetrack.json data/work-hours.json data/jira-worklog.json \
         data/copilot-history.json data/TogglTrack_*.csv secrets/config secrets/Arbejdstimer; do
    [ -e "$ROOT/$f" ] && cp -Rp "$ROOT/$f" "$dir/"
done
echo "[$(date '+%H:%M:%S')] backup: $(ls "$dir" | wc -l | tr -d ' ') files → ${dir#"$ICLOUD/"}"
find "$DEST" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

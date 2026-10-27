#!/bin/bash
# backup.sh — copies the files that can't be re-fetched to iCloud Drive:
# <date>/ once a day (the first run that day), and <date>/<HHMM>-<tag>/ when
# called with a tag — land-cloud.sh does that before applying cloud commits.
# Day folders older than 30 days are removed. Restore = copy a file back.
#
# Under launchd, macOS lets this write to iCloud Drive but not list a folder
# there, so nothing here may list one: old days are removed by name.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
DEST="$ICLOUD/jiraffe-backup"
[ -d "$ICLOUD" ] || exit 0          # no iCloud Drive on this machine

dir="$DEST/$(date +%F)${1:+/$(date +%H%M)-$1}"
[ -d "$dir" ] && exit 0
mkdir -p "$dir" || exit 1
for f in data/timetrack.json data/work-hours.json data/jira-worklog.json \
         data/copilot-history.json data/TogglTrack_*.csv secrets/config secrets/Arbejdstimer; do
    [ -e "$ROOT/$f" ] && cp -Rp "$ROOT/$f" "$dir/"
done
echo "[$(date '+%H:%M:%S')] backup: → ${dir#"$ICLOUD/"}"
# only folders this agent created can be removed; one made from a Terminal stays
for n in $(seq 31 60); do rm -rf "$DEST/$(date -v-${n}d +%F)" 2>/dev/null; done

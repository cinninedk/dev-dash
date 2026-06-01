#!/bin/bash
# pr-comments.sh — dump reviewer comments for a Bitbucket PR to stdout
# Usage: ./pr-comments.sh <pr_id>
#        ./pr-comments.sh <pr_id> | pbcopy   # send straight to clipboard

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/secrets/config"
PASSWORD=$(cat "$SCRIPT_DIR/secrets/bitbucket-token")
BB_DATA="$SCRIPT_DIR/data/bitbucket.json"

PR_ID="${1?Usage: $0 <pr_id>}"

# Look up PR metadata from the last poll
PR_JSON=$(jq -c --argjson id "$PR_ID" \
    '(.my_prs + .reviewer_prs) | map(select(.id == $id)) | .[0] // empty' \
    "$BB_DATA" 2>/dev/null)

if [ -z "$PR_JSON" ]; then
    echo "Error: PR #$PR_ID not found in data/bitbucket.json" >&2
    echo "       (Run poll.sh first, or check the PR ID)" >&2
    exit 1
fi

PROJECT=$(echo "$PR_JSON" | jq -r '.project')
SLUG=$(echo "$PR_JSON"    | jq -r '.slug')
TITLE=$(echo "$PR_JSON"   | jq -r '.title')
AUTHOR=$(echo "$PR_JSON"  | jq -r '.author')
REPO=$(echo "$PR_JSON"    | jq -r '.repo')
BRANCH=$(echo "$PR_JSON"  | jq -r '.branch // ""')

# Paginated fetch of all PR activities
ALL="[]"
START=0
while true; do
    PAGE=$(curl -sf \
        -H "Authorization: Bearer $PASSWORD" \
        "${STASH_URL}/rest/api/1.0/projects/${PROJECT}/repos/${SLUG}/pull-requests/${PR_ID}/activities?limit=100&start=${START}")
    [ -z "$PAGE" ] && break
    VALUES=$(echo "$PAGE" | jq '.values // []')
    ALL=$(jq -n --argjson a "$ALL" --argjson v "$VALUES" '$a + $v')
    [ "$(echo "$PAGE" | jq -r '.isLastPage')" = "true" ] && break
    NEXT=$(echo "$PAGE" | jq -r '.nextPageStart // empty')
    [ -z "$NEXT" ] && break
    START="$NEXT"
done

THREAD_COUNT=$(echo "$ALL" | jq '[.[] | select(.action == "COMMENTED")] | length')

printf '# PR #%s: %s\n' "$PR_ID" "$TITLE"
printf '# Repo: %s  |  Branch: %s\n' "$REPO" "$BRANCH"
printf '# Author: %s\n' "$AUTHOR"
printf '# %s comment thread(s)\n\n' "$THREAD_COUNT"

echo "$ALL" | jq -r '
  map(select(.action == "COMMENTED" and (.comment != null) and (.comment.state != "RESOLVED"))) |
  .[] |
  [
    (
      (.commentAnchor.path // "") as $path |
      (.commentAnchor.line | if type == "number" then " (line " + tostring + ")" else "" end) as $ln |
      if $path != "" then "\n---\n## File: \($path)\($ln)" else "\n---\n## General" end
    ),
    (
      (.comment.author.displayName // .comment.author.name // "Unknown") as $name |
      (if .comment.severity == "BLOCKER" then " [BLOCKER]" else "" end) as $blk |
      (if .comment.state == "RESOLVED" then " [RESOLVED]" else "" end) as $res |
      "\n**\($name)**\($blk)\($res):\n\(.comment.text)"
    ),
    (
      [ .comment.comments[]? |
        "  > **\(.author.displayName // .author.name // "Unknown")**\(if .state == "RESOLVED" then " [RESOLVED]" else "" end): \(.text)"
      ] | join("\n")
    )
  ] | join("\n")
'

#!/bin/bash
# Usage: ./scripts/pr-comments.sh <pr_id>
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"
PASSWORD=$(cat "$ROOT/secrets/bitbucket-token")

PR_ID="${1:?Usage: $0 <pr_id>}"

DATA="$ROOT/data/bitbucket.json"
PR=$(jq --argjson id "$PR_ID" '[.my_prs[], .reviewer_prs[]] | map(select(.id == $id)) | .[0]' "$DATA" 2>/dev/null)
if [ -z "$PR" ] || [ "$PR" = "null" ]; then
    echo "PR #$PR_ID not found in data/bitbucket.json" >&2
    exit 1
fi

PROJECT=$(echo "$PR" | jq -r '.project')
SLUG=$(echo "$PR" | jq -r '.slug')
TITLE=$(echo "$PR" | jq -r '.title')
AUTHOR=$(echo "$PR" | jq -r '.author')

activities=()
start=0
is_last="false"
while [ "$is_last" != "true" ]; do
    body=$(curl -sf \
        -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/api/1.0/projects/$PROJECT/repos/$SLUG/pull-requests/$PR_ID/activities?limit=100&start=$start" \
        2>/dev/null) || break
    [ -z "$body" ] && break
    activities+=("$body")
    is_last=$(echo "$body" | jq -r '.isLastPage // true')
    start=$(echo "$body" | jq -r '.nextPageStart // 0')
done

all_activities=$(printf '%s\n' "${activities[@]}" | jq -s '[.[].values[]?]')

printf "\n\033[1;33mPR #%s\033[0m  %s\n" "$PR_ID" "$TITLE"
printf "\033[2mAuthor: %s  |  Repo: %s\033[0m\n\n" "$AUTHOR" "$SLUG"

echo "$all_activities" | jq -r '
  map(select(.action == "COMMENTED" and .comment != null and (.comment.parent == null)
             and (.comment.state != "RESOLVED") and (.comment.threadResolved != true))) |
  map({
    state:  .comment.state,
    author: (.comment.author.displayName // .comment.author.name // "Unknown"),
    text:   .comment.text,
    path:   (.commentAnchor.path // ""),
    line:   (.commentAnchor.line // null)
  }) |
  .[] |
  [.state, .author, .path, (.line | tostring), .text] | @tsv
' | awk -F'\t' '
{
  state=$1; author=$2; path=$3; line=$4; text=$5

  if (state == "RESOLVED") {
    state_col="\033[2m[RESOLVED]\033[0m"
  } else {
    state_col="\033[1;32m[OPEN]\033[0m"
  }

  if (path != "") {
    loc = path
    if (line != "null") loc = loc ":" line
    printf "  %s  \033[0;36m%s\033[0m  \033[2m(%s)\033[0m\n", state_col, author, loc
  } else {
    printf "  %s  \033[0;36m%s\033[0m\n", state_col, author
  }
  printf "  %s\n\n", text
}
'

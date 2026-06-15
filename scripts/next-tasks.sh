#!/bin/bash
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$ROOT/data/jira.json"
JIRA_URL=$(jq -r '.jira_url // ""' "$DATA" 2>/dev/null)

jq -r '
  .next_tasks[] |
  [.key, .status, .priority, .summary] | @tsv
' "$DATA" 2>/dev/null \
| awk -v jira="$JIRA_URL" -F'\t' '
BEGIN { printf "\n" }
{
  key=$1; status=$2; pri=$3; summary=$4
  url = (jira != "") ? "  " jira "/browse/" key : ""
  printf "  \033[1;33m%-20s\033[0m  \033[2m%-14s\033[0m  %s\n", key, status, summary
  if (url != "") printf "  \033[2m%s\033[0m\n", url
  printf "\n"
}
END { printf "" }
'

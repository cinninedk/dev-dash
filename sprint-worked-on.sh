#!/bin/bash
# Lists issues you worked on this sprint — assigned to you and moved from
# Implement to Quality Assurance (i.e. you did the implementation).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/secrets/config"
JIRA_PASSWORD=$(cat "$SCRIPT_DIR/secrets/jira-token")

JQL="sprint in openSprints() AND project in ($JIRA_PROJECTS) AND status CHANGED FROM \"Implement\" TO \"Quality Assurance\" BY currentUser() ORDER BY updated DESC"

RAW=$(curl -s \
    "$JIRA_URL/rest/api/2/search" \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    --get \
    --data-urlencode "jql=$JQL" \
    --data-urlencode "maxResults=100" \
    --data-urlencode "fields=summary,status")

echo "$RAW" | jq -r '
  if .errorMessages then
    "Error: \(.errorMessages[])"
  else
    (.issues // []) | to_entries[] |
    "\(.key + 1).\t\(.value.key)\t[\(.value.fields.status.name)]\t\(.value.fields.summary)"
  end
' | column -t -s $'\t'

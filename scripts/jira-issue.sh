#!/bin/bash
# jira-issue.sh — fetch full Jira issue details for AI context
# Usage: ./scripts/jira-issue.sh PROJ-123
#        ./scripts/jira-issue.sh PROJ-123 | pbcopy

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"
JIRA_PASSWORD=$(cat "$ROOT/secrets/jira-token")

ISSUE="${1?Usage: $0 <ISSUE-KEY>}"

# ── Fetch issue ──────────────────────────────────────────────────────────────
DATA=$(curl -sf \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    "${JIRA_URL}/rest/api/2/issue/${ISSUE}?fields=summary,description,status,issuetype,priority,assignee,reporter,labels,comment,subtasks,parent,components,issuelinks" \
    2>/dev/null)

[ -z "$DATA" ] && { echo "Error: could not reach ${JIRA_URL}" >&2; exit 1; }

ERR=$(echo "$DATA" | jq -r '.errorMessages[0] // empty' 2>/dev/null)
[ -n "$ERR" ] && { echo "Jira error: $ERR" >&2; exit 1; }

# ── Header ───────────────────────────────────────────────────────────────────
echo "$DATA" | jq -r '
  .key as $key | .fields as $f |
  "# \($key): \($f.summary)",
  "**Type:** \($f.issuetype.name)  |  **Status:** \($f.status.name)  |  **Priority:** \($f.priority.name)",
  "**Reporter:** \($f.reporter.displayName // "–")  |  **Assignee:** \($f.assignee.displayName // "Unassigned")",
  (if (($f.labels // []) | length) > 0 then "**Labels:** \($f.labels | join(", "))" else empty end),
  (if (($f.components // []) | length) > 0 then "**Components:** \($f.components | map(.name) | join(", "))" else empty end),
  (if $f.parent then "**Epic:** \($f.parent.key) – \($f.parent.fields.summary // "")" else empty end)
'
echo ""

# ── Description ──────────────────────────────────────────────────────────────
echo "---"
echo ""
echo "## Description"
echo ""
echo "$DATA" | jq -r '.fields.description // "_No description provided_"'
echo ""

# ── Subtasks ─────────────────────────────────────────────────────────────────
SUBTASKS=$(echo "$DATA" | jq -r '
  (.fields.subtasks // []) | to_entries[] |
  "  \(.key + 1). \(.value.key)  [\(.value.fields.status.name)]  \(.value.fields.summary)"
')
if [ -n "$SUBTASKS" ]; then
    echo "## Subtasks"
    echo "$SUBTASKS"
    echo ""
fi

# ── Issue links ───────────────────────────────────────────────────────────────
LINKS=$(echo "$DATA" | jq -r '
  (.fields.issuelinks // [])[] |
  if .outwardIssue then
    "  \(.type.outward | ascii_upcase): \(.outwardIssue.key)  [\(.outwardIssue.fields.status.name)]  \(.outwardIssue.fields.summary)"
  elif .inwardIssue then
    "  \(.type.inward | ascii_upcase): \(.inwardIssue.key)  [\(.inwardIssue.fields.status.name)]  \(.inwardIssue.fields.summary)"
  else empty end
')
if [ -n "$LINKS" ]; then
    echo "## Linked issues"
    echo "$LINKS"
    echo ""
fi

# ── Comments ─────────────────────────────────────────────────────────────────
COMMENT_COUNT=$(echo "$DATA" | jq '.fields.comment.comments | length')
if [ "$COMMENT_COUNT" -gt 0 ]; then
    echo "## Comments ($COMMENT_COUNT)"
    echo ""
    echo "$DATA" | jq -r '
      .fields.comment.comments[] |
      "**\(.author.displayName // .author.name)** (\(.created[:10])):",
      .body,
      ""
    '
fi

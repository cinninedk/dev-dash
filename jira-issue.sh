#!/bin/bash
# jira-issue.sh — fetch full Jira issue details for AI context
# Usage: ./jira-issue.sh PROJ-123
#        ./jira-issue.sh PROJ-123 | pbcopy

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/secrets/config"
JIRA_PASSWORD=$(cat "$SCRIPT_DIR/secrets/jira-token")

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
if [ "$(echo "$DATA" | jq '(.fields.subtasks // []) | length')" -gt 0 ]; then
    echo "---"
    echo ""
    echo "## Subtasks"
    echo ""
    echo "$DATA" | jq -r '
      .fields.subtasks[] |
      "- [\(.key)] \(.fields.summary) — \(.fields.status.name)"
    '
    echo ""
fi

# ── Linked issues ─────────────────────────────────────────────────────────────
if [ "$(echo "$DATA" | jq '(.fields.issuelinks // []) | length')" -gt 0 ]; then
    echo "---"
    echo ""
    echo "## Linked Issues"
    echo ""
    echo "$DATA" | jq -r '
      .fields.issuelinks[] |
      if .inwardIssue then
        "- \(.type.inward): [\(.inwardIssue.key)] \(.inwardIssue.fields.summary) (\(.inwardIssue.fields.status.name))"
      elif .outwardIssue then
        "- \(.type.outward): [\(.outwardIssue.key)] \(.outwardIssue.fields.summary) (\(.outwardIssue.fields.status.name))"
      else empty end
    '
    echo ""
fi

# ── Comments (fetched separately to guarantee all are returned) ───────────────
COMMENTS=$(curl -sf \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    "${JIRA_URL}/rest/api/2/issue/${ISSUE}/comment?maxResults=100&orderBy=created" \
    2>/dev/null)

COMMENT_COUNT=$(echo "$COMMENTS" | jq '.total // 0' 2>/dev/null || echo 0)

if [ "$COMMENT_COUNT" -gt 0 ]; then
    echo "---"
    echo ""
    echo "## Comments ($COMMENT_COUNT)"
    echo ""
    echo "$COMMENTS" | jq -r '
      .comments[] |
      "**\(.author.displayName)** (\(.created[:10])):",
      .body,
      ""
    '
fi

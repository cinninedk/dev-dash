#!/bin/bash
# poll.sh — writes data/bitbucket.json and data/jira.json for the dashboard
# Run once manually or via launchd every 60s.
# Place this file next to index.html and the data/ folder.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/secrets/credentials"
PASSWORD=$(cat "$SCRIPT_DIR/secrets/bitbucket-token")
JIRA_PASSWORD=$(cat "$SCRIPT_DIR/secrets/jira-token")
INTERVAL="${1:-60}"

OUT_DIR="$(dirname "$0")/data"
mkdir -p "$OUT_DIR"
BB_OUT="$OUT_DIR/bitbucket.json"
JR_OUT="$OUT_DIR/jira.json"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Helpers ──

# Single curl: emits three lines — state, name, url
get_build_info() {
    local commit="$1"
    curl -s -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/build-status/1.0/commits/$commit" \
        | jq -r '
            (.values[0].state // "NO_BUILD"),
            (.values[0].name  // "Unknown"),
            (.values[0].url   // "")'
}

# Single curl: emits five lines — result, bugs, smells, vulns, hotspots
get_sonar_info() {
    local project="$1" slug="$2" commit="$3"
    local reports
    reports=$(curl -s -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/insights/1.0/projects/$project/repos/$slug/commits/$commit/reports")
    echo "$reports" | jq -r '
        [.values[]? | select(
            ((.key // "") | ascii_downcase | test("sonar|code-quality")) or
            ((.title // "") | ascii_downcase | test("sonar|code quality")) or
            ((.reporter // "") | ascii_downcase | test("sonar"))
        )][0] as $r |
        if $r != null then
            ($r.result // "NONE"),
            ($r.data // [] | map(select(.title | test("[Bb]ugs")))     | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Ss]mells")))   | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Vv]ulnerab"))) | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Hh]otspot")))  | .[0].value // 0 | tostring)
        else
            "NONE", "0", "0", "0", "0"
        end'
}

# Map sonar result to our labels
qg_label() {
    case "$1" in
        PASS|OK)    echo "PASS" ;;
        FAIL|ERROR) echo "FAIL" ;;
        WARN)       echo "WARN" ;;
        *)          echo "–" ;;
    esac
}

# ── Per-poll fetch logic ──

process_prs_to_json() {
    local json_input="$1"
    echo "$json_input" | jq -c '(.values // []) | map(select(.id != null)) | sort_by(.id) | .[]' | while read -r pr; do
        local id title repo slug project author branch jira_key commit \
              approvals reviewer_count tasks comments merge_outcome
        id=$(echo "$pr"             | jq -r '.id')
        title=$(echo "$pr"          | jq -r '.title')
        repo=$(echo "$pr"           | jq -r '.toRef.repository.name')
        slug=$(echo "$pr"           | jq -r '.toRef.repository.slug')
        project=$(echo "$pr"        | jq -r '.toRef.repository.project.key')
        author=$(echo "$pr"         | jq -r '.author.user.displayName // .author.user.name // "Unknown"')
        branch=$(echo "$pr"         | jq -r '.fromRef.displayId // ""')
        jira_key=$(echo "$branch"   | grep -oE '[A-Z]+-[0-9]+' | head -1)
        commit=$(echo "$pr"         | jq -r '.fromRef.latestCommit')
        approvals=$(echo "$pr"      | jq -r '[.reviewers[]? | select(.approved == true)] | length')
        reviewer_count=$(echo "$pr" | jq -r '.reviewers | length')
        tasks=$(echo "$pr"          | jq -r '.properties.openTaskCount // 0')
        comments=$(echo "$pr"       | jq -r '.properties.commentCount // 0')
        merge_outcome=$(echo "$pr"  | jq -r '.properties.mergeResult.outcome // "CLEAN"')

        local build_state build_name build_url
        {
            IFS= read -r build_state
            IFS= read -r build_name
            IFS= read -r build_url
        } < <(get_build_info "$commit")

        local qg_raw bugs smells vulns hotspots qg
        {
            IFS= read -r qg_raw
            IFS= read -r bugs
            IFS= read -r smells
            IFS= read -r vulns
            IFS= read -r hotspots
        } < <(get_sonar_info "$project" "$slug" "$commit")
        qg=$(qg_label "$qg_raw")

        jq -n \
            --argjson id "$id" \
            --arg title "$title" \
            --arg repo "$repo" \
            --arg slug "$slug" \
            --arg project "$project" \
            --arg author "$author" \
            --arg branch "$branch" \
            --arg jira_key "$jira_key" \
            --arg commit "$commit" \
            --argjson approvals "$approvals" \
            --argjson reviewer_count "$reviewer_count" \
            --argjson tasks "$tasks" \
            --argjson comments "$comments" \
            --arg merge_outcome "$merge_outcome" \
            --arg build_state "$build_state" \
            --arg build_name "$build_name" \
            --arg build_url "$build_url" \
            --arg qg_label "$qg" \
            --argjson bugs "${bugs:-0}" \
            --argjson smells "${smells:-0}" \
            --argjson vulns "${vulns:-0}" \
            --argjson hotspots "${hotspots:-0}" \
            '{id:$id, title:$title, repo:$repo, slug:$slug, project:$project,
              author:$author, branch:$branch, jira_key:$jira_key, commit:$commit,
              approvals:$approvals, reviewer_count:$reviewer_count,
              tasks:$tasks, comments:$comments, merge_outcome:$merge_outcome,
              build_state:$build_state, build_name:$build_name, build_url:$build_url, qg_label:$qg_label,
              bugs:$bugs, smells:$smells, vulns:$vulns, hotspots:$hotspots}'
    done
}

while true; do
log "Fetching Bitbucket PRs..."
MY_PRS_RAW=$(curl -s -H "Authorization: Bearer $PASSWORD" \
    "$STASH_URL/rest/api/1.0/dashboard/pull-requests?state=OPEN&role=AUTHOR")

REVIEWER_PRS_RAW=$(curl -s -H "Authorization: Bearer $PASSWORD" \
    "$STASH_URL/rest/api/1.0/dashboard/pull-requests?state=OPEN&role=REVIEWER" \
    | jq -c --arg user "$USERNAME" '
        .values |= (. // [] | map(select(
            (.author.user.name != $user) and
            (.author.user.name | ascii_downcase | test("renovate") | not) and
            ((.fromRef.id // "") | ascii_downcase | test("renovate") | not) and
            (.isDraft != true) and
            ((.title // "") | ascii_downcase | test("wip|draft") | not)
        ))) | .size = ((.values // []) | length)
    ')

MY_PRS_JSON=$(process_prs_to_json "$MY_PRS_RAW" | jq -s '.')
REV_PRS_JSON=$(process_prs_to_json "$REVIEWER_PRS_RAW" | jq -s '.')

jq -n \
    --argjson my_prs "$MY_PRS_JSON" \
    --argjson reviewer_prs "$REV_PRS_JSON" \
    --arg stash_url "$STASH_URL" \
    --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{my_prs:$my_prs, reviewer_prs:$reviewer_prs, stash_url:$stash_url, updated:$updated}' \
    > "$BB_OUT"

log "Bitbucket: $(echo "$MY_PRS_JSON" | jq 'length') my PRs, $(echo "$REV_PRS_JSON" | jq 'length') for review"

# ── Jira ──
log "Fetching Jira issues..."

# Fetch issues assigned to me, in the relevant statuses, across configured projects
JQL="sprint in openSprints() AND project in ($JIRA_PROJECTS) AND (assignee = currentUser() OR status CHANGED FROM \"Implement\" BY currentUser()) AND status in (\"Open\",\"Reopened\",\"Implement\",\"Quality Assurance\",\"Business Validation\",\"Resolved\") ORDER BY updated DESC"

JIRA_RAW=$(curl -s \
    "$JIRA_URL/rest/api/2/search" \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    --get \
    --data-urlencode "jql=$JQL" \
    --data-urlencode "maxResults=50" \
    --data-urlencode "fields=summary,status,issuetype,priority,assignee,updated")

ISSUES_JSON=$(echo "$JIRA_RAW" | jq '[.issues[]? | {
    key:     .key,
    summary: .fields.summary,
    status:  (.fields.status.name | ascii_upcase),
    type:    .fields.issuetype.name,
    priority:.fields.priority.name,
    updated: .fields.updated
}]' 2>/dev/null || echo "[]")

jq -n \
    --argjson issues "$ISSUES_JSON" \
    --arg jira_url "$JIRA_URL" \
    --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issues:$issues, jira_url:$jira_url, updated:$updated}' \
    > "$JR_OUT"

log "Jira: $(echo "$ISSUES_JSON" | jq 'length') issues written"
log "Done. BB → $BB_OUT  |  Jira → $JR_OUT. Sleeping ${INTERVAL}s."
sleep "$INTERVAL"
done
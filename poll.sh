#!/bin/bash
# poll.sh — writes data/bitbucket.json and data/jira.json for the dashboard

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/secrets/config"
PASSWORD=$(cat "$SCRIPT_DIR/secrets/bitbucket-token")
JIRA_PASSWORD=$(cat "$SCRIPT_DIR/secrets/jira-token")
SONAR_PASSWORD=$(cat "$SCRIPT_DIR/secrets/sonar-token")

OUT_DIR="$(dirname "$0")/data"
mkdir -p "$OUT_DIR"
BB_OUT="$OUT_DIR/bitbucket.json"
JR_OUT="$OUT_DIR/jira.json"
CACHE_FILE="$OUT_DIR/.build-cache.json"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
cfg() { grep "^$1:" "$SCRIPT_DIR/config.yaml" 2>/dev/null | awk -F': *' '{print $2}'; }

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

# Two curls: emits five lines — qg_raw, bugs, smells, vulns, hotspots
get_sonar_info() {
    local project="$1" slug="$2" commit="$3"
    local reports
    reports=$(curl -s -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/insights/1.0/projects/$project/repos/$slug/commits/$commit/reports" \
        | LC_ALL=C sed 's/\\uD[89ABab][0-9A-Fa-f][0-9A-Fa-f]\\u[Dd][CDEFcdef][0-9A-Fa-f][0-9A-Fa-f]/?/g')

    local row
    row=$(echo "$reports" | jq -r '
        [.values[]? | select(
            ((.key // "") | ascii_downcase | test("sonar|code-quality")) or
            ((.title // "") | ascii_downcase | test("sonar|code quality")) or
            ((.reporter // "") | ascii_downcase | test("sonar"))
        )][0] as $r |
        if $r != null then [
            ($r.link // ""),
            ($r.data // [] | map(select(.title | test("[Bb]ugs")))     | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Ss]mells")))   | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Vv]ulnerab"))) | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Hh]otspot")))  | .[0].value // 0 | tostring)
        ] | join("\t")
        else "\t0\t0\t0\t0" end')

    local sonar_link bugs smells vulns hotspots
    IFS=$'\t' read -r sonar_link bugs smells vulns hotspots <<< "$row"

    local qg_raw="NONE"
    if [ -n "$sonar_link" ] && [ "$sonar_link" != "null" ]; then
        local sonar_base sonar_proj sonar_branch
        sonar_base=$(echo "$sonar_link" | grep -oE 'https?://[^/]+')
        sonar_proj=$(echo "$sonar_link" | grep -oE '[?&]id=[^&]+' | cut -d= -f2 \
            | python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null)
        sonar_branch=$(echo "$sonar_link" | grep -oE '[?&]branch=[^&]+' | cut -d= -f2 \
            | python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null)

        if [ -n "$sonar_proj" ]; then
            qg_raw=$(curl -s -H "Authorization: Bearer $SONAR_PASSWORD" \
                --get \
                --data-urlencode "projectKey=$sonar_proj" \
                ${sonar_branch:+--data-urlencode "branch=$sonar_branch"} \
                "$sonar_base/api/qualitygates/project_status" \
                | jq -r '.projectStatus.status // "NONE"')
        fi
    fi

    echo "$qg_raw"
    echo "${bugs:-0}"
    echo "${smells:-0}"
    echo "${vulns:-0}"
    echo "${hotspots:-0}"
}

qg_label() {
    case "$1" in
        PASS|OK)    echo "PASS" ;;
        FAIL|ERROR) echo "FAIL" ;;
        WARN)       echo "WARN" ;;
        *)          echo "–" ;;
    esac
}

# ── Per-poll fetch logic ──
#
# $1 — raw Bitbucket JSON
# $2 — cache-updates tempfile (one JSON object per line, appended for each fresh fetch)
#
# Reads $CACHE (global) and $POLL_BUILD_STALE (global) set in the main loop.
# Skips Jenkins+Sonar curls when the PR's commit is unchanged, the last build
# wasn't INPROGRESS, and the cached data is newer than $POLL_BUILD_STALE seconds.

process_prs_to_json() {
    local json_input="$1"
    local cache_updates="$2"
    echo "$json_input" | jq -c '(.values // []) | map(select(.id != null)) | sort_by(.id) | .[]' | while read -r pr; do
        local id title repo slug project author branch jira_key commit \
              approvals needs_work reviewer_count tasks comments merge_outcome
        id=$(echo "$pr"             | jq -r '.id')
        title=$(echo "$pr"          | jq -r '.title')
        repo=$(echo "$pr"           | jq -r '.toRef.repository.name')
        slug=$(echo "$pr"           | jq -r '.toRef.repository.slug')
        project=$(echo "$pr"        | jq -r '.toRef.repository.project.key')
        author=$(echo "$pr"         | jq -r '.author.user.displayName // .author.user.name // "Unknown"')
        branch=$(echo "$pr"         | jq -r '.fromRef.displayId // ""')
        jira_key=$(echo "$branch"   | grep -oE '[A-Z]+-[0-9]+' | head -1)
        commit=$(echo "$pr"         | jq -r '.fromRef.latestCommit')
        approvals=$(echo "$pr"      | jq -r '[.reviewers[]? | select(.status == "APPROVED")] | length')
        needs_work=$(echo "$pr"     | jq -r '[.reviewers[]? | select(.status == "NEEDS_WORK")] | length')
        reviewer_count=$(echo "$pr" | jq -r '.reviewers | length')
        tasks=$(echo "$pr"          | jq -r '.properties.openTaskCount // 0')
        comments=$(echo "$pr"       | jq -r '.properties.commentCount // 0')
        merge_outcome=$(echo "$pr"  | jq -r '.properties.mergeResult.outcome // "CLEAN"')

        # ── Cache check ──
        local cached c_commit c_state c_fetched c_age use_cache
        cached=$(echo "$CACHE" | jq -c --argjson id "$id" '.[$id | tostring] // null')
        use_cache=false
        if [ "$cached" != "null" ]; then
            c_commit=$(echo "$cached" | jq -r '.commit // ""')
            c_state=$(echo "$cached"  | jq -r '.build_state // ""')
            c_fetched=$(echo "$cached" | jq -r '.fetched_at // ""')
            c_age=999999
            if [ -n "$c_fetched" ] && [ "$c_fetched" != "null" ]; then
                local ts
                ts=$(date -jf "%Y-%m-%dT%H:%M:%SZ" "$c_fetched" +%s 2>/dev/null || echo 0)
                [ "${ts:-0}" -gt 0 ] && c_age=$(( $(date +%s) - ts ))
            fi
            if [ "$commit" = "$c_commit" ] && \
               [ "$c_state" != "INPROGRESS" ] && \
               [ "$c_age" -lt "$POLL_BUILD_STALE" ]; then
                use_cache=true
            fi
        fi

        local build_state build_name build_url qg bugs smells vulns hotspots
        if $use_cache; then
            build_state=$(echo "$cached" | jq -r '.build_state')
            build_name=$(echo "$cached"  | jq -r '.build_name')
            build_url=$(echo "$cached"   | jq -r '.build_url')
            qg=$(echo "$cached"          | jq -r '.qg_label')
            bugs=$(echo "$cached"        | jq -r '.bugs')
            smells=$(echo "$cached"      | jq -r '.smells')
            vulns=$(echo "$cached"       | jq -r '.vulns')
            hotspots=$(echo "$cached"    | jq -r '.hotspots')
        else
            # Fresh fetch from Jenkins + SonarQube
            {
                IFS= read -r build_state
                IFS= read -r build_name
                IFS= read -r build_url
            } < <(get_build_info "$commit")

            local qg_raw
            {
                IFS= read -r qg_raw
                IFS= read -r bugs
                IFS= read -r smells
                IFS= read -r vulns
                IFS= read -r hotspots
            } < <(get_sonar_info "$project" "$slug" "$commit")
            qg=$(qg_label "$qg_raw")

            # Append cache entry (one compact JSON object per line)
            jq -cn \
                --argjson id "$id" \
                --arg commit "$commit" \
                --arg build_state "$build_state" \
                --arg build_name "$build_name" \
                --arg build_url "$build_url" \
                --arg qg_label "$qg" \
                --argjson bugs "${bugs:-0}" \
                --argjson smells "${smells:-0}" \
                --argjson vulns "${vulns:-0}" \
                --argjson hotspots "${hotspots:-0}" \
                --arg fetched_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                '{($id|tostring): {commit:$commit, build_state:$build_state, build_name:$build_name,
                  build_url:$build_url, qg_label:$qg_label, bugs:$bugs, smells:$smells,
                  vulns:$vulns, hotspots:$hotspots, fetched_at:$fetched_at}}' \
                >> "$cache_updates"
        fi

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
            --argjson needs_work "$needs_work" \
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
              approvals:$approvals, needs_work:$needs_work, reviewer_count:$reviewer_count,
              tasks:$tasks, comments:$comments, merge_outcome:$merge_outcome,
              build_state:$build_state, build_name:$build_name, build_url:$build_url, qg_label:$qg_label,
              bugs:$bugs, smells:$smells, vulns:$vulns, hotspots:$hotspots}'
    done
}

# ── Main loop ──

while true; do

# Read config (re-read each iteration so edits take effect without restart)
SLEEP=$(cfg poll_active_seconds);                 SLEEP=${SLEEP:-15}
POLL_BUILD_STALE=$(cfg poll_build_stale_seconds); POLL_BUILD_STALE=${POLL_BUILD_STALE:-600}
WORK_HOURS_ENABLED=$(cfg work_hours_enabled);     WORK_HOURS_ENABLED=${WORK_HOURS_ENABLED:-true}
WORK_START_HOUR=$(cfg work_start_hour);           WORK_START_HOUR=${WORK_START_HOUR:-8}
WORK_END_HOUR=$(cfg work_end_hour);               WORK_END_HOUR=${WORK_END_HOUR:-18}

NEXT_POLL=$(date -u -v+${SLEEP}S +%Y-%m-%dT%H:%M:%SZ)

# Load build/sonar cache (read once; available to process_prs_to_json subshells)
CACHE=$(cat "$CACHE_FILE" 2>/dev/null || echo '{}')
CACHE_UPDATES=$(mktemp)

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
            (.draft != true) and
            ((.title // "") | ascii_downcase | test("wip|draft") | not)
        ))) | .size = ((.values // []) | length)
    ')

MY_PRS_JSON=$(process_prs_to_json "$MY_PRS_RAW"       "$CACHE_UPDATES" | jq -s '.')
REV_PRS_JSON=$(process_prs_to_json "$REVIEWER_PRS_RAW" "$CACHE_UPDATES" | jq -s '.')

fetched_count=$(wc -l < "$CACHE_UPDATES" | tr -d ' ')
log "Bitbucket: $(echo "$MY_PRS_JSON" | jq 'length') my PRs, $(echo "$REV_PRS_JSON" | jq 'length') for review (${fetched_count} build/sonar fetched)"

# Merge cache updates (each line is one JSON object keyed by PR id)
if [ -s "$CACHE_UPDATES" ]; then
    jq -s --argjson base "$CACHE" \
        'reduce .[] as $x ($base; . * $x)' \
        "$CACHE_UPDATES" > "${CACHE_FILE}.tmp" && \
    mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
fi
rm -f "$CACHE_UPDATES"

# Evict cache entries for PRs that are no longer open
ALL_IDS=$(echo "$MY_PRS_JSON" "$REV_PRS_JSON" | \
    jq -s '[.[].[] | .id | tostring] | unique')
jq --argjson ids "$ALL_IDS" \
    'with_entries(select(.key as $k | ($ids | index($k)) != null))' \
    "${CACHE_FILE}" 2>/dev/null > "${CACHE_FILE}.tmp" && \
mv "${CACHE_FILE}.tmp" "$CACHE_FILE"

jq -n \
    --argjson my_prs "$MY_PRS_JSON" \
    --argjson reviewer_prs "$REV_PRS_JSON" \
    --arg stash_url "$STASH_URL" \
    --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg next_poll "$NEXT_POLL" \
    --argjson work_hours_enabled "$([ "$WORK_HOURS_ENABLED" = "true" ] && echo true || echo false)" \
    --argjson work_start_hour "$WORK_START_HOUR" \
    --argjson work_end_hour "$WORK_END_HOUR" \
    '{my_prs:$my_prs, reviewer_prs:$reviewer_prs, stash_url:$stash_url, updated:$updated, next_poll:$next_poll, work_hours_enabled:$work_hours_enabled, work_start_hour:$work_start_hour, work_end_hour:$work_end_hour}' \
    > "$BB_OUT"

# ── Jira ──
log "Fetching Jira issues..."

# Query 1: issues currently assigned to me (Open/Reopened/Implement)
JQL_MINE="sprint in openSprints() AND project in ($JIRA_PROJECTS) AND assignee = currentUser() AND status in (\"Open\",\"Reopened\",\"Implement\",\"Quality Assurance\",\"Business Validation\") ORDER BY updated DESC"

# Query 2: issues I implemented (moved out of Implement), now in QA/BV/Resolved
JQL_IMPL="sprint in openSprints() AND project in ($JIRA_PROJECTS) AND status in (\"Quality Assurance\",\"Business Validation\",\"Resolved\") AND status CHANGED FROM \"Implement\" BY currentUser() ORDER BY updated DESC"

# Query 3: all sprint QA issues with teknisk_QA label (any assignee)
JQL_TQA="sprint in openSprints() AND project in ($JIRA_PROJECTS) AND status = \"Quality Assurance\" AND labels = \"teknisk_QA\" ORDER BY updated DESC"

JQ_PROJ='[.issues[]? | {
    key:        .key,
    summary:    .fields.summary,
    status:     (.fields.status.name | ascii_upcase),
    type:       .fields.issuetype.name,
    priority:   .fields.priority.name,
    updated:    .fields.updated,
    teknisk_qa: ((.fields.labels // []) | any(. == "teknisk_QA"))
}]'

JQ_PROJ_TQA='[.issues[]? | {
    key:            .key,
    summary:        .fields.summary,
    status:         (.fields.status.name | ascii_upcase),
    type:           .fields.issuetype.name,
    priority:       .fields.priority.name,
    updated:        .fields.updated,
    teknisk_qa:     true,
    assigned_to_me: ((.fields.assignee.name // "") == $username)
}]'

MINE_JSON=$(curl -s \
    "$JIRA_URL/rest/api/2/search" \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    --get \
    --data-urlencode "jql=$JQL_MINE" \
    --data-urlencode "maxResults=50" \
    --data-urlencode "fields=summary,status,issuetype,priority,updated,labels" \
    | jq "$JQ_PROJ" 2>/dev/null || echo "[]")

IMPL_JSON=$(curl -s \
    "$JIRA_URL/rest/api/2/search" \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    --get \
    --data-urlencode "jql=$JQL_IMPL" \
    --data-urlencode "maxResults=50" \
    --data-urlencode "fields=summary,status,issuetype,priority,updated,labels" \
    | jq "$JQ_PROJ" 2>/dev/null || echo "[]")

TQA_JSON=$(curl -s \
    "$JIRA_URL/rest/api/2/search" \
    -H "Authorization: Bearer $JIRA_PASSWORD" \
    --get \
    --data-urlencode "jql=$JQL_TQA" \
    --data-urlencode "maxResults=50" \
    --data-urlencode "fields=summary,status,issuetype,priority,updated,labels,assignee" \
    | jq --arg username "$USERNAME" "$JQ_PROJ_TQA" 2>/dev/null || echo "[]")

MINE_JSON=${MINE_JSON:-[]}
IMPL_JSON=${IMPL_JSON:-[]}
TQA_JSON=${TQA_JSON:-[]}

ISSUES_JSON=$(jq -n --argjson a "$MINE_JSON" --argjson b "$IMPL_JSON" '
  ($a + ($b | map(select(.key as $k | ($a | map(.key) | index($k)) == null))))
  | map(select(.teknisk_qa != true))
')

jq -n \
    --argjson issues "$ISSUES_JSON" \
    --argjson tqa_issues "$TQA_JSON" \
    --arg jira_url "$JIRA_URL" \
    --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{issues:$issues, tqa_issues:$tqa_issues, jira_url:$jira_url, updated:$updated}' \
    > "$JR_OUT"

log "Jira: $(echo "$MINE_JSON" | jq 'length') mine + $(echo "$IMPL_JSON" | jq 'length') implemented = $(echo "$ISSUES_JSON" | jq 'length') issues, $(echo "$TQA_JSON" | jq 'length') tekn_qa"

log "Done. BB → $BB_OUT  |  Jira → $JR_OUT. Sleeping ${SLEEP}s (next_poll: $NEXT_POLL)."
sleep "$SLEEP"
done

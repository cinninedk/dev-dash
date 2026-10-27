#!/bin/bash
# poll.sh — writes data/bitbucket.json and data/jira.json for the dashboard

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/secrets/config"
PASSWORD=$(cat "$ROOT/secrets/bitbucket-token")
JIRA_PASSWORD=$(cat "$ROOT/secrets/jira-token")
SONAR_PASSWORD=$(cat "$ROOT/secrets/sonar-token")

# Every endpoint below sits behind the VPN. Without a timeout, a curl call
# started while the VPN is down (or dropped mid-request) can block on the
# dead socket for many minutes — the OS has no way to know the tunnel is
# gone until its own retransmit timeout gives up. That wedges this whole
# long-running loop even after the VPN comes back, since the stuck call
# never returns to let the loop continue. Bound every call instead.
CURL_OPTS=(--connect-timeout 3 --max-time 10)

# Fast reachability probe (short-circuits vpn_ok well before CURL_OPTS'
# own timeout would) — any HTTP response at all means the VPN/network is up,
# regardless of status code, so no auth header is needed here.
vpn_ok() {
    curl -s -o /dev/null --connect-timeout 2 --max-time 3 "$STASH_URL"
}

# Usernames for "is this me" checks: Bitbucket's from own PRs (may differ from
# USERNAME in config), Jira's for TQA assignee and activity by others. Looked
# up again on each poll until they resolve: the poller starts at login, often
# before the VPN is up, and an empty name filters out nobody.
BB_USERNAME=""
JIRA_ME=""
resolve_me() {
    [ -n "$BB_USERNAME" ] || BB_USERNAME=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/api/1.0/dashboard/pull-requests?state=OPEN&role=AUTHOR&limit=1" \
        | jq -r '.values[0].author.user.name // ""' 2>/dev/null)
    [ -n "$JIRA_ME" ] || JIRA_ME=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $JIRA_PASSWORD" \
        "$JIRA_URL/rest/api/2/myself" | jq -r '.name // ""' 2>/dev/null)
}

OUT_DIR="$ROOT/data"
mkdir -p "$OUT_DIR"
BB_OUT="$OUT_DIR/bitbucket.json"
JR_OUT="$OUT_DIR/jira.json"
JK_OUT="$OUT_DIR/jenkins.json"
CACHE_FILE="$OUT_DIR/.build-cache.json"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
cfg() { grep "^$1:" "$ROOT/config.yaml" 2>/dev/null | awk -F': *' '{print $2}'; }

# ── Helpers ──

# Single curl: emits three lines — state, name, url
get_build_info() {
    local commit="$1"
    curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/build-status/1.0/commits/$commit" \
        | jq -r 'select(has("values")) |      # an error response prints nothing = failed
            (.values[0].state // "NO_BUILD"),
            (.values[0].name  // "Unknown"),
            (.values[0].url   // "")'
}

# Up to three curls: emits six lines — "qg_raw<TAB>sonar_url", bugs, smells,
# vulns, hotspots, building (true/false)
get_sonar_info() {
    local project="$1" slug="$2" commit="$3" cached_sonar_url="$4" branch="$5"
    local reports
    reports=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/insights/1.0/projects/$project/repos/$slug/commits/$commit/reports" \
        | LC_ALL=C sed 's/\\uD[89ABab][0-9A-Fa-f][0-9A-Fa-f]\\u[Dd][CDEFcdef][0-9A-Fa-f][0-9A-Fa-f]/?/g')
    # A failed request (timeout, error page) is not "no report": say so, so the
    # caller keeps what it knew instead of caching an empty result.
    if ! echo "$reports" | jq -e '.values' >/dev/null 2>&1; then
        printf 'FETCHFAIL\t\n0\n0\n0\n0\nfalse\n'
        return
    fi

    local row
    row=$(echo "$reports" | jq -r '
        [.values[]? | select(
            ((.key // "") | ascii_downcase | test("sonar|code-quality")) or
            ((.title // "") | ascii_downcase | test("sonar|code quality")) or
            ((.reporter // "") | ascii_downcase | test("sonar"))
        )][0] as $r |
        if $r != null then [
            ($r.link | if type == "string" and startswith("http") then . else "NOLINK" end),
            ($r.data // [] | map(select(.title | test("[Bb]ugs")))     | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Ss]mells")))   | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Vv]ulnerab"))) | .[0].value // 0 | tostring),
            ($r.data // [] | map(select(.title | test("[Hh]otspot")))  | .[0].value // 0 | tostring)
        ] | join("\t")
        else "NOREPORT\t0\t0\t0\t0" end')

    # Markers, not empty fields: read with IFS=tab merges an empty first field
    # into the next one.
    local sonar_link bugs smells vulns hotspots report_found=true
    IFS=$'\t' read -r sonar_link bugs smells vulns hotspots <<< "$row"
    case "$sonar_link" in
        NOREPORT) report_found=false; sonar_link="" ;;
        NOLINK)   sonar_link="" ;;
    esac

    local qg_raw="NONE"
    local building="false"

    # Resolve project key/branch off whichever link we have: the current
    # commit's own report if one has posted yet, else the last known link
    # (from cache) — that's still enough to ask Sonar "are you analyzing
    # this branch right now?" even before this commit's report shows up.
    local lookup_url="$sonar_link"
    # Some reports come without a link. The Sonar project is per repo and the
    # branch is the PR's own, so look the project up by repo name (exact key
    # match) and build the link — it also makes the QG badge clickable.
    if [ -z "$lookup_url" ] && $report_found && [ -n "$SONAR_URL" ] && [ -n "$branch" ]; then
        local key
        key=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $SONAR_PASSWORD" --get \
            --data-urlencode "qualifiers=TRK" --data-urlencode "q=$slug" \
            "$SONAR_URL/api/components/search" \
            | jq -r --arg s "$slug" '[.components[]?.key | select(. == $s or endswith(":" + $s))][0] // empty' 2>/dev/null)
        if [ -n "$key" ]; then
            lookup_url="$SONAR_URL/dashboard?id=$(jq -rn --arg v "$key" '$v|@uri')&branch=$(jq -rn --arg v "$branch" '$v|@uri')"
            sonar_link="$lookup_url"
        fi
    fi
    if [ -z "$lookup_url" ]; then
        lookup_url="$cached_sonar_url"
    fi

    if [ -n "$lookup_url" ] && [ "$lookup_url" != "null" ]; then
        local sonar_base sonar_proj sonar_branch
        sonar_base=$(echo "$lookup_url" | grep -oE 'https?://[^/]+')
        sonar_proj=$(echo "$lookup_url" | grep -oE '[?&]id=[^&]+' | cut -d= -f2 \
            | python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null)
        sonar_branch=$(echo "$lookup_url" | grep -oE '[?&]branch=[^&]+' | cut -d= -f2 \
            | python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))" 2>/dev/null)

        if [ -n "$sonar_proj" ]; then
            if $report_found && [ -n "$sonar_link" ]; then
                qg_raw=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $SONAR_PASSWORD" \
                    --get \
                    --data-urlencode "projectKey=$sonar_proj" \
                    ${sonar_branch:+--data-urlencode "branch=$sonar_branch"} \
                    "$sonar_base/api/qualitygates/project_status" \
                    | jq -r '.projectStatus.status // "NONE"')

                # Counts from the same Sonar analysis as the gate (new code, like
                # the gate judges). The commit's Bitbucket report can describe a
                # different analysis than Sonar's latest — right after a push, or
                # while Sonar is still processing — which showed smells next to
                # a passing gate. The report's numbers remain the fallback.
                local measures
                measures=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $SONAR_PASSWORD" \
                    --get \
                    --data-urlencode "component=$sonar_proj" \
                    ${sonar_branch:+--data-urlencode "branch=$sonar_branch"} \
                    --data-urlencode "metricKeys=new_bugs,new_code_smells,new_vulnerabilities,new_security_hotspots" \
                    "$sonar_base/api/measures/component" \
                    | jq -r '(.component.measures // []) | map({(.metric): (.value // .period.value // "0")}) | add // {}
                             | if length == 4 then [.new_bugs, .new_code_smells, .new_vulnerabilities, .new_security_hotspots] | join("\t") else empty end' 2>/dev/null)
                [ -n "$measures" ] && IFS=$'\t' read -r bugs smells vulns hotspots <<< "$measures"
            fi

            # Compute Engine queue for this branch: PENDING/IN_PROGRESS means
            # Sonar is actively analyzing it right now.
            local ce
            ce=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $SONAR_PASSWORD" \
                --get \
                --data-urlencode "component=$sonar_proj" \
                ${sonar_branch:+--data-urlencode "branch=$sonar_branch"} \
                "$sonar_base/api/ce/component")
            if echo "$ce" | jq -e '[(.queue // [])[] | select(.status == "PENDING" or .status == "IN_PROGRESS")] | length > 0' \
                >/dev/null 2>&1; then
                building="true"
            fi
        fi
    fi

    echo "${qg_raw}	${sonar_link:-}"
    echo "${bugs:-0}"
    echo "${smells:-0}"
    echo "${vulns:-0}"
    echo "${hotspots:-0}"
    echo "${building}"
}

qg_label() {
    case "$1" in
        PASS|OK)    echo "PASS" ;;
        FAIL|ERROR) echo "FAIL" ;;
        WARN)       echo "WARN" ;;
        *)          echo "–" ;;
    esac
}

# Single curl: returns count of open (unresolved) comment threads on a PR,
# not counting threads I started myself
get_unresolved_comments() {
    local project="$1" slug="$2" pr_id="$3"
    local count=0 start=0 is_last="false"
    while [ "$is_last" != "true" ]; do
        local body
        body=$(curl -sf "${CURL_OPTS[@]}" \
            -H "Authorization: Bearer $PASSWORD" \
            "${STASH_URL}/rest/api/1.0/projects/${project}/repos/${slug}/pull-requests/${pr_id}/activities?limit=100&start=${start}" \
            2>/dev/null) || break
        [ -z "$body" ] && break
        local page_count
        page_count=$(echo "$body" | jq --arg me "$BB_USERNAME" '[.values[]? | select(.action == "COMMENTED" and (.comment.parent == null) and (.comment.state != "RESOLVED") and (.comment.threadResolved != true) and (.comment.author.name != $me))] | length' 2>/dev/null || echo 0)
        count=$((count + page_count))
        is_last=$(echo "$body" | jq -r '.isLastPage // true')
        start=$(echo "$body" | jq -r '.nextPageStart // 0')
    done
    echo "$count"
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
              approvals needs_work reviewer_count tasks comments merge_outcome draft updated
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
        comments=$(get_unresolved_comments "$project" "$slug" "$id")
        merge_outcome=$(echo "$pr"  | jq -r '.properties.mergeResult.outcome // "CLEAN"')
        # same draft rule as the For Review filter and the kanban PR badge
        draft=$(echo "$pr"          | jq -r '(.draft == true) or ((.title // "") | ascii_downcase | test("wip|draft"))')
        updated=$(echo "$pr"        | jq -r '.updatedDate // 0')      # epoch ms, any activity on the PR

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
                ts=$(date -juf "%Y-%m-%dT%H:%M:%SZ" "$c_fetched" +%s 2>/dev/null || echo 0)   # UTC, not local
                [ "${ts:-0}" -gt 0 ] && c_age=$(( $(date +%s) - ts ))
            fi
            if [ "$commit" = "$c_commit" ] && \
               [ "$c_state" != "INPROGRESS" ] && \
               [ "$c_age" -lt "$POLL_BUILD_STALE" ]; then
                use_cache=true
            fi
        fi

        local build_state build_name build_url qg bugs smells vulns hotspots sonar_url sonar_building
        if $use_cache; then
            build_state=$(echo "$cached" | jq -r '.build_state')
            build_name=$(echo "$cached"  | jq -r '.build_name')
            build_url=$(echo "$cached"   | jq -r '.build_url')
            qg=$(echo "$cached"          | jq -r '.qg_label')
            bugs=$(echo "$cached"        | jq -r '.bugs')
            smells=$(echo "$cached"      | jq -r '.smells')
            vulns=$(echo "$cached"       | jq -r '.vulns')
            hotspots=$(echo "$cached"    | jq -r '.hotspots')
            sonar_url=$(echo "$cached"   | jq -r '.sonar_url // ""')
            sonar_building=$(echo "$cached" | jq -r '.sonar_building // false')
        else
            # Fresh fetch from Jenkins + SonarQube
            {
                IFS= read -r build_state
                IFS= read -r build_name
                IFS= read -r build_url
            } < <(get_build_info "$commit")

            local qg_raw prev_sonar_url_for_lookup
            prev_sonar_url_for_lookup=$(echo "$cached" | jq -r '.sonar_url // ""')
            {
                IFS= read -r qg_line
                IFS= read -r bugs
                IFS= read -r smells
                IFS= read -r vulns
                IFS= read -r hotspots
                IFS= read -r sonar_building
            } < <(get_sonar_info "$project" "$slug" "$commit" "$prev_sonar_url_for_lookup" "$branch")
            qg_raw="${qg_line%%	*}"
            sonar_url="${qg_line#*	}"
            [[ "$sonar_url" =~ ^https?:// ]] || sonar_url=""
            qg=$(qg_label "$qg_raw")

            # A request that failed (timeout, error page) returns nothing, not
            # "no build" / "no report". Keep the last known values for that part
            # and don't cache this fetch, so the next cycle asks again. (Caching
            # it used to show a green PR as build "" / QG – for minutes.)
            local fetch_failed=false
            if [ -z "$build_state" ]; then
                fetch_failed=true
                build_state=$(echo "$cached" | jq -r '.build_state // "NO_BUILD"')
                build_name=$(echo "$cached"  | jq -r '.build_name // "Unknown"')
                build_url=$(echo "$cached"   | jq -r '.build_url // ""')
            fi
            if [ "$qg_raw" = "FETCHFAIL" ]; then
                fetch_failed=true
                qg=$(echo "$cached"        | jq -r '.qg_label // "–"')
                sonar_url=$(echo "$cached" | jq -r '.sonar_url // ""')
                bugs=$(echo "$cached"      | jq -r '.bugs // 0')
                smells=$(echo "$cached"    | jq -r '.smells // 0')
                vulns=$(echo "$cached"     | jq -r '.vulns // 0')
                hotspots=$(echo "$cached"  | jq -r '.hotspots // 0')
                sonar_building=$(echo "$cached" | jq -r '.sonar_building // false')
            fi
            $fetch_failed && echo "[$(date '+%H:%M:%S')] ⚠ PR #$id: build/Sonar request failed — kept last known, retrying next cycle" >&2

            # If build is INPROGRESS and no Sonar report attached yet, keep last known QG data
            if [ "$build_state" = "INPROGRESS" ] && [ -z "$sonar_url" ] && [ "$cached" != "null" ]; then
                local prev_qg prev_sonar_url prev_bugs prev_smells prev_vulns prev_hotspots
                prev_qg=$(echo "$cached"        | jq -r '.qg_label // "–"')
                prev_sonar_url=$(echo "$cached" | jq -r '.sonar_url // ""')
                prev_bugs=$(echo "$cached"      | jq -r '.bugs // 0')
                prev_smells=$(echo "$cached"    | jq -r '.smells // 0')
                prev_vulns=$(echo "$cached"     | jq -r '.vulns // 0')
                prev_hotspots=$(echo "$cached"  | jq -r '.hotspots // 0')
                if [ "$prev_qg" != "–" ] && [ -n "$prev_qg" ]; then
                    qg="$prev_qg"
                    sonar_url="$prev_sonar_url"
                    bugs="$prev_bugs"
                    smells="$prev_smells"
                    vulns="$prev_vulns"
                    hotspots="$prev_hotspots"
                fi
            fi

            # Append cache entry (one compact JSON object per line)
            $fetch_failed || jq -cn \
                --argjson id "$id" \
                --arg commit "$commit" \
                --arg build_state "$build_state" \
                --arg build_name "$build_name" \
                --arg build_url "$build_url" \
                --arg qg_label "$qg" \
                --arg sonar_url "${sonar_url:-}" \
                --argjson bugs "${bugs:-0}" \
                --argjson smells "${smells:-0}" \
                --argjson vulns "${vulns:-0}" \
                --argjson hotspots "${hotspots:-0}" \
                --argjson sonar_building "${sonar_building:-false}" \
                --arg fetched_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                '{($id|tostring): {commit:$commit, build_state:$build_state, build_name:$build_name,
                  build_url:$build_url, qg_label:$qg_label, sonar_url:$sonar_url, bugs:$bugs, smells:$smells,
                  vulns:$vulns, hotspots:$hotspots, sonar_building:$sonar_building, fetched_at:$fetched_at}}' \
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
            --argjson draft "$draft" \
            --argjson updated "$updated" \
            --arg build_state "$build_state" \
            --arg build_name "$build_name" \
            --arg build_url "$build_url" \
            --arg qg_label "$qg" \
            --arg sonar_url "${sonar_url:-}" \
            --argjson bugs "${bugs:-0}" \
            --argjson smells "${smells:-0}" \
            --argjson vulns "${vulns:-0}" \
            --argjson hotspots "${hotspots:-0}" \
            --argjson sonar_building "${sonar_building:-false}" \
            '{id:$id, title:$title, repo:$repo, slug:$slug, project:$project,
              author:$author, branch:$branch, jira_key:$jira_key, commit:$commit,
              approvals:$approvals, needs_work:$needs_work, reviewer_count:$reviewer_count,
              tasks:$tasks, comments:$comments, merge_outcome:$merge_outcome, draft:$draft, updated:$updated,
              build_state:$build_state, build_name:$build_name, build_url:$build_url, qg_label:$qg_label,
              sonar_url:$sonar_url, bugs:$bugs, smells:$smells, vulns:$vulns, hotspots:$hotspots,
              sonar_building:$sonar_building}'
    done
}

# ── Fetchers ── one per source, each writing its own data/*.json

poll_bitbucket() {
    resolve_me
    # Load build/sonar cache (read once; available to process_prs_to_json subshells)
    # An empty or broken file must read as {} — jq prints nothing for empty
    # input, and an empty $CACHE used to break every later merge, for good.
    CACHE=$(jq -c 'objects' "$CACHE_FILE" 2>/dev/null); [ -n "$CACHE" ] || CACHE='{}'
    CACHE_UPDATES=$(mktemp)

    log "Fetching Bitbucket PRs..."
    MY_PRS_RAW=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/api/1.0/dashboard/pull-requests?state=OPEN&role=AUTHOR")

    REVIEWER_PRS_RAW=$(curl -s "${CURL_OPTS[@]}" -H "Authorization: Bearer $PASSWORD" \
        "$STASH_URL/rest/api/1.0/dashboard/pull-requests?state=OPEN&role=REVIEWER" \
        | jq -c --arg user "$BB_USERNAME" '
            .values |= (. // [] | map(select(
                (.author.user.name != $user) and
                (.author.user.name | ascii_downcase | test("renovate") | not) and
                ((.fromRef.id // "") | ascii_downcase | test("renovate") | not) and
                (.draft != true) and
                ((.title // "") | ascii_downcase | test("wip|draft") | not) and
                ([.reviewers[]? | select(.status == "APPROVED")] | length) == 0
            ))) | .size = ((.values // []) | length)
        ')

    MY_PRS_JSON=$(process_prs_to_json "$MY_PRS_RAW"       "$CACHE_UPDATES" | jq -s '.')
    REV_PRS_JSON=$(process_prs_to_json "$REVIEWER_PRS_RAW" "$CACHE_UPDATES" | jq -s '.')

    fetched_count=$(wc -l < "$CACHE_UPDATES" | tr -d ' ')
    log "Bitbucket: $(echo "$MY_PRS_JSON" | jq 'length') my PRs, $(echo "$REV_PRS_JSON" | jq 'length') for review (${fetched_count} build/sonar fetched)"

    # Merge this cycle's fetches (one JSON object per line, keyed by PR id) and
    # evict PRs that are no longer open, in one write.
    ALL_IDS=$(echo "$MY_PRS_JSON" "$REV_PRS_JSON" | jq -s '[.[].[] | .id | tostring] | unique')
    jq -n --argjson base "$CACHE" --argjson ids "${ALL_IDS:-[]}" --slurpfile up "$CACHE_UPDATES" \
        'reduce $up[] as $x ($base; . * $x) | with_entries(select(.key as $k | ($ids | index($k)) != null))' \
        > "${CACHE_FILE}.tmp" && mv "${CACHE_FILE}.tmp" "$CACHE_FILE"
    rm -f "$CACHE_UPDATES"

    jq -n \
        --argjson my_prs "$MY_PRS_JSON" \
        --argjson reviewer_prs "$REV_PRS_JSON" \
        --arg stash_url "$STASH_URL" \
        --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{my_prs:$my_prs, reviewer_prs:$reviewer_prs, stash_url:$stash_url, updated:$updated}' \
        > "$BB_OUT.tmp" && mv "$BB_OUT.tmp" "$BB_OUT"
}

# Recent activity by OTHER people on a kanban issue — comments, status changes
# and field edits, newest first, at most 10 — for the board's "new" badges.
# Board/admin noise (Rank, Sprint rollover, time tracking) is left out. Times
# become ISO with a colon offset ("+02:00") so every browser parses them.
JQ_ACTIVITY='def iso: sub("(?<h>[+-][0-9]{2})(?<m>[0-9]{2})$"; "\(.h):\(.m)");
def activity($me):
  ([ (.changelog.histories // [])[] | select(.author.name != $me) | . as $h
     | [.items[] | select(.field as $f | ["Rank","Sprint","timespent","timeestimate","timeoriginalestimate","WorklogId"] | index($f) | not)]
     | select(length > 0)
     | (map(select(.field == "status")) | first) as $st
     | if $st then {type: "status", who: $h.author.displayName, when: ($h.created | iso), from: $st.fromString, to: $st.toString}
       else {type: "edit", who: $h.author.displayName, when: ($h.created | iso), fields: (map(.field) | unique)} end ]
   + [ (.fields.comment.comments // [])[] | select(.author.name != $me)
       | {type: "comment", who: .author.displayName, when: (.created | iso), text: (.body | gsub("\\s+"; " ") | .[0:160])} ])
  | sort_by(.when) | reverse | .[0:10];
'

poll_jira() {
    resolve_me
    # ── Jira ──
    log "Fetching Jira issues..."

    # JQL queries are defined in secrets/config as JQL_MINE, JQL_IMPL, JQL_TQA, JQL_NEXT

    JQ_PROJ=$JQ_ACTIVITY'[.issues[]? | {
        id:         .id,
        key:        .key,
        summary:    .fields.summary,
        status:     (.fields.status.name | ascii_upcase),
        type:       .fields.issuetype.name,
        priority:   .fields.priority.name,
        updated:    .fields.updated,
        parent_key: (.fields.parent.key // null),
        teknisk_qa: ((.fields.labels // []) | any(. == "teknisk_QA")),
        unassigned: (.fields.assignee == null),
        assignee:   (.fields.assignee.displayName // null),
        blocked_by: [(.fields.issuelinks // [])[] | select(.type.inward == "is blocked by" and .inwardIssue != null) | {key: .inwardIssue.key, status: (.inwardIssue.fields.status.name | ascii_upcase)}],
        activity:   activity($me)
    }]'

    JQ_PROJ_TQA=$JQ_ACTIVITY'[.issues[]? | {
        id:             .id,
        key:            .key,
        summary:        .fields.summary,
        status:         (.fields.status.name | ascii_upcase),
        type:           .fields.issuetype.name,
        priority:       .fields.priority.name,
        updated:        .fields.updated,
        teknisk_qa:     true,
        assigned_to_me: ((.fields.assignee.name // "") == $me),
        unassigned:     (.fields.assignee == null),
        activity:       activity($me)
    }]'

    MINE_JSON=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/api/2/search" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        --get \
        --data-urlencode "jql=$JQL_MINE" \
        --data-urlencode "maxResults=50" \
        --data-urlencode "fields=summary,status,issuetype,priority,updated,labels,assignee,issuelinks,parent,comment" \
        --data-urlencode "expand=changelog" \
        | jq --arg me "$JIRA_ME" "$JQ_PROJ" 2>/dev/null || echo "[]")

    IMPL_JSON=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/api/2/search" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        --get \
        --data-urlencode "jql=$JQL_IMPL" \
        --data-urlencode "maxResults=50" \
        --data-urlencode "fields=summary,status,issuetype,priority,updated,labels,assignee,issuelinks,parent,comment" \
        --data-urlencode "expand=changelog" \
        | jq --arg me "$JIRA_ME" "$JQ_PROJ" 2>/dev/null || echo "[]")

    TQA_JSON=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/api/2/search" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        --get \
        --data-urlencode "jql=$JQL_TQA" \
        --data-urlencode "maxResults=50" \
        --data-urlencode "fields=summary,status,issuetype,priority,updated,labels,assignee,comment" \
        --data-urlencode "expand=changelog" \
        | jq --arg me "$JIRA_ME" "$JQ_PROJ_TQA" 2>/dev/null || echo "[]")

    _JQ_NEXT='[.issues[]? | {
        key:        .key,
        summary:    .fields.summary,
        status:     (.fields.status.name | ascii_upcase),
        type:       .fields.issuetype.name,
        priority:   .fields.priority.name,
        updated:    .fields.updated,
        teknisk_qa: ((.fields.labels // []) | any(. == "teknisk_QA"))
    }]'

    NEXT_CURRENT=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/api/2/search" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        --get \
        --data-urlencode "jql=$JQL_NEXT_CURRENT" \
        --data-urlencode "maxResults=50" \
        --data-urlencode "fields=summary,status,issuetype,priority,updated,labels" \
        | jq "$_JQ_NEXT" 2>/dev/null || echo "[]")

    NEXT_FUTURE=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/api/2/search" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        --get \
        --data-urlencode "jql=$JQL_NEXT_FUTURE" \
        --data-urlencode "maxResults=50" \
        --data-urlencode "fields=summary,status,issuetype,priority,updated,labels" \
        | jq "$_JQ_NEXT" 2>/dev/null || echo "[]")

    NEXT_JSON=$(jq -n \
        --argjson current "$NEXT_CURRENT" \
        --argjson future  "$NEXT_FUTURE" \
        '($current | map(. + {sprint_type:"current"})) + ($future | map(. + {sprint_type:"next"}))')

    MINE_JSON=${MINE_JSON:-[]}
    IMPL_JSON=${IMPL_JSON:-[]}
    TQA_JSON=${TQA_JSON:-[]}
    NEXT_JSON=${NEXT_JSON:-[]}

    ISSUES_JSON=$(jq -n --argjson a "$MINE_JSON" --argjson b "$IMPL_JSON" '
      ($a | map(select(.teknisk_qa != true))) as $mine |
      ($mine | map(.key)) as $mine_keys |
      $mine + ($b | map(select(.key as $k | ($mine_keys | index($k)) == null)))
    ')

    # Mark each blocker with whether it is assigned to me (i.e. appears in my own issues)
    MINE_KEYS=$(echo "$MINE_JSON" | jq '[.[].key]')
    ISSUES_JSON=$(echo "$ISSUES_JSON" | jq --argjson mine_keys "${MINE_KEYS:-[]}" '
      map(.blocked_by = ((.blocked_by // []) | map(. + {mine: (.key as $k | ($mine_keys | index($k)) != null)})))
    ')

    # Flag which of my issues actually sit in the current open sprint. Anything
    # assigned to me but outside the active sprint gets a NOT IN SPRINT warning.
    MINE_SPRINT_KEYS=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/api/2/search" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        --get \
        --data-urlencode "jql=project in ($JIRA_PROJECTS) AND assignee = currentUser() AND sprint in openSprints()" \
        --data-urlencode "maxResults=200" \
        --data-urlencode "fields=key" \
        | jq '[.issues[]?.key]' 2>/dev/null || echo "[]")
    ISSUES_JSON=$(echo "$ISSUES_JSON" | jq --argjson sprint_keys "${MINE_SPRINT_KEYS:-[]}" '
      map(.in_current_sprint = ((.key as $k | ($sprint_keys | index($k)) != null)))
    ')

    IMPL_KEYS=$(echo "$IMPL_JSON" | jq '[.[].key]')

    TQA_JSON=$(echo "$TQA_JSON" | jq --argjson impl_keys "${IMPL_KEYS:-[]}" '
      map(. + {implemented_by_me: (.key as $k | ($impl_keys | index($k)) != null)})
    ')

    # Linked PRs per kanban card: Jira's development panel lists them (by the
    # issue's numeric id, not its key); Bitbucket is asked only about the OPEN ones,
    # for draft + approvals, which Jira doesn't carry. Draft = Bitbucket's flag or
    # WIP/draft in the title, like the For Review filter. Per issue:
    #   {review, approved, draft, merged} — open counts split three ways + merged.
    # Declined PRs are dropped. NEXT TASK candidates are skipped — ~70 unassigned
    # issues every cycle, and nobody has a PR on them yet.
    PRS='{}'
    for ID in $(jq -rn --argjson a "$ISSUES_JSON" --argjson b "$TQA_JSON" '($a + $b) | map(.id) | unique | .[]'); do
        LIST=$(curl -s "${CURL_OPTS[@]}" \
            "$JIRA_URL/rest/dev-status/latest/issue/detail?issueId=$ID&applicationType=stash&dataType=pullrequest" \
            -H "Authorization: Bearer $JIRA_PASSWORD" \
            | jq -c '[.detail[]?.pullRequests[]? | {status, url}]' 2>/dev/null)
        [ -z "$LIST" ] && continue
        C=$(jq -c '{review:0, approved:0, draft:0, merged:([.[] | select(.status == "MERGED")] | length)}' <<<"$LIST")
        for API in $(jq -r '.[] | select(.status == "OPEN") | .url
                     | capture("/projects/(?<p>[^/]+)/repos/(?<r>[^/]+)/pull-requests/(?<n>[0-9]+)")
                     | "projects/\(.p)/repos/\(.r)/pull-requests/\(.n)"' <<<"$LIST"); do
            KIND=$(curl -s "${CURL_OPTS[@]}" "$STASH_URL/rest/api/1.0/$API" -H "Authorization: Bearer $PASSWORD" \
                | jq -r 'if (.draft == true) or ((.title // "") | ascii_downcase | test("wip|draft")) then "draft"
                         elif any(.reviewers[]?; .approved) then "approved" else "review" end' 2>/dev/null)
            [ -n "$KIND" ] && C=$(jq -c --arg k "$KIND" '.[$k] += 1' <<<"$C")
        done
        PRS=$(jq -c --arg id "$ID" --argjson c "$C" '. + {($id): $c}' <<<"$PRS")
    done
    ISSUES_JSON=$(jq --argjson prs "$PRS" 'map(.prs = $prs[.id])' <<<"$ISSUES_JSON")
    TQA_JSON=$(jq --argjson prs "$PRS" 'map(.prs = $prs[.id])' <<<"$TQA_JSON")

    FIRST_PROJECT=$(echo "$JIRA_PROJECTS" | cut -d, -f1 | tr -d '"' | tr -d ' ')
    BOARD_IDS=$(curl -s "${CURL_OPTS[@]}" \
        "$JIRA_URL/rest/agile/1.0/board?projectKeyOrId=$FIRST_PROJECT&type=scrum&maxResults=50" \
        -H "Authorization: Bearer $JIRA_PASSWORD" \
        | jq -r '[.values[]?.id] | .[]')

    SPRINT_JSON='null'
    ACTIVE_BOARD_ID=""
    for BOARD_ID in $BOARD_IDS; do
        CANDIDATE=$(curl -s "${CURL_OPTS[@]}" \
            "$JIRA_URL/rest/agile/1.0/board/$BOARD_ID/sprint?state=active" \
            -H "Authorization: Bearer $JIRA_PASSWORD" \
            | jq '{name: (.values[0].name // ""), start_date: (.values[0].startDate // ""), end_date: (.values[0].endDate // "")}' \
            2>/dev/null || echo 'null')
        if [ "$(echo "$CANDIDATE" | jq -r '.start_date')" != "" ] && \
           [ "$(echo "$CANDIDATE" | jq -r '.start_date')" != "null" ]; then
            SPRINT_JSON="$CANDIDATE"
            ACTIVE_BOARD_ID="$BOARD_ID"
            break
        fi
    done

    BACKLOG_URL=""
    [ -n "$ACTIVE_BOARD_ID" ] && BACKLOG_URL="${JIRA_URL}/secure/RapidBoard.jspa?rapidView=${ACTIVE_BOARD_ID}&view=planning"

    jq -n \
        --argjson issues "$ISSUES_JSON" \
        --argjson tqa_issues "$TQA_JSON" \
        --argjson next_tasks "$NEXT_JSON" \
        --argjson sprint "$SPRINT_JSON" \
        --arg jira_url "$JIRA_URL" \
        --arg backlog_url "$BACKLOG_URL" \
        --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{issues:$issues, tqa_issues:$tqa_issues, next_tasks:$next_tasks, sprint:$sprint, jira_url:$jira_url, backlog_url:$backlog_url, updated:$updated}' \
        > "$JR_OUT.tmp" && mv "$JR_OUT.tmp" "$JR_OUT"

    log "Jira: $(echo "$MINE_JSON" | jq 'length') mine + $(echo "$IMPL_JSON" | jq 'length') implemented = $(echo "$ISSUES_JSON" | jq 'length') issues, $(echo "$TQA_JSON" | jq 'length') tekn_qa"
}

# ── Master build monitoring (via Jenkins Prometheus metrics) ──────────────────
# MASTER_BUILDS (secrets/config): one entry per line, "Label|<jenkins job path
# after ILT/>". A single fetch of $JENKINS_URL/prometheus/ covers every
# configured repo directly from Jenkins' own last-build state — no Bitbucket
# build-status webhook dependency, no guessing at Bitbucket PROJECT/repoSlug.
poll_jenkins() {
    JK_PREV=$(cat "$JK_OUT" 2>/dev/null || echo '{"pipelines":[]}')
    PROM_RAW=$(curl -s --connect-timeout 5 --max-time 20 --compressed "$JENKINS_URL/prometheus/")

    if [ -z "$PROM_RAW" ]; then
        log "⚠ Jenkins prometheus unreachable — keeping last jenkins.json"
    else
        RESULT_LINES=$(echo "$PROM_RAW"   | grep '^default_jenkins_builds_last_build_result_ordinal{')
        START_LINES=$(echo "$PROM_RAW"    | grep '^default_jenkins_builds_last_build_start_time_milliseconds{')
        DUR_LINES=$(echo "$PROM_RAW"      | grep '^default_jenkins_builds_last_build_duration_milliseconds{')
        RUNNING_LINES=$(echo "$PROM_RAW"  | grep '^default_jenkins_builds_running_build_duration_milliseconds{')

        PIPES='[]'
        while IFS= read -r line; do
            line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
            [ -z "$line" ] && continue
            label="${line%%|*}"; path="${line#*|}"
            label="${label#"${label%%[![:space:]]*}"}"; label="${label%"${label##*[![:space:]]}"}"
            path="${path#"${path%%[![:space:]]*}"}"; path="${path%"${path##*[![:space:]]}"}"
            needle="jenkins_job=\"ILT/$path\","

            ordinal=$(echo "$RESULT_LINES"   | grep -F "$needle" | awk '{print $NF}')
            start=$(echo "$START_LINES"      | grep -F "$needle" | awk '{print $NF}')
            dur=$(echo "$DUR_LINES"          | grep -F "$needle" | awk '{print $NF}')
            building=$(echo "$RUNNING_LINES" | grep -F "$needle")

            # last_build_result_ordinal is Jenkins' own Result.ordinal:
            # 0=SUCCESS 1=UNSTABLE 2=FAILURE 3=NOT_BUILT 4=ABORTED. Strip the
            # ".0" Prometheus suffix before matching on the bare int.
            ordinal_int="${ordinal%%.*}"
            state="UNKNOWN"
            case "$ordinal_int" in
                0) state="SUCCESS" ;;
                1) state="UNSTABLE" ;;
                2) state="FAILURE" ;;
                4) state="ABORTED" ;;
            esac
            # running_build_duration_milliseconds only exists while a build is
            # actively in progress — last_build_result still reflects the
            # *previous* completed build until then, so this takes priority.
            [ -n "$building" ] && state="BUILDING"

            build_url="$JENKINS_URL/job/ILT/job/$(echo "$path" | sed 's#/#/job/#g')/lastBuild/"

            PIPES=$(echo "$PIPES" | jq \
                --arg label "$label" --arg state "$state" --arg url "$build_url" \
                --arg start "${start:-0}" --arg dur "${dur:-0}" \
                '. + [{label:$label, url:$url, state:$state, commit:"", timestamp:($start|tonumber), duration:($dur|tonumber), build_url:$url}]')
        done <<< "$MASTER_BUILDS"

        jq -n --argjson pipelines "$PIPES" --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            '{updated:$updated, pipelines:$pipelines}' > "$JK_OUT.tmp" && mv "$JK_OUT.tmp" "$JK_OUT"

        # macOS desktop notification only on a NEW transition into a red state
        echo "$PIPES" | jq -c '.[]' | while IFS= read -r p; do
            st=$(echo "$p" | jq -r '.state'); lbl=$(echo "$p" | jq -r '.label')
            if [ "$st" = "FAILURE" ]; then
                prev=$(echo "$JK_PREV" | jq -r --arg l "$lbl" '.pipelines[]? | select(.label==$l) | .state' 2>/dev/null)
                if [ "$prev" != "FAILURE" ]; then
                    osascript -e "display notification \"$lbl master build is FAILED\" with title \"⚠ Master build failed\" sound name \"Basso\"" 2>/dev/null || true
                fi
            fi
        done
        log "Master builds: $(echo "$PIPES" | jq 'length') repos, $(echo "$PIPES" | jq '[.[]|select(.state=="FAILURE")]|length') red"
    fi
}

# ── Scheduling ──
# Each source has its own timer that adapts to what is going on:
#   Bitbucket  10 s while any PR build or Sonar analysis runs; otherwise 15 s
#              after a change, doubling each unchanged fetch up to 2 min.
#   Jira       1 min after a change, doubling up to 10 min.
#   Jenkins    20 s while a master build runs, otherwise 5 min.
#   Cloud      every 10 min: backup.sh, then land-cloud.sh (cloud sessions'
#              branches onto master). Not behind the VPN, so it runs first.
# "Changed" = the file differs apart from its updated stamp. Dashboard activity
# (page load, tab focus, clicks — server.py touches data/.poll-wake) makes
# everything due now and resets the back-off, so the loop checks every second.
BB_FAST=15;  BB_BUSY=10; BB_MAX=120
JR_FAST=60;              JR_MAX=600
JK_IDLE=300; JK_BUSY=20
CLOUD_INT=600
SELF="$ROOT/scripts/poll.sh"; SELF_SUM=$(md5 -q "$SELF")
WAKE_FILE="$OUT_DIR/.poll-wake"
STATE_FILE="$OUT_DIR/poll-state.json"

# The schedule, for the dashboard's status bar (server.py pushes it to the page
# as a "schedule" event). Times are epoch seconds; $1 = source being fetched now.
OFFLINE=false
write_state() {
    printf '{"fetching":"%s","offline":%s,"bitbucket":{"interval":%d,"next":%d,"busy":%s},"jira":{"interval":%d,"next":%d},"jenkins":{"interval":%d,"next":%d,"busy":%s}}\n' \
        "${1:-}" "$OFFLINE" \
        "$BB_INT" "$BB_DUE" "$([ "$BB_INT" = "$BB_BUSY" ] && echo true || echo false)" \
        "$JR_INT" "$JR_DUE" \
        "$JK_INT" "$JK_DUE" "$([ "$JK_INT" = "$JK_BUSY" ] && echo true || echo false)" \
        > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}

content_sig() { jq -S 'del(.updated)' "$1" 2>/dev/null | md5; }
mtime()       { stat -f %m "$1" 2>/dev/null || echo 0; }
capped()      { [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

POLL_BUILD_STALE=$(cfg poll_build_stale_seconds); POLL_BUILD_STALE=${POLL_BUILD_STALE:-600}

# POLL_ONCE=1 runs every source once and exits —
# for a manual fetch from the terminal; the dashboard wakes the loop instead.
if [ -n "$POLL_ONCE" ]; then
    vpn_ok || { log "One-shot poll aborted: $STASH_URL unreachable (VPN down?)."; exit 1; }
    poll_bitbucket
    poll_jira
    [ -n "${MASTER_BUILDS//[[:space:]]/}" ] && poll_jenkins
    log "Done (one-shot)."
    exit 0
fi

BB_INT=$BB_FAST; JR_INT=$JR_FAST; JK_INT=$JK_IDLE
BB_DUE=0; JR_DUE=0; JK_DUE=0; CLOUD_DUE=0
WAKE_SEEN=$(mtime "$WAKE_FILE")

while true; do
    NOW=$(date +%s)
    W=$(mtime "$WAKE_FILE")
    if [ "$W" != "$WAKE_SEEN" ]; then
        WAKE_SEEN=$W
        BB_INT=$BB_FAST; JR_INT=$JR_FAST; BB_DUE=0; JR_DUE=0; JK_DUE=0
        log "Dashboard activity — fetching everything now"
    fi

    if [ "$NOW" -ge "$CLOUD_DUE" ]; then
        "$ROOT/scripts/backup.sh"
        "$ROOT/scripts/land-cloud.sh"
        CLOUD_DUE=$(( $(date +%s) + CLOUD_INT ))
        if [ "$(md5 -q "$SELF")" != "$SELF_SUM" ]; then
            log "poll.sh changed — restarting"
            exec /bin/bash "$SELF"
        fi
    fi

    if [ "$NOW" -ge "$BB_DUE" ] || [ "$NOW" -ge "$JR_DUE" ] || [ "$NOW" -ge "$JK_DUE" ]; then
        POLL_BUILD_STALE=$(cfg poll_build_stale_seconds); POLL_BUILD_STALE=${POLL_BUILD_STALE:-600}

        # Fail fast if the VPN/network is down instead of working through every
        # endpoint until each curl times out. Existing data/*.json files stay
        # (last-known-good), so DATA AGE turns red rather than the board erroring.
        if ! vpn_ok; then
            log "⚠ $STASH_URL unreachable (VPN down?) — retrying in ${BB_FAST}s, keeping last data"
            BB_DUE=$((NOW + BB_FAST)); [ "$JR_DUE" -lt "$BB_DUE" ] && JR_DUE=$BB_DUE; [ "$JK_DUE" -lt "$BB_DUE" ] && JK_DUE=$BB_DUE
            OFFLINE=true; write_state
            sleep 1
            continue
        fi
        OFFLINE=false

        if [ "$NOW" -ge "$BB_DUE" ]; then
            write_state bitbucket
            BEFORE=$(content_sig "$BB_OUT")
            poll_bitbucket
            if jq -e '[.my_prs[]?, .reviewer_prs[]?] | any(.build_state == "INPROGRESS" or .sonar_building == true)' "$BB_OUT" >/dev/null 2>&1; then
                BB_INT=$BB_BUSY
            elif [ "$(content_sig "$BB_OUT")" != "$BEFORE" ]; then
                BB_INT=$BB_FAST
            else
                BB_INT=$(capped $((BB_INT * 2)) $BB_MAX)
            fi
            BB_DUE=$(( $(date +%s) + BB_INT ))
            log "Bitbucket: next in ${BB_INT}s"
        fi

        if [ "$NOW" -ge "$JR_DUE" ]; then
            write_state jira
            BEFORE=$(content_sig "$JR_OUT")
            poll_jira
            if [ "$(content_sig "$JR_OUT")" != "$BEFORE" ]; then
                JR_INT=$JR_FAST
            else
                JR_INT=$(capped $((JR_INT * 2)) $JR_MAX)
            fi
            JR_DUE=$(( $(date +%s) + JR_INT ))
            log "Jira: next in ${JR_INT}s"
        fi

        if [ "$NOW" -ge "$JK_DUE" ] && [ -n "${MASTER_BUILDS//[[:space:]]/}" ]; then
            write_state jenkins
            poll_jenkins
            if jq -e 'any(.pipelines[]?; .state == "BUILDING")' "$JK_OUT" >/dev/null 2>&1; then
                JK_INT=$JK_BUSY
            else
                JK_INT=$JK_IDLE
            fi
            JK_DUE=$(( $(date +%s) + JK_INT ))
            log "Jenkins: next in ${JK_INT}s"
        fi
        write_state
    fi
    sleep 1
done

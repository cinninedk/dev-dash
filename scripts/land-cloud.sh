#!/bin/bash
# land-cloud.sh — brings work from cloud Claude Code sessions onto master.
# Run by poll.sh every 10 minutes.
#
# Cloud sessions push to their own claude/… branch, authored by Claude and
# with Co-Authored-By/Claude-Session trailers. Each new commit on such a branch
# is cherry-picked onto master with the local git identity as author and those
# lines removed, master is pushed, and the branch is deleted from GitHub.
# A commit already on master (same patch) is not applied twice, so a session
# that pushes again later only lands its new commits.
#
# Never touches a working copy someone is using: skips when not on master,
# with uncommitted changes, or with local commits that aren't pushed yet.
# A branch that doesn't apply cleanly is left on GitHub to sort out by hand.
#
# Restarts take care of themselves: server.py re-execs when it changes, the
# dashboard page reloads when a page file changes, and poll.sh re-execs after
# this script when poll.sh itself changed.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
log() { echo "[$(date '+%H:%M:%S')] cloud: $*"; }

# Under launchd, macOS privacy protection blocks ~/.ssh (key, config,
# known_hosts) and ~/Documents (where ~/.gitconfig may point). So ssh gets a
# deploy key from secrets/ and GitHub's published host key, and git skips a
# global config it can't read — the repo's own config must then hold
# user.name/user.email.
KEY="$ROOT/secrets/github-deploy-key"
[ -r "$KEY" ] || { log "no secrets/github-deploy-key, skipping (README → Cloud sessions)"; exit 0; }
GITHUB_HOSTKEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -F /dev/null -i '$KEY' -o IdentitiesOnly=yes -o HostName=github.com -o HostKeyAlias=github.com -o User=git \
    -o UserKnownHostsFile=/dev/null -o GlobalKnownHostsFile=/dev/null -o 'KnownHostsCommand=/bin/echo $GITHUB_HOSTKEY' \
    -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=2"
git config --global --list >/dev/null 2>&1 || export GIT_CONFIG_GLOBAL=/dev/null
[ -n "$(git config user.email)" ] || { log "no git user.email readable, skipping (git config user.email … in the repo)"; exit 0; }

head=$(git symbolic-ref --short HEAD 2>&1)
[ "$head" = master ] || { log "not on master ($head), skipping"; exit 0; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { log "uncommitted changes, skipping"; exit 0; }
git fetch -q --prune origin || { log "fetch failed"; exit 0; }

# master pushed from somewhere else: catch up first
if ! git merge-base --is-ancestor origin/master HEAD; then
    git merge -q --ff-only origin/master || { log "master has diverged from GitHub, skipping"; exit 0; }
    log "pulled master → $(git rev-parse --short HEAD)"
fi
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)" ] || { log "local commits not pushed yet, skipping"; exit 0; }

# drop the lines that credit Claude; git's whitespace cleanup trims what's left
scrub() {
    grep -v -i -E '^(co-authored-by|claude-session|generated-by|assisted-by):|generated with \[?claude|claude\.(ai|com)/|noreply@anthropic\.com'
}

for branch in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/claude/); do
    name=${branch#origin/}
    start=$(git rev-parse HEAD)
    picks=$(git cherry HEAD "$branch" | sed -n 's/^+ //p')   # oldest first, not yet on master
    ok=true
    for c in $picks; do
        [ "$ok" = true ] || break
        [ "$(git rev-parse HEAD)" = "$start" ] && "$ROOT/scripts/backup.sh" "before-${name#claude/}"
        before=$(git rev-parse HEAD)
        if git cherry-pick --empty=drop "$c" >/dev/null 2>&1; then
            # an emptied commit is dropped and HEAD stays — don't amend the previous one
            [ "$(git rev-parse HEAD)" = "$before" ] ||
                git log -1 --format=%B | scrub | git commit -q --amend --reset-author -F - || ok=false
        else
            git cherry-pick --abort 2>/dev/null
            ok=false
        fi
    done
    if [ "$ok" != true ]; then
        git reset -q --hard "$start"
        log "$name doesn't apply cleanly on master, left on GitHub"
        continue
    fi
    if [ -n "$picks" ]; then
        if ! /usr/bin/python3 -m py_compile server.py; then
            git reset -q --hard "$start"
            log "$name breaks server.py, left on GitHub"
            continue
        fi
        if ! git push -q origin master; then
            git reset -q --hard "$start"
            log "push failed, retrying $name next round"
            continue
        fi
        log "landed $name: $(git log --format='%h %s' "$start..HEAD" | paste -sd ';' -)"
    fi
    git push -q origin --delete "$name" && log "deleted $name from GitHub"
done

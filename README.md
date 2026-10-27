# DEV-DASHBOARD

A local developer dashboard: your Bitbucket pull requests, Jira sprint issues,
Jenkins master builds, SonarQube quality and GitHub Copilot usage in one
browser view, plus a time tracker, timesheet and salary-hours ledger. Runs
entirely on your machine — no cloud service, no telemetry.

Shell pollers write `data/*.json`; `server.py` serves the pages and those files
at `http://localhost:666`, and does anything live through `/api/*` endpoints so
the tokens never reach the browser. No frameworks, no build step.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| `bash`, `curl`, `jq` | `scripts/poll.sh` data fetcher |
| `python3` (3.9+) | `server.py` and the helper scripts |
| macOS `launchd` | auto-start and keep-alive |
| `afplay` (macOS) | build sounds |

```sh
brew install jq
```

---

## Setup

### 1 — Secrets

`scripts/setup.sh` creates `secrets/` (gitignored) with placeholder files:

```
secrets/
├── config           # URLs, project keys, JQL, optional page settings
├── bitbucket-token  # Bitbucket personal access token
├── jira-token       # Jira personal access token
├── sonar-token      # SonarQube user token
└── copilot-token    # GitHub token for the Copilot usage poll (add by hand)
```

Tokens are plain text files. Generate them under Profile → Personal access
tokens in each web UI.

**`secrets/config`:**
```bash
STASH_URL="https://your-bitbucket-server"
JIRA_URL="https://your-jira-server"
JIRA_PROJECTS="PROJ1,PROJ2"
LAUNCHD_PREFIX="com.yourname.jiraffe"   # launchd agent labels

# Kanban queries (exact Jira status names, mixed case):
JQL_MINE="…"          # issues assigned to me (Open / Implement / QA / BV)
JQL_IMPL="…"          # issues I moved out of Implement this sprint
JQL_TQA="…"           # sprint issues labelled teknisk_QA
JQL_NEXT_CURRENT="…"  # NEXT TASK candidates in the current sprint
JQL_NEXT_FUTURE="…"   # NEXT TASK candidates in the next sprint

# Optional: SonarQube, for PRs whose Sonar report in Bitbucket carries no link
SONAR_URL="https://your-sonarqube-server"
# Optional: which issues backlog.html lists (no sprint clause, no ORDER BY).
# Default: open, unassigned-or-mine, not an Epic.
JQL_BACKLOG="project in ($JIRA_PROJECTS) AND statusCategory != Done AND (assignee is EMPTY OR assignee = currentUser()) AND issuetype != Epic"
# Optional: master builds for jenkins.html, one "Label|<job path>" per line
# (path below Jenkins' ILT/ folder, which poll.sh assumes)
JENKINS_URL="https://your-jenkins"
MASTER_BUILDS="
my-app|my-app/master
"
# Optional: apps versions.html shows, one "Label|PROJECT/repoSlug|base URL" per
# line, {env} standing in for lt/ft/et/prep/prod. Each environment's Spring Boot
# /actuator/info and /actuator/health are read live, plus master's newest tag.
VERSION_APPS="
my-app|PROJ/my-app|https://{env}.my-app.example.com
"
# Optional: links.html, one "Label|URL" per line. Zoom join links
# (https://…zoom.us/j/…) open straight in the Zoom app.
LINKS="
Standup|https://example.zoom.us/j/123456789?pwd=…
Support|https://your-jira-server/secure/Dashboard.jspa
"
# Optional: the org's monthly Copilot credit pool. copilot.html then shows each
# user's share of it.
COPILOT_POOL=57000
```

### 2 — Start

Three launchd agents live in `~/Library/LaunchAgents/<LAUNCHD_PREFIX>-*.plist`
(written by hand, not by `setup.sh`):

| Agent | Runs |
|---|---|
| `-server` | `server.py` (KeepAlive; restarts itself when server.py changes) |
| `-poll` | `scripts/poll.sh` (loops with its own timers) |
| `-copilot` | `scripts/poll-copilot.sh` (every 4 h) |

```sh
scripts/start.sh    # load the agents
scripts/stop.sh     # unload them
scripts/status.sh   # agent status + last poll log
```

`POLL_ONCE=1 scripts/poll.sh` fetches every source once, from the terminal.

### Test mode

`scripts/start-test.sh` serves made-up data on port 664, next to the real
dashboard. Each start rewrites `data-test/` with `scripts/make-test-data.py`
(invented names, dated around today), so test-mode edits don't survive a
restart. The `/api/*` endpoints that call Jira, Bitbucket or Sonar still need
`secrets/`.

### Cloud sessions

Claude Code sessions in the cloud push their work to a `claude/…` branch.
Every 10 minutes `poll.sh` runs `scripts/land-cloud.sh`, which cherry-picks each
new commit on those branches onto master with the local git identity as
author and the Claude attribution lines removed, pushes master and deletes
the branch. It skips a working copy that isn't on master, has uncommitted
changes or unpushed commits; a branch that doesn't apply cleanly stays on
GitHub. Nothing needs a manual restart: `server.py` re-execs on change, the
dashboard tab reloads when a page file changes, and `poll.sh` re-execs when
it changed itself. Watch it with `grep cloud: /tmp/dashboard-poll.log`.

macOS doesn't let a launchd agent read `~/.ssh`, so the script uses its own
deploy key, and the repo's git config needs your name and email (a
`~/.gitconfig` that links into `~/Documents` is unreadable too). One-time setup:

```sh
ssh-keygen -t ed25519 -N "" -C "jiraffe land-cloud.sh" -f secrets/github-deploy-key
gh repo deploy-key add secrets/github-deploy-key.pub --allow-write --title "jiraffe land-cloud"
git config user.name "$(git config --global user.name)"
git config user.email "$(git config --global user.email)"
```

### Backup

`scripts/backup.sh` (also run by `poll.sh`) copies the files that can't be
re-fetched — `timetrack.json`, `work-hours.json`, `jira-worklog.json`,
`copilot-history.json`, the Toggl exports, `secrets/config` and
`secrets/Arbejdstimer` — to `iCloud Drive/jiraffe-backup/<date>/` once a day,
and to a tagged folder before cloud commits are applied. Folders older than
30 days are removed. Restore by copying a file back.

---

## Configuration

`config.yaml`:

```yaml
port: 666                      # HTTP server port (restart the server to change)
poll_build_stale_seconds: 240  # re-fetch Jenkins/Sonar even if the commit is unchanged
```

---

## Pages

| Page | What it shows |
|---|---|
| `index.html` | Main board: my PRs, Jira kanban, time-tracking box, PRs and tech-QA for my review |
| `backlog.html` | Jira backlog by sprint and epic, expandable issue detail |
| `jenkins.html` | Master-branch builds (`MASTER_BUILDS`); failing ones flash |
| `versions.html` | Deployed version and health per app per environment vs master's newest tag |
| `copilot.html` | Org Copilot credit usage this month + my long-term history |
| `time.html` | Timesheet from the tracker; writes worklogs to Jira |
| `worklog.html` | Salary hours: worked + leave vs the norm per month/year |
| `leave.html` | Vacation, sickness, child's sick days and other leave |
| `links.html` | Personal quick links (`LINKS`) |

Every page has a theme switcher (amber, C64, green, ice, paper, corkboard).

---

## Main board (index.html)

### Layout

```
┌─ header: nav · theme · NEXT TASK · ⟳ REFRESH ─────────────────────┐
├─ stats: My PRs │ For Review │ Jira Active │ Builds Failing ─────────┤
├─ MY PULL REQUESTS ──────────────────────────────────────────────────┤
│  #id  repo  title          ✓appr/total  BUILD  QG  CONFLICT         │
│       commit  bugs smells vulns hots  N cmt                          │
├─ JIRA KANBAN ─────────────────────────────────────────┬─ TRACKING ──┤
│  OPEN │ IMPLEMENT │ QA │ BV │ RESOLVED                │  issue       │
│  — or, with nothing in Implement: —                   │  elapsed     │
│  NEXT TASK (2×wide) │ OPEN │ QA │ BV │ RESOLVED       │Idle Pause End│
├─ FOR MY REVIEW ───────────────────────────────────────┴──────────────┤
│  TECH QA — NEEDS MY REVIEW  ·  PRs — NEEDS MY REVIEW                 │
├─ status bar: session · last poll · data age · sprint · sound · POLL ─┤
```

Empty kanban columns are hidden and the rest expand to fill the width; each
column scrolls on its own.

The workday runs from its first tracked entry to **End day**. Inside it,
nothing running means forgotten tracking: the TRACKING box breathes red
NOT TRACKING, and the timesheet lists the stretch as a gap. **Pause** (doctor,
errands) breathes yellow instead and never counts as worked time or a gap.
Starting again after End day reopens the day; the time off becomes a pause.
Pauses and day ends live in `timetrack.json` under `days`, outside `trackers`.

### Kanban columns

| Column | What appears here |
|--------|------------------|
| OPEN | Issues assigned to you in Open or Reopened |
| IMPLEMENT | Issues assigned to you in Implement |
| NEXT TASK | Replaces IMPLEMENT when you have nothing in Implement (or on demand, via the NEXT TASK button): unassigned Open/Reopened issues from `JQL_NEXT_CURRENT`, then the next sprint's (`JQL_NEXT_FUTURE`) dimmed. Double width. |
| QA | Issues you implemented, now in Quality Assurance |
| BV | Issues you implemented, now in Business Validation |
| RESOLVED | Issues you moved out of Implement this sprint, now Resolved |

`teknisk_QA` issues never sit in the kanban: unassigned ones (or yours) that you
didn't implement show under **FOR MY REVIEW → TECH QA**.

Cards carry badges for: linked-PR state (in review / approved / all merged /
draft), unseen activity by others (new comments, status changes, edits — click
to mark seen), NOT IN SPRINT, blockers, and the time-tracker button.

### PR rows

```
#id  repo  title         DRAFT ✓approved/total NEEDS WORK T:tasks  BUILD  QG  CONFLICT
     commithash  bugs:N  smells:N  vulns:N  hots:N  N cmt
```

- BUILD links to Jenkins, QG to SonarQube; a failing gate gets an **issues**
  button listing the Sonar issues.
- Green = passing / approved, red = failed / bugs or vulnerabilities, yellow =
  building / smells or hotspots.
- My PRs are sorted by latest activity. For Review leaves out renovate bots,
  drafts, WIP titles, my own PRs and PRs anyone has already approved.

### Refreshing

The page never polls remote systems itself. Loading it, switching back to its
tab, **⟳ REFRESH** and clicking around (at most once a minute) wake poll.sh,
which then fetches every source at once. The page reloads whenever a data file
changes (server.py pushes it over `/api/data-events`).

Status bar: LAST POLL is when poll.sh last wrote Bitbucket data, DATA AGE turns
red after 2 minutes (poller stopped or VPN down), POLL shows each source's next
fetch and interval. Hover any item for an explanation.

### Build sounds

New outcomes on my PRs and master builds play a sound — build red, quality
gate failed, all green, approved, gate passed, build green — one per reload,
worst news first. The page renders the sound and POSTs it to
`/api/play-sound`; server.py plays it on the Mac with `afplay` (browsers won't
play audio on a page nobody clicked).

---

## How polling works

`scripts/poll.sh` runs one timer per source, each adapting to what is going on:

| Source | Busy | Quiet |
|---|---|---|
| Bitbucket | 10 s while a PR build or Sonar analysis runs | 15 s after a change, doubling up to 2 min |
| Jira | — | 1 min after a change, doubling up to 10 min |
| Jenkins master builds | 20 s while one builds | 5 min |
| Backup + cloud branches | — | 10 min (not behind the VPN, so it runs first) |

Dashboard activity (`POST /api/wake` touches `data/.poll-wake`) makes all three
due at once and resets the back-off. If Bitbucket is unreachable the cycle is
skipped and the last data stays. What each source fetches:

1. **Bitbucket** — my open PRs (author + reviewer). Build status and Sonar data
   per PR are only re-fetched when the commit changed, the build was
   `INPROGRESS`, or the cache is older than `poll_build_stale_seconds`
   (`data/.build-cache.json`). A failed request keeps the last known values.
2. **Jira** — the `JQL_*` queries above, linked-PR state per card (Jira
   dev-status + Bitbucket), recent activity by others, and the active sprint.
3. **Jenkins** — `$JENKINS_URL/prometheus/` once, for every `MASTER_BUILDS` entry.

Writes `data/bitbucket.json`, `jira.json`, `jenkins.json` and `poll-state.json`
(atomically, via `.tmp` + `mv`).

Copilot usage is polled separately by `scripts/poll-copilot.sh`, which also
folds the leaderboard's snapshot archive into `data/copilot-history.json` —
the API only serves 28 days, so that file is the only long-term record.

---

## Files

```
jiraffe/
├── server.py          # HTTP server + /api/* endpoints (Python 3.9)
├── config.yaml        # port, poll_build_stale_seconds
├── *.html             # one file per page (see Pages)
├── theme.css/.js      # shared themes + switcher
├── sounds.js          # build sounds
├── scripts/
│   ├── poll.sh            # Bitbucket, Jira, Jenkins poller
│   ├── poll-copilot.sh    # Copilot usage poller
│   ├── parse-copilot.py   # leaderboard output -> data/copilot.json
│   ├── copilot-history.py # leaderboard archive -> data/copilot-history.json
│   ├── export-worklog.py  # every worked day -> data/worklog-export.csv
│   ├── check-norm.mjs     # self-check of worklog.html's norm/leave rules
│   ├── make-test-data.py  # made-up data-test/ for test mode
│   ├── land-cloud.sh      # cloud sessions' branches -> master
│   ├── backup.sh          # precious files -> iCloud Drive
│   ├── jira-issue.sh      # dump an issue as text
│   ├── pr-comments.sh     # dump a PR's unresolved comment threads
│   ├── sprint-worked-on.sh, next-tasks.sh
│   └── setup.sh, start.sh, stop.sh, status.sh, start-test.sh
├── data-test/         # gitignored — written by make-test-data.py
├── secrets/           # gitignored
└── data/              # gitignored — written by the pollers and server.py
```

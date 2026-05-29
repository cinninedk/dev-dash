# DEV-DASHBOARD

A local developer dashboard that surfaces your open Bitbucket pull-requests and
Jira sprint issues in one view. Runs entirely on your machine — no cloud service,
no telemetry. Two frontends share the same JSON data files:

- **GUI** — browser page at `http://localhost:666`
- **TUI** — terminal interface (`tui.py`)

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| `bash` | `poll.sh` data fetcher |
| `curl` | API calls to Bitbucket / Jira / SonarQube |
| `jq` | JSON processing in `poll.sh` |
| `python3` (3.10+) | TUI and local HTTP server |
| macOS `launchd` | Auto-start and keep-alive |

```sh
brew install jq
```

---

## Setup

### 1 — Secrets

Create the `secrets/` folder (gitignored):

```
secrets/
├── config           # URLs, username, Jira project keys
├── bitbucket-token  # Bitbucket personal access token (plain text)
├── jira-token       # Jira personal access token (plain text)
└── sonar-token      # SonarQube personal access token (plain text)
```

**`secrets/config`:**
```bash
STASH_URL="https://your-bitbucket-server"
JIRA_URL="https://your-jira-server"
USERNAME="your-bitbucket-username"
JIRA_PROJECTS="PROJ1,PROJ2"
```

Tokens are plain text files with no trailing newline. Generate them from the
respective web UIs under Profile → Personal access tokens.

### 2 — Install and start

```sh
./setup.sh   # creates launchd plists in ~/Library/LaunchAgents/
./start.sh   # loads both agents (HTTP server + poller)
```

`setup.sh` is safe to re-run — it skips files that already exist.

To stop:
```sh
./stop.sh
```

To restart (e.g. after config changes):
```sh
./stop.sh && ./setup.sh && ./start.sh
```

To check service status and last poll output:
```sh
./status.sh
```

### 3 — First data

The poller starts automatically. To fetch immediately without waiting:
```sh
./poll.sh
```

---

## Configuration

`config.yaml` controls all tunable settings. Changes take effect on the next
poll cycle without restarting.

```yaml
port: 666                      # HTTP server port

poll_active_seconds: 15        # how often poll.sh loops
poll_fetch_buffer_seconds: 2   # browser fetches this many seconds after next_poll

poll_build_stale_seconds: 600  # re-fetch Jenkins/Sonar even if commit unchanged

tui_refresh_seconds: 15        # how often the TUI re-reads data files

work_hours_enabled: false      # set true to pause TUI outside work hours
work_start_hour: 8
work_end_hour: 18
```

---

## GUI (browser)

Open **http://localhost:666**.

### Layout

```
┌─ header: title · poll status · clock · ⟳ REFRESH ─────────────────┐
├─ stats: My PRs │ For Review │ Jira Active │ Builds Failing ─────────┤
├─ MY PULL REQUESTS ──────────────────────────┬─ ACTIVITY FEED ───────┤
│  #id  repo  title            ✓appr/total    │  Jira quick stats      │
│       commit  bugs smells vulns hots  N cmt │                        │
├─ JIRA KANBAN ───────────────────────────────┤                        │
│  [visible columns only — empty ones hidden] │                        │
│  OPEN │ IMPLEMENT │ QA │ BV │ RESOLVED      │                        │
│  — or, when nothing assigned to you: —      │                        │
│  NEXT TASK (2×wide) │ QA │ BV │ RESOLVED   │                        │
├─ PRs FOR MY REVIEW ─────────────────────────┴────────────────────────┤
│  (collapses to title bar when empty)                                  │
└────────────────────────────────────────────────────────────────────┘
```

All kanban columns scroll independently. Empty columns are hidden automatically.

### Kanban columns

| Column | What appears here |
|--------|------------------|
| OPEN | Issues assigned to you in Open or Reopened status |
| IMPLEMENT | Issues assigned to you in Implement status |
| NEXT TASK | Shown instead of OPEN+IMPLEMENT when you have nothing assigned — unassigned or your sprint Stories not yet in QA/BV/Resolved. Double width. |
| QA | All sprint issues with label `teknisk_QA` (yours highlighted in green) + QA issues you implemented |
| BV | Business Validation issues you implemented |
| RESOLVED | Issues you moved out of Implement this sprint |

Empty columns are hidden and the remaining ones expand to fill the width.

Issues with the `teknisk_QA` label are shown at the top of the QA column with a
blue `TEKN·QA` badge. Those assigned to you have a green issue key. They are
excluded from the Resolved column.

### PR rows

Each PR renders as two lines:

```
#id  repo  title                     ✓approved/total  T:tasks  [BUILD]  [QG]
     commithash  bugs:N  smells:N  vulns:N  hots:N  N cmt  [CONFLICT]
```

**Colours:**
- Green — build passing / quality gate passed / all reviewers approved
- Red — build failed / bugs or vulnerabilities present
- Yellow — build in progress / code smells or hotspots
- `CONFLICT` — merge conflict detected
- `NEEDS WORK` — at least one reviewer requested changes

### Refreshing

The browser auto-refreshes roughly 15 seconds after each poll completes — it
reads the `next_poll` timestamp written into `bitbucket.json` and schedules the
fetch for that moment. Hit **⟳ REFRESH** or **Cmd+R** for an immediate reload.

---

## TUI (terminal)

```sh
python3 tui.py
```

Requires Python 3.10+.

### Layout

```
◆ DEV-DASHBOARD  UP 00:12:34  NEXT 12s              Thu 28/05  09:41:22
────────────────────────────────────────────────────────────────────────
MY PRs: 2  FOR REVIEW: 3  JIRA ACTIVE: 7  BUILDS FAILING: 0
────────────────────────────────────────────────────────────────────────
── MY PULL REQUESTS
#704  stilloginportal  [Casper Nielsen]  feat/STILLOGIN-704…   APPROVED  T:0  SUCCESSFUL  QG PASSED
      a1b2c3d  bugs:0  smells:2  vulns:0  hots:0  1 cmt
── JIRA KANBAN
 OPEN (0) │ IMPLEMENT (1) │ QA (2) │ BV (0) │ RESOLVED (3)
```

### Key bindings

| Key | Action |
|-----|--------|
| `R` | Reload data from disk immediately |
| `Q` / `Ctrl-C` | Quit |

**Mouse:** click any underlined issue key or PR link to open it in the browser.

The TUI re-reads the data files every `tui_refresh_seconds` (default 15).
If `work_hours_enabled: true`, polling pauses outside the configured hours.

---

## How it works

`poll.sh` runs in a continuous loop, sleeping `poll_active_seconds` between
iterations. Each iteration:

1. **Bitbucket** — fetches your open PRs (author + reviewer roles) — 2 curls
2. **Jenkins / SonarQube** — fetches build status and code quality per PR, but
   only when the PR's commit hash changed, the last known build was `INPROGRESS`,
   or the cached data is older than `poll_build_stale_seconds`. Results are cached
   in `data/.build-cache.json`. Closed PRs are evicted from the cache automatically.
3. **Jira** — four queries every iteration:
   - Issues currently assigned to you (Open / Implement / QA / BV)
   - Issues you moved out of Implement this sprint (now in QA / BV / Resolved)
   - All sprint QA issues with label `teknisk_QA` (any assignee)
   - Next task candidates: sprint Stories not in QA/BV/Resolved, unassigned or yours (shown when you have nothing in OPEN/IMPLEMENT)
4. Writes `data/bitbucket.json` and `data/jira.json`

The browser and TUI only read those files — they never call the APIs directly.

---

## Files

```
dashboard/
├── poll.sh               # data fetcher
├── index.html            # browser GUI
├── tui.py                # terminal TUI
├── config.yaml           # tunable settings
├── setup.sh              # install launchd plists
├── start.sh              # load launchd agents
├── stop.sh               # unload launchd agents
├── status.sh             # service status + last poll log
├── sprint-worked-on.sh   # list issues you moved through Implement this sprint
├── secrets/              # gitignored
│   ├── config
│   ├── bitbucket-token
│   ├── jira-token
│   └── sonar-token
└── data/                 # gitignored — written by poll.sh
    ├── bitbucket.json
    ├── jira.json
    └── .build-cache.json
```

# DEV-DASHBOARD

A local developer dashboard that surfaces your open Bitbucket pull-requests and
Jira sprint issues in one view. Runs entirely on your machine — no cloud service,
no telemetry. Two frontends share the same data files:

- **GUI** — a browser page served by a local HTTP server
- **TUI** — a terminal interface using Python curses

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| `bash` | `poll.sh` data fetcher |
| `curl` | API calls to Bitbucket / Jira |
| `jq`   | JSON processing in `poll.sh` |
| `python3` | TUI and local HTTP server |
| macOS `launchd` | Auto-start and scheduled polling |

Install `jq` if needed:
```sh
brew install jq
```

---

## Setup

### 1 — Create your secrets

```
dashboard/
└── secrets/
    ├── config             # URLs, username, project keys
    ├── bitbucket-token    # Bitbucket personal access token
    └── jira-token         # Jira personal access token
```

**`secrets/config`** — copy and fill in your values:
```bash
STASH_URL="https://your-bitbucket-server"
JIRA_URL="https://your-jira-server"
USERNAME="your-username"
JIRA_PROJECTS="PROJ1,PROJ2"
```

**`secrets/bitbucket-token`** — plain text, no newline:
```
your-bitbucket-pat-here
```
Generate at: Bitbucket → Profile → Personal access tokens

**`secrets/jira-token`** — plain text, no newline:
```
your-jira-pat-here
```
Generate at: Jira → Profile → Personal access tokens

### 2 — Install the launchd plists

Copy the two plist files into `~/Library/LaunchAgents/`:
```sh
cp launchd/com.yourname.dashboard-server.plist ~/Library/LaunchAgents/
cp launchd/com.yourname.dashboard-poll.plist   ~/Library/LaunchAgents/
```
Edit both files to replace the `WorkingDirectory` path with your dashboard folder.

### 3 — Start everything

```sh
./setup.sh
```

This loads both launchd agents:
- **HTTP server** — runs `python3 -m http.server 666`, kept alive automatically
- **Poller** — runs `poll.sh` every 60 seconds

To stop:
```sh
./stop.sh
```

To check service status and last poll output:
```sh
./status.sh
```

### 4 — First data fetch

Run the poller once manually to get immediate data:
```sh
./poll.sh
```

This writes `data/bitbucket.json` and `data/jira.json`.

---

## GUI (browser)

Open **http://localhost:666** in your browser.

### Layout

```
┌─ header: title · last poll time · clock · refresh button ─────────┐
├─ stats: My PRs │ For Review │ Jira Active │ Builds Failing ─────────┤
├─ MY PULL REQUESTS ─────────────────────────┬─ ACTIVITY FEED ────────┤
│  #id  repo  title               ✓X/Y  T:N  │  Jira quick stats      │
│       commit  bugs smells vulns hots  N cmt │  Build summary         │
├─ JIRA KANBAN ──────────────────────────────┤  Data source           │
│  IMPLEMENT │ REVIEW │ QA │ BV │ RESOLVED   │                        │
├─ PRs FOR MY REVIEW ────────────────────────┴────────────────────────┤
│  #id  repo  author  title          ✓X/Y  T:N  [BUILD]  [QG]        │
│       commit  bugs smells vulns hots  N cmt  CONFLICT               │
└────────────────────────────────────────────────────────────────────┘
```

### Kanban columns

| Column | What appears here |
|--------|------------------|
| IMPLEMENT | Issues assigned to you in Implement status, no open PR |
| REVIEW | Implement issues where a matching PR exists (branch name contains the Jira key) |
| QA | Quality Assurance issues you worked on this sprint |
| BV | Business Validation issues you worked on this sprint |
| RESOLVED | Resolved issues from the current sprint |

### PR rows

Each PR shows two lines:

1. `#id  repo  title  ✓approved/total  T:open-tasks  [BUILD]  [QG]  CONFLICT`
2. `commithash  bugs:N  smells:N  vulns:N  hotspots:N  N cmt`

**Colour coding:**
- Green — build passing, approvals received
- Red — build failed, bugs or vulnerabilities present
- Yellow — build in progress, code smells or hotspots
- `CONFLICT` badge — merge conflict detected

### Refreshing

The page auto-refreshes every 60 seconds during work hours (08:00–18:00 Mon–Fri).
Hit the **⟳ REFRESH** button or press **Cmd+R** for an immediate refresh.

---

## TUI (terminal)

```sh
python3 tui.py
```

Requires Python 3.10+ (uses `match` statements).

### Layout

```
◆ DEV-DASHBOARD  UP 00:12:34  NEXT  45s                   Mon 26/05  09:41:22
────────────────────────────────────────────────────────────────────────────────
MY PRs: 2   FOR REVIEW: 3   JIRA ACTIVE: 7   BUILDS FAILING: 0
────────────────────────────────────────────────────────────────────────────────
── MY PULL REQUESTS
#704  stilloginportal  feat/STILLOGIN-704 – Add login flow    ✓2/4  T:0  [SUCC] [QG✓]
      a1b2c3d  bugs:0  smells:2  vulns:0  hots:0  1 cmt
────────────────────────────────────────────────────────────────────────────────
── JIRA KANBAN
 IMPLEMENT (1)  │  REVIEW (1)   │  QA (2)  │  BV (0)  │  RESOLVED (3)
────────────────
 STILLOGIN-700  │  STILLOGIN-704│  ...
 Fix token exp  │  Add login    │
 Story ─ Medium │  Story ▲ High │
```

### Key bindings

| Key | Action |
|-----|--------|
| `R` | Reload data immediately from disk |
| `Q` or `Ctrl-C` | Quit |

Auto-refreshes every 60 seconds during work hours (08:00–18:00 Mon–Fri).
Outside work hours the TUI stays open but stops polling.

---

## Customising Jira statuses

The Jira status names used in JQL and the kanban column filters are currently
defined in three places. If your Jira workflow uses different names (e.g.
`In Progress` instead of `Implement`), update all three:

| File | What to change |
|------|---------------|
| `poll.sh` line ~157 | `status in (...)` inside the JQL string, and the `status CHANGED FROM` clause |
| `index.html` | Status strings in `renderAll()`: `'IMPLEMENT'`, `'QUALITY ASSURANCE'`, etc. |
| `tui.py` | Status strings in `render_kanban()`: same values |

Note: the JQL uses the *exact Jira status name* (e.g. `"Quality Assurance"`),
while the JSON and UI compare against the uppercased version (e.g. `"QUALITY ASSURANCE"`).

---

## Files

```
dashboard/
├── poll.sh               # data fetcher — run manually or via launchd
├── index.html            # browser GUI
├── tui.py                # terminal TUI
├── setup.sh              # load launchd agents
├── stop.sh               # unload launchd agents
├── status.sh             # show service status + last poll log
├── secrets/              # gitignored — credentials go here
│   ├── config
│   ├── bitbucket-token
│   └── jira-token
└── data/                 # gitignored — written by poll.sh
    ├── bitbucket.json
    └── jira.json
```

---

## How it works

`poll.sh` runs every 60 seconds via launchd. It:
1. Fetches your open PRs (author + reviewer roles) from the Bitbucket dashboard API
2. For each PR, fetches build status and SonarQube metrics in two API calls
3. Fetches Jira issues matching your sprint, projects, and workflow statuses
4. Writes `data/bitbucket.json` and `data/jira.json`

The browser GUI and terminal TUI both read those two files — no direct API access
from the frontend. Pressing refresh reloads the files from disk; it does not
re-poll the APIs (that only happens via `poll.sh`).

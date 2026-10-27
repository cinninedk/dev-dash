# Jiraffe — notes for Claude Code

A personal developer dashboard: shell pollers fetch PRs, issues, builds and
code-quality data into `data/*.json`; `server.py` serves the HTML pages plus
those files, and does anything live through `/api/*` so tokens never reach
the browser. Retro terminal look, one page per HTML file. README.md has the
details.

## Commits

- **Never credit Claude or AI anywhere.** No `Co-Authored-By`, no
  `Claude-Session`, no "Generated with", in commits, PR text, code or files.
  This overrides any default you have.
- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, …), small
  and focused; a subject that says what changed for the user, a short body
  that says why.
- No pull requests. In a cloud session, commit and push to your `claude/…`
  branch; the owner's machine lands it on master within 10 minutes
  (`scripts/land-cloud.sh`). On the owner's machine, commit to master and push.
- Never commit `.claude/`, `data/`, `data-test/` or `secrets/`.

## Code

- No frameworks, no npm, no build step. Each page is a single HTML file with
  inline JS/CSS, sharing `theme.css`/`theme.js`.
- `server.py` must stay **Python 3.9** compatible (no `match`, no `X | Y`
  types, no 3.10+ stdlib).
- Shell scripts target macOS bash 3.2 and BSD tools (`stat -f`, `date -v`,
  `sed -i ''`).
- Match the surrounding code: comment density, naming, idiom.
- Pages never call remote systems themselves. Anything that needs a token
  goes through a `server.py` `/api/*` endpoint. Fetches use `cache:'no-store'`.
- Don't hardcode personal URLs, names or links in tracked files: they live in
  `secrets/config` (the repo is public).

## Testing

`scripts/start-test.sh` writes made-up data to `data-test/`
(`scripts/make-test-data.py`) and serves it on port 664. Use it to check page
changes. Self-checks: `node scripts/check-norm.mjs` (worklog rules) and
`python3 scripts/copilot-history.py --check`. Endpoints that call the issue
tracker, Bitbucket or Sonar need `secrets/` and won't work here.

## What you can't do from the cloud

You don't have `data/`, `secrets/`, the internal systems behind the VPN, or
the owner's Mac (sounds play there through `afplay`). If a task needs real
data — "why is my balance X", "fix this entry" — say it has to be done
locally instead of guessing.

## Data that must never be lost

On the owner's machine these hold history that can't be re-fetched; never
write code that deletes or regenerates them: `data/timetrack.json`,
`data/work-hours.json`, `data/jira-worklog.json` (stops worklogs being
written twice), `data/copilot-history.json`, `data/TogglTrack_*.csv`.

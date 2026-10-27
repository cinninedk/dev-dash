#!/usr/bin/env python3
"""Writes data-test/: made-up data for every page, dated relative to today so
the timesheet and worklog always have a current month to show.

Run by start-test.sh (it rewrites data-test/ each start, so edits made in test
mode don't survive a restart). All names, keys and numbers are invented — never
copy anything from data/ in here, the repo is public.

    python3 scripts/make-test-data.py [outdir]
"""
import datetime as dt
import json
import pathlib
import random
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data-test"
rnd = random.Random(7)                      # same data on every run (apart from the dates)
NOW = dt.datetime.now().astimezone().replace(microsecond=0)
TODAY = NOW.date()
TZ = NOW.tzinfo
ME = "Alex Example"
OTHERS = ["Robin Placeholder", "Sam Sample", "Kim Fixture", "Jo Mockley"]
URL = "https://{}.example.com"


def iso(d):
    return d.isoformat(timespec="seconds")


def at(day, h, m=0):
    return dt.datetime.combine(day, dt.time(h, m), TZ)


def ms(d):
    return int(d.timestamp() * 1000)


def sha():
    return uuid.UUID(int=rnd.getrandbits(128)).hex + "%08x" % rnd.getrandbits(32)


def write(name, obj, indent=2):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=indent) + "\n")


ISSUES = [  # key, summary, status, type
    ("ORBIT-101", "Add dark mode to the settings page", "IMPLEMENT", "Story"),
    ("ORBIT-104", "Fix crash when the profile picture is missing", "IMPLEMENT", "Bug"),
    ("ORBIT-97", "Cache the weather widget for five minutes", "QUALITY ASSURANCE", "Story"),
    ("NOVA-23", "Rate-limit the public search endpoint", "BUSINESS VALIDATION", "Story"),
    ("NOVA-19", "Upgrade the JSON parser library", "RESOLVED", "Task"),
    ("ORBIT-110", "Export the report as CSV", "OPEN", "Story"),
]
SUMMARY = {k: s for k, s, _, _ in ISSUES}


# ── timetrack.json + work-hours.json ─────────────────────────────────────
# Tracker history starts two months back (or 1 Jan); earlier months of the
# year are "seed" months with a made-up total, like the imported spreadsheet.
start_month = TODAY.month - 2 if TODAY.month > 2 else 1
tracker_start = dt.date(TODAY.year, start_month, 1)
holidays = [dt.date(TODAY.year, m, d).isoformat() for m, d in ((1, 1), (6, 5), (12, 24), (12, 25), (12, 26), (12, 31))]
vacation_day = tracker_start + dt.timedelta(days=9)
sick_day = tracker_start + dt.timedelta(days=23)
leave = []
for day, kind in ((vacation_day, "vacation"), (sick_day, "sick"), (TODAY + dt.timedelta(days=14), "vacation")):
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    leave.append({"id": uuid.UUID(int=rnd.getrandbits(128)).hex[:12], "date": day.isoformat(),
                  "type": kind, "hours": 7.4, "note": ""})
leave_days = {lv["date"] for lv in leave}

trackers = {k: {"summary": SUMMARY[k], "total_seconds": 0, "segments": []}
            for k in ("ORBIT-101", "ORBIT-104", "ORBIT-97", "NOVA-23", "NOVA-19")}
trackers["__IDLE__"] = {"summary": "Idle", "total_seconds": 0, "segments": []}
days = {}


def add(key, start, end):
    trackers[key]["segments"].append({"id": uuid.UUID(int=rnd.getrandbits(128)).hex[:12],
                                      "started_at": iso(start), "ended_at": iso(end),
                                      "seconds": int((end - start).total_seconds())})


day = tracker_start
while day < TODAY:
    ds = day.isoformat()
    weekday = day.weekday() < 5 and ds not in holidays and ds not in leave_days
    saturday_session = day.weekday() == 5 and day.day % 3 == 0   # now and then a bit of weekend work
    if weekday or saturday_session:
        t = at(day, 8, rnd.choice((0, 15, 30))) if weekday else at(day, 13)
        end = t + dt.timedelta(minutes=rnd.randint(400, 500) if weekday else rnd.randint(90, 200))
        while t < end:
            key = rnd.choice(["__IDLE__", "__IDLE__"] + list(trackers)[:-1])
            t2 = min(end, t + dt.timedelta(minutes=rnd.randint(20, 150)))
            add(key, t, t2)
            t = t2
        days[ds] = {"end": iso(end)}
    day += dt.timedelta(days=1)

# today: an hour of work so far, ORBIT-101 running since
active_since = NOW - dt.timedelta(minutes=40)
if active_since - dt.timedelta(hours=1) > at(TODAY, 0):
    add("__IDLE__", active_since - dt.timedelta(hours=1), active_since)
for tr in trackers.values():
    tr["total_seconds"] = sum(s["seconds"] for s in tr["segments"])
write("timetrack.json", {"active": {"key": "ORBIT-101", "started_at": iso(active_since)},
                         "trackers": trackers, "days": days, "updated": iso(NOW)})

write("work-hours.json", {
    "updated": iso(NOW),
    "current_year": str(TODAY.year),
    "years": {str(TODAY.year): {
        "constants": {"basis": 40000, "dagsnormtimer": 7.4, "bonustimegraense": 120, "timepris": 800,
                      "bonusprocent": 0.5, "bonustimeloen": 400, "bonusdagsloen": 2960,
                      "feriedage": 25, "feriefridag": 2},
        "holidays": holidays,
        "tracker_start": tracker_start.isoformat(),
        "seed_months": {str(m): round(140 + rnd.random() * 20, 1) for m in range(1, tracker_start.month)},
    }},
    "leave": leave,
})
write("jira-worklog.json", {"written": {}})


# ── jira.json ────────────────────────────────────────────────────────────
def activity(n):
    out = []
    for i in range(n):
        who, when = rnd.choice(OTHERS), iso(NOW - dt.timedelta(hours=3 + 20 * i))
        out.append(rnd.choice([
            {"type": "comment", "who": who, "when": when, "text": "Looks good, one question about the edge case."},
            {"type": "status", "who": who, "when": when, "from": "Open", "to": "Implement"},
            {"type": "edit", "who": who, "when": when, "fields": ["description"]},
        ]))
    return out


issues = []
for i, (key, summary, status, kind) in enumerate(ISSUES):
    if status == "OPEN":
        continue
    issues.append({"id": str(20000 + i), "key": key, "summary": summary, "status": status, "type": kind,
                   "priority": rnd.choice(["2", "3"]), "updated": iso(NOW - dt.timedelta(hours=i * 7)),
                   "parent_key": None, "teknisk_qa": False, "unassigned": False, "assignee": ME,
                   "in_current_sprint": True,
                   "blocked_by": [{"key": "NOVA-19", "status": "RESOLVED", "mine": True}] if key == "ORBIT-104" else [],
                   "prs": {"review": 1 if status == "IMPLEMENT" else 0, "approved": 1 if key == "ORBIT-97" else 0,
                           "draft": 1 if key == "ORBIT-104" else 0, "merged": 1 if status == "RESOLVED" else 0},
                   "activity": activity(i % 3)})
sprint_start = at(TODAY - dt.timedelta(days=TODAY.weekday() + 7), 9)
write("jira.json", {
    "updated": iso(NOW),
    "issues": issues,
    "tqa_issues": [
        {"id": "20100", "key": "NOVA-31", "summary": "Log the request id on every error", "status": "QUALITY ASSURANCE",
         "type": "Story", "priority": "3", "updated": iso(NOW - dt.timedelta(hours=5)), "teknisk_qa": True,
         "unassigned": True, "assigned_to_me": False, "implemented_by_me": False, "prs": {"review": 0, "approved": 0, "draft": 0, "merged": 1},
         "activity": []},
    ],
    "next_tasks": [
        {"key": "ORBIT-110", "summary": SUMMARY["ORBIT-110"], "status": "OPEN", "type": "Story", "priority": "3", "sprint_type": "current"},
        {"key": "NOVA-40", "summary": "Move the cron jobs to the scheduler", "status": "OPEN", "type": "Task", "priority": "2", "sprint_type": "next"},
    ],
    "sprint": {"name": "Sprint 42", "start_date": iso(sprint_start), "end_date": iso(sprint_start + dt.timedelta(days=14))},
    "jira_url": URL.format("jira"),
    "backlog_url": URL.format("jira") + "/secure/RapidBoard.jspa?rapidView=1",
})


# ── bitbucket.json ───────────────────────────────────────────────────────
def pr(id_, key, repo, author, build, qg, **kw):
    branch = f"feature/{key}-{SUMMARY.get(key, 'change').lower().replace(' ', '-')[:30]}"
    commit = sha()
    base = {"id": id_, "title": f"{key}: {SUMMARY.get(key, 'Small change')}", "repo": repo, "slug": repo,
            "project": repo.split("-")[0].upper(), "author": author, "branch": branch, "jira_key": key,
            "commit": commit, "approvals": 0, "reviewer_count": 2, "tasks": 0, "comments": 0,
            "merge_outcome": "CLEAN", "build_state": build, "build_name": f"{repo}-pr-{id_}",
            "build_url": URL.format("jenkins") + f"/job/{repo}/{id_}/", "needs_work": False, "draft": False,
            "updated": ms(NOW - dt.timedelta(minutes=id_ % 300)), "qg_label": qg,
            "sonar_url": URL.format("sonar") + f"/dashboard?id={repo}" if qg != "–" else "",
            "sonar_building": False, "bugs": 0, "smells": 0, "vulns": 0, "hotspots": 0}
    base.update(kw)
    return base


write("bitbucket.json", {
    "stash_url": URL.format("bitbucket"),
    "updated": iso(NOW),
    "my_prs": [
        pr(311, "ORBIT-101", "orbit-web", ME, "SUCCESSFUL", "PASS", approvals=1, smells=3, comments=2),
        pr(318, "ORBIT-104", "orbit-web", ME, "INPROGRESS", "–", draft=True, sonar_building=True),
        pr(64, "NOVA-23", "nova-gateway", ME, "FAILED", "FAIL", bugs=2, vulns=1, hotspots=1, tasks=1,
           needs_work=True, merge_outcome="CONFLICTED", comments=5),
    ],
    "reviewer_prs": [
        pr(320, "ORBIT-112", "orbit-api", "Robin Placeholder", "SUCCESSFUL", "PASS", comments=1),
        pr(71, "NOVA-35", "nova-gateway", "Sam Sample", "SUCCESSFUL", "WARN", smells=7),
    ],
})


# ── jenkins.json ─────────────────────────────────────────────────────────
pipelines = []
for label, state, ago in (("orbit-web", "SUCCESS", 50), ("orbit-api", "FAILURE", 130), ("nova-gateway", "BUILDING", 3)):
    url = URL.format("jenkins") + f"/job/{label}/job/master/lastBuild/"
    pipelines.append({"label": label, "url": url, "state": state, "commit": sha()[:7],
                      "timestamp": ms(NOW - dt.timedelta(minutes=ago)), "duration": 0.0 if state == "BUILDING" else 240000.0,
                      "build_url": url})
write("jenkins.json", {"updated": iso(NOW), "pipelines": pipelines})


# ── copilot.json + copilot-history.json ──────────────────────────────────
cols = [str(d) for d in range(1, TODAY.day + 1)]
users = []
for name in ("alex-example", "robin-ph", "sam-sample", "kim-fixture", "jo-mock"):
    daily = [None if rnd.random() < 0.2 else rnd.randint(0, 600) for _ in cols]
    users.append({"user": name, "month_total": sum(d or 0 for d in daily), "daily": daily})
users.sort(key=lambda u: -u["month_total"])
everyone = [sum(u["daily"][i] or 0 for u in users) for i in range(len(cols))]
users.insert(0, {"user": "All", "month_total": sum(everyone), "daily": everyone})
mine = next(u for u in users if u["user"] == "alex-example")
write("copilot.json", {
    "updated": iso(NOW), "org": "example-org", "month_label": TODAY.strftime("%B %Y"),
    "summary": f"{len(cols)} days of data | {len(users) - 1} active seats",
    "day_columns": cols, "my_user": "alex-example", "my_month_total": mine["month_total"],
    "my_daily": mine["daily"], "pool": 40000, "users": users,
})
write("copilot-history.json", {(TODAY - dt.timedelta(days=n)).isoformat(): rnd.randint(0, 500)
                               for n in range(90, 0, -1) if not 40 <= n < 50})   # a gap, like a poller that was off


# ── poll-state.json ──────────────────────────────────────────────────────
t = int(NOW.timestamp())
write("poll-state.json", {"fetching": "", "offline": False,
                          "bitbucket": {"interval": 30, "next": t + 30, "busy": False},
                          "jira": {"interval": 120, "next": t + 120},
                          "jenkins": {"interval": 20, "next": t + 20, "busy": True}},
      indent=None)   # one line: server.py sends it as a single server-sent-event data line
print(f"test data written to {OUT}")

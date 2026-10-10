#!/usr/bin/env python3
# export-worklog.py — every day of work ever recorded, as one CSV for salary
# analysis elsewhere. Writes data/worklog-export.csv (date,billed_hours,note,source).
#
# billed_hours follows the worklog page's faktureret: worked + ferie; sygdom and
# andet fravær are listed but bill 0. Sources, oldest first:
#   toggl        — daily sums of the Toggl detailed export (2024-03 .. 2026-05)
#   spreadsheet  — months sealed in work-hours.json seed_months only have a
#                  month total, so one reconcile row on the month's last day
#                  makes that month's rows sum to it (untracked ferie, rounding)
#   tracker      — timetrack.json from tracker_start on, quarter-rounded per
#                  issue per day exactly like the worklog page
#   leave        — work-hours.json leave records
#
#   python3 scripts/export-worklog.py
import calendar, csv, json, pathlib, sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server import _round_quarter

DATA = ROOT / "data"
TOGGL = DATA / "TogglTrack_Report_Detailed_report_(from_01_01_2006_to_12_31_2028).csv"
LEAVE_NOTE = {"vacation": "holiday", "sick": "sick", "child_sick": "child sick", "other": "leave"}

wh = json.loads((DATA / "work-hours.json").read_text())
tt = json.loads((DATA / "timetrack.json").read_text())
tracker_start = min(c["tracker_start"] for c in wh["years"].values() if c.get("tracker_start"))

worked = defaultdict(float)       # date -> hours
source = {}                       # date -> where its hours came from

# Toggl before the tracker took over. ponytail: an entry running past midnight
# counts on its start date; there are 7, all short.
with open(TOGGL, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        d = row["Start date"]
        if d >= tracker_start or row["Duration"].count(":") != 2:   # "-" = empty entry
            continue
        h, m, s = map(int, row["Duration"].split(":"))
        worked[d] += h + m / 60 + s / 3600
        source[d] = "toggl"

# tracker: quarter-round each (issue, day) block, then sum per day
per_key_day = defaultdict(int)
for key, tr in tt.get("trackers", {}).items():
    for seg in tr.get("segments", []):
        d = seg.get("started_at", "")[:10]
        if d >= tracker_start:
            per_key_day[key, d] += seg.get("seconds", 0)
for (_, d), secs in per_key_day.items():
    worked[d] += _round_quarter(secs) / 3600
    source[d] = "tracker"

leave = defaultdict(list)          # date -> [record]
for lv in wh.get("leave", []):
    leave[lv["date"]].append(lv)

rows = {}
for d in sorted(set(worked) | set(leave)):
    billed = worked[d] + sum(lv["hours"] for lv in leave[d] if lv["type"] == "vacation")
    notes = [f'{LEAVE_NOTE[lv["type"]]} {lv["hours"]:g}h' + (f' ({lv["note"]})' if lv.get("note") else "")
             for lv in leave[d]]
    rows[d] = [d, round(billed, 2), "; ".join(notes), source.get(d, "leave")]

# reconcile sealed months to their spreadsheet total
for year, cfg in wh["years"].items():
    for month, total in cfg.get("seed_months", {}).items():
        ym = f"{year}-{int(month):02d}"
        if ym >= tracker_start[:7]:
            continue
        diff = round(total - sum(r[1] for d, r in rows.items() if d.startswith(ym)), 2)
        if diff:
            last = f"{ym}-{calendar.monthrange(int(year), int(month))[1]:02d}"
            rows[last + "~"] = [last, diff, f"month total {total:g}h per spreadsheet; difference to the daily rows", "spreadsheet"]

out = DATA / "worklog-export.csv"
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "billed_hours", "note", "source"])
    w.writerows(rows[k] for k in sorted(rows))

# self-check: sealed months must sum to the spreadsheet, tracker months to the page's formula
by_month = defaultdict(float)
for r in rows.values():
    by_month[r[0][:7]] += r[1]
for year, cfg in wh["years"].items():
    for month, total in cfg.get("seed_months", {}).items():
        ym = f"{year}-{int(month):02d}"
        if ym < tracker_start[:7]:
            assert abs(by_month[ym] - total) < 0.01, (ym, by_month[ym], total)
print(f"wrote {out}: {len(rows)} rows, {rows[min(rows)][0]} .. {rows[max(rows)][0]}")

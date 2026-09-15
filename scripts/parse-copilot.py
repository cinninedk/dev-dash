#!/usr/bin/env python3
# parse-copilot.py — reads copilot-leaderboard's stdout on stdin, extracts the
# ai_credits_used table, and writes data/copilot.json. Called by poll-copilot.sh.
import json, os, re, sys
from datetime import datetime, timezone

raw = sys.stdin.read()
clean = re.sub(r'\x1b\[[0-9;]*m', '', raw)  # strip ANSI color codes
lines = clean.splitlines()


def find_table(lines, heading):
    start = None
    for i, l in enumerate(lines):
        if l.strip() == f'### {heading}':
            start = i
            break
    if start is None:
        return None, None
    j = start + 1
    while j < len(lines) and not lines[j].strip().startswith('|'):
        j += 1
    header = lines[j]
    rows = []
    k = j + 2  # skip header + separator row
    while k < len(lines) and lines[k].strip().startswith('|'):
        rows.append(lines[k])
        k += 1
    return header, rows


def parse_row(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def to_int(s):
    s = s.strip()
    return int(s) if s.lstrip('-').isdigit() else None


header, rows = find_table(lines, 'ai_credits_used')
if header is None:
    print('ERROR: could not find ai_credits_used table in output', file=sys.stderr)
    sys.exit(1)

cols = parse_row(header)
# Parse by column name, not position — copilot-leaderboard has reordered this
# table before (User/Total/days -> Total/days/User) and will again.
user_idx = cols.index('User')
total_idx = next(i for i, c in enumerate(cols) if 'total' in c.lower())
day_idxs = [i for i in range(len(cols)) if i not in (user_idx, total_idx)]
day_cols = [cols[i] for i in day_idxs]

users = []
for r in rows:
    cells = parse_row(r)
    user = cells[user_idx].strip('*').strip()
    if not user:
        continue
    month_total = to_int(cells[total_idx]) or 0
    daily = [to_int(cells[i]) if i < len(cells) else None for i in day_idxs]
    users.append({'user': user, 'month_total': month_total, 'daily': daily})

summary = ''
for l in lines:
    if l.strip().startswith('_') and 'days of data' in l:
        summary = l.strip().strip('_')
        break

title = next((l for l in lines if l.startswith('# Copilot Leaderboard')), '')
m = re.search(r'—\s*([\w-]+)\s*—\s*(.+)$', title)
org = m.group(1) if m else ''
month_label = m.group(2).strip() if m else ''

my_user = os.environ.get('MY_USER', 'nine-cin')
mine = next((u for u in users if u['user'] == my_user), None)

data = {
    'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'org': org,
    'month_label': month_label,
    'summary': summary,
    'day_columns': day_cols,
    'my_user': my_user,
    'my_month_total': mine['month_total'] if mine else None,
    'my_daily': mine['daily'] if mine else [],
    'users': users,
}

out_path = os.environ['OUT']
tmp_path = out_path + '.tmp'
with open(tmp_path, 'w') as f:
    json.dump(data, f, indent=2)
os.replace(tmp_path, out_path)
print(f"wrote {out_path}: {my_user} month total = {data['my_month_total']}")

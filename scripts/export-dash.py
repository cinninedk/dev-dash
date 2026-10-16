#!/usr/bin/env python3
"""Export compact row files from data/*.json for the Claude dashboard
("Jiraffe Daily"). Writes data/claude-dash/*.json. Read-only on data/.
Hour rules mirror worklog.html (quarter rounding per tracker+day,
faktureret = worked + vacation, sick/other come off the norm)."""
import json, os, datetime as dt
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, 'data'); OUT = os.path.join(D, 'claude-dash')
os.makedirs(OUT, exist_ok=True)
def load(n):
    with open(os.path.join(D, n)) as f: return json.load(f)
def save(n, rows):
    tmp = os.path.join(OUT, n + '.tmp')
    with open(tmp, 'w') as f: json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(OUT, n))

today = dt.date.today()
meta = {}

# ── PRs ──
b = load('bitbucket.json'); meta['prs'] = b.get('updated')
rows = []
for role, lst in (('mine', b.get('my_prs', [])), ('review', b.get('reviewer_prs', []))):
    for p in lst:
        rows.append({
            'pr': f"{p['repo']}#{p['id']}", 'role': role, 'id': p['id'], 'repo': p['repo'],
            'title': p['title'], 'author': p['author'], 'jira_key': p.get('jira_key') or '',
            'approvals': p.get('approvals', 0), 'reviewers': p.get('reviewer_count', 0),
            'needs_work': p.get('needs_work', 0), 'tasks': p.get('tasks', 0),
            'comments': p.get('comments', 0), 'conflict': p.get('merge_outcome') == 'CONFLICTED',
            'draft': bool(p.get('draft')), 'build': p.get('build_state', ''),
            'quality_gate': p.get('qg_label', ''), 'bugs': p.get('bugs', 0), 'vulns': p.get('vulns', 0),
            'updated': dt.datetime.utcfromtimestamp((p.get('updated') or 0) / 1000).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'url': f"{b.get('stash_url','')}/projects/{p.get('project','')}/repos/{p.get('slug','')}/pull-requests/{p['id']}",
        })
save('prs.json', rows)

# ── Jira ──
j = load('jira.json'); meta['jira'] = j.get('updated')
rows, seen = [], set()
for lst, group in ((j.get('issues', []), 'mine'), (j.get('tqa_issues', []), 'tech_qa')):
    for i in lst:
        if (i['key'], group) in seen: continue
        seen.add((i['key'], group))
        if group == 'tech_qa' and (i.get('implemented_by_me') or not i.get('unassigned')): continue
        prs = i.get('prs') or {}
        rows.append({'key': i['key'], 'group': group, 'summary': i['summary'], 'status': i['status'],
                     'type': i.get('type', ''), 'updated': (i.get('updated') or '')[:10],
                     'open_prs': prs.get('review', 0) + prs.get('approved', 0) + prs.get('draft', 0),
                     'blocked': bool(i.get('blocked_by')),
                     'url': f"{j.get('jira_url','')}/browse/{i['key']}"})
save('jira.json', rows)

# ── master builds ──
k = load('jenkins.json'); meta['builds'] = k.get('updated')
save('builds.json', [{'pipeline': p['label'], 'state': p['state'],
    'finished': dt.datetime.utcfromtimestamp((p['timestamp'] + p['duration']) / 1000).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'minutes': round(p['duration'] / 60000, 1), 'url': p['build_url']} for p in k.get('pipelines', [])])

# ── hours ──
def round_q(sec):
    m = int(sec // 60)
    if m <= 0: return 0
    r = m % 15; m = m - r if r < 5 else m + 15 - r
    return max(15, m) * 60
tt = load('timetrack.json'); wh = load('work-hours.json')
per = defaultdict(float)
for key, tr in tt.get('trackers', {}).items():
    for s in tr.get('segments', []):
        d = (s.get('started_at') or '')[:10]
        if d: per[(key, d)] += s.get('seconds', 0)
worked = defaultdict(float); by_issue = defaultdict(float)
since = (today - dt.timedelta(days=30)).isoformat()
for (key, d), sec in per.items():
    h = round_q(sec) / 3600; worked[d] += h
    if d >= since: by_issue[key] += h
leave = defaultdict(lambda: {'vacation': 0, 'absence': 0})
for lv in wh.get('leave', []):
    t = 'vacation' if lv['type'] == 'vacation' else 'absence'
    leave[lv['date']][t] += lv['hours']
Y = str(wh.get('current_year', today.year)); yc = wh['years'][Y]; c = yc['constants']
hol = set(yc['holidays']); dn = c['dagsnormtimer']
def owed(d): return 0 if d.weekday() >= 5 or d.isoformat() in hol else dn
start = dt.date.fromisoformat(yc.get('tracker_start') or f'{Y}-01-01')
daily, d = [], max(start, today - dt.timedelta(days=62))
while d <= today:
    ds = d.isoformat(); lv = leave.get(ds, {'vacation': 0, 'absence': 0})
    daily.append({'date': ds, 'worked': round(worked.get(ds, 0), 2), 'vacation': lv['vacation'],
                  'absence': lv['absence'], 'norm': owed(d)})
    d += dt.timedelta(days=1)
save('hours_daily.json', daily)

seeds = yc.get('seed_months', {}); ts = yc.get('tracker_start')
monthly = []
for m in range(1, 13):
    first = dt.date(int(Y), m, 1)
    nxt = dt.date(int(Y) + (m == 12), m % 12 + 1, 1)
    days = [first + dt.timedelta(n) for n in range((nxt - first).days)]
    absn = sum(min(leave[x.isoformat()]['absence'], dn) for x in days if owed(x) and x.isoformat() in leave)
    norm = sum(owed(x) for x in days) - absn
    phase = 'past' if nxt <= today else ('current' if first <= today else 'future')
    sealed = str(m) in seeds and (not ts or m < int(ts[5:7]))
    if sealed: fakt = seeds[str(m)]
    elif phase == 'future': fakt = None
    else: fakt = sum(worked.get(x.isoformat(), 0) + (leave[x.isoformat()]['vacation'] if x.isoformat() in leave else 0)
                     for x in days if x <= today)
    monthly.append({'month': first.isoformat()[:7], 'phase': phase,
                    'faktureret': None if fakt is None else round(fakt, 2), 'norm': round(norm, 2),
                    'bonus_threshold': c['bonustimegraense'], 'bonus_rate': c['bonustimeloen']})
save('hours_monthly.json', monthly)
names = {k: v.get('summary', '') for k, v in tt.get('trackers', {}).items()}
save('time_by_issue.json', sorted([{'key': 'Idle' if k == '__IDLE__' else k,
      'summary': 'Untracked time (idle tracker)' if k == '__IDLE__' else names.get(k, ''),
      'hours': round(h, 2)} for k, h in by_issue.items() if h > 0], key=lambda r: -r['hours']))
meta['hours'] = wh.get('updated')

# ── Copilot ──
cp = load('copilot.json'); meta['copilot'] = cp.get('updated'); me = cp.get('my_user')
users = sorted([u for u in cp.get('users', []) if u['user'] != 'All'], key=lambda u: -u['month_total'])
save('copilot_seats.json', [{'rank': i + 1, 'seat': 'Me' if u['user'] == me else f'Seat {i + 1}',
     'is_me': u['user'] == me, 'credits': u['month_total'], 'pool': cp.get('pool')} for i, u in enumerate(users)])
hist = load('copilot-history.json')
save('copilot_history.json', [{'date': k, 'credits': v} for k, v in sorted(hist.items())])
with open(os.path.join(OUT, 'meta.json'), 'w') as f: json.dump(meta, f)
print('wrote', sorted(os.listdir(OUT)))

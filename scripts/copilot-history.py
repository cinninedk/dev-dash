#!/usr/bin/env python3
# copilot-history.py — keeps data/copilot-history.json: my AI credits per day,
# for good. The Copilot page's table only covers the current month; this folds
# copilot-leaderboard's archive (one 28-day snapshot of GitHub's per-user
# per-day report per --update) into a history that outlives month ends.
# Called by poll-copilot.sh after every --update.
#
# A missing history file is rebuilt from every snapshot in the archive (delete
# it to rebuild, e.g. after a --backfill); after that only the newest snapshot
# is folded in, since it covers the last 28 days. A day a snapshot covers with
# no record for me is 0; a day no snapshot covers (the poller was off) stays
# absent, so the graph shows a gap instead of a zero.
#
#   python3 scripts/copilot-history.py --check    # self-check, synthetic data
import glob, json, os, sys
from datetime import date, timedelta


def fold(history, lines, user):
    """Fold one snapshot's NDJSON lines into history (ISO day -> credits)."""
    mine, first, last = {}, None, None
    for line in lines:
        r = json.loads(line)
        last = r['report_end_day']
        if r.get('ai_credits_used') is None:  # null before token billing started (July 2026)
            continue
        first = min(first or r['day'], r['day'])
        if r.get('user_login') == user:
            mine[r['day']] = r['ai_credits_used']
    if not first:
        return
    d = date.fromisoformat(first)
    while d <= date.fromisoformat(last):
        # truncated like the leaderboard table, so the numbers match the page
        history[d.isoformat()] = int(mine.get(d.isoformat(), 0))
        d += timedelta(days=1)


def main():
    out, user = os.environ['OUT'], os.environ['MY_USER']
    try:
        with open(out) as f:
            history = json.load(f)
    except FileNotFoundError:
        history = {}
    snaps = sorted(glob.glob(os.path.join(os.environ['ARCHIVE'], '*.ndjson')))
    for snap in (snaps[-1:] if history else snaps):
        with open(snap) as f:
            fold(history, f, user)
    tmp = out + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(history, f, indent=0, sort_keys=True)
    os.replace(tmp, out)
    print(f'wrote {out}: {len(history)} days')


def check():
    def snap(start, end, *recs):
        return [json.dumps({'report_start_day': start, 'report_end_day': end, **r}) for r in recs]
    h = {}
    # pre-billing nulls are skipped; zero-filled from the first day with credits to the end
    fold(h, snap('2026-07-01', '2026-07-05',
                 {'day': '2026-07-01', 'user_login': 'me', 'ai_credits_used': None},
                 {'day': '2026-07-02', 'user_login': 'bob', 'ai_credits_used': 5.0},
                 {'day': '2026-07-03', 'user_login': 'me', 'ai_credits_used': 12.9}), 'me')
    assert h == {'2026-07-02': 0, '2026-07-03': 12, '2026-07-04': 0, '2026-07-05': 0}, h
    # a newer snapshot rewrites the days it covers and keeps the older ones
    fold(h, snap('2026-07-04', '2026-07-06',
                 {'day': '2026-07-04', 'user_login': 'me', 'ai_credits_used': 3.5}), 'me')
    assert h == {'2026-07-02': 0, '2026-07-03': 12, '2026-07-04': 3, '2026-07-05': 0, '2026-07-06': 0}, h
    # a snapshot with no credits at all changes nothing
    fold(h, snap('2026-07-06', '2026-07-09',
                 {'day': '2026-07-08', 'user_login': 'me', 'ai_credits_used': None}), 'me')
    assert len(h) == 5 and '2026-07-08' not in h, h
    print('ok')


if __name__ == '__main__':
    check() if sys.argv[1:] == ['--check'] else main()

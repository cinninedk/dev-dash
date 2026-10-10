#!/usr/bin/env node
// Self-check for the sygdom / "andet fravær" norm rule: those hours are not billable
// (never enter faktureret) but must not count as hours owed either, so they
// come off the norm. Runs the REAL functions out of worklog.html — no copy to
// drift — against the real work-hours.json plus a few synthetic edge cases.
//
//   node scripts/check-norm.mjs

import {readFileSync} from 'fs';
import assert from 'assert';

const root = new URL('..', import.meta.url);
const html = readFileSync(new URL('worklog.html', root), 'utf8');

// pull the shipped implementations straight out of the page
const grab = name => {
  const m = html.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, 'm'));
  if (!m) throw new Error(`${name}() not found in worklog.html`);
  return m[0];
};

const pad = n => String(n).padStart(2, '0');
const ymd = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;
const daysInMonth = (y, m) => new Date(y, m, 0).getDate();
const leaveByDate = {};          // mutated in place; the closure sees updates

const [absenceNormHours, workdaysInMonth] = new Function(
  'daysInMonth', 'ymd', 'leaveByDate', 'today',
  `${grab('absenceNormHours')}\n${grab('workdaysInMonth')}\nreturn [absenceNormHours, workdaysInMonth];`
)(daysInMonth, ymd, leaveByDate, new Date());

const setLeave = entries => {
  for (const k of Object.keys(leaveByDate)) delete leaveByDate[k];
  for (const e of entries) {
    const m = leaveByDate[e.date] || (leaveByDate[e.date] = {vacation: 0, sick: 0, child_sick: 0, other: 0});
    m[e.type] += e.hours;
  }
};

const wh = JSON.parse(readFileSync(new URL('data/work-hours.json', root), 'utf8'));
const cfg = wh.years['2026'];
const dn = cfg.constants.dagsnormtimer;
const hol = new Set(cfg.holidays);

// ── 1. real data: two Kickoff days + one sick day in September 2026 ───
setLeave(wh.leave);
const rawNorm = workdaysInMonth(2026, 9, hol) * dn;
const off = +absenceNormHours(2026, 9, hol, dn, null).toFixed(2);
assert.strictEqual(rawNorm, 162.8, `Sept raw norm ${rawNorm}`);
assert.strictEqual(off, 22.2, `Sept fravær off norm ${off}`);
assert.strictEqual(+(rawNorm - off).toFixed(2), 140.6, 'Sept adjusted norm');

// a month with no "andet" is untouched
assert.strictEqual(absenceNormHours(2026, 7, hol, dn, null), 0, 'July has no andet');

// ── 2. an over-long entry cannot reduce a day below its own norm ──────
setLeave([{date: '2026-09-17', type: 'other', hours: 12}]);
assert.strictEqual(absenceNormHours(2026, 9, hol, dn, null), dn, 'capped at one day norm');

// ── 3. weekend / holiday entries carry no norm to cancel ──────────────
setLeave([{date: '2026-09-19', type: 'other', hours: 7.4}]);   // Saturday
assert.strictEqual(absenceNormHours(2026, 9, hol, dn, null), 0, 'weekend contributes 0');
const holiday = cfg.holidays.find(h => {
  const [y, m, d] = h.split('-').map(Number);
  return new Date(y, m - 1, d).getDay() % 6 !== 0;             // a weekday holiday
});
if (holiday) {
  setLeave([{date: holiday, type: 'other', hours: 7.4}]);
  const [hy, hm] = holiday.split('-').map(Number);
  assert.strictEqual(absenceNormHours(hy, hm, hol, dn, null), 0, `holiday ${holiday} contributes 0`);
}

// ── 4. sygdom cancels norm like andet; ferie (billable) never does ────
setLeave([{date: '2026-09-17', type: 'vacation', hours: 7.4},
          {date: '2026-09-18', type: 'sick', hours: 7.4}]);
assert.strictEqual(absenceNormHours(2026, 9, hol, dn, null), 7.4, 'sygdom off norm, ferie not');
setLeave([{date: '2026-09-18', type: 'sick', hours: 4}, {date: '2026-09-18', type: 'other', hours: 7.4}]);
assert.strictEqual(absenceNormHours(2026, 9, hol, dn, null), dn, 'sygdom + andet share one day cap');

// ── 5. `before` bounds it to days that have already happened ──────────
setLeave(wh.leave);
assert.strictEqual(absenceNormHours(2026, 9, hol, dn, new Date(2026, 8, 18)), 7.4, 'only 09-17 elapsed');
assert.strictEqual(absenceNormHours(2026, 9, hol, dn, new Date(2026, 8, 17)), 0, 'neither elapsed yet');

// ── 6. working-time balance (Arbejdstid, 37 t/uge): owed vs worked + leave ──
const worked = {};
const dayWork = new Function('leaveByDate', 'worked', `${grab('dayWork')}\nreturn dayWork;`)(leaveByDate, worked);
const diff = ds => +dayWork(ds, hol, dn).diff.toFixed(2);
setLeave([{date: '2026-10-06', type: 'child_sick', hours: 7.4},
          {date: '2026-10-10', type: 'vacation', hours: 7.4}]);          // Saturday
worked['2026-10-07'] = 6;   assert.strictEqual(diff('2026-10-07'), -1.4, 'short day deducts');
worked['2026-10-08'] = 9;   assert.strictEqual(diff('2026-10-08'), 1.6, 'long day adds');
assert.strictEqual(diff('2026-10-06'), 0, 'barns sygedag covers the day');
assert.strictEqual(diff('2026-10-10'), 0, 'leave on a weekend adds nothing');
worked['2026-10-11'] = 2;   assert.strictEqual(diff('2026-10-11'), 2, 'weekend work adds');
assert.strictEqual(diff('2026-10-09'), -dn, 'empty workday owes a full day');
if (holiday) assert.strictEqual(diff(holiday), 0, 'holiday owes nothing');

console.log('ok — sygdom, barns sygedag and andet fravær reduce norm; working-time balance rules hold');

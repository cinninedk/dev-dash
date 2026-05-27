#!/usr/bin/env python3
"""
DEV-DASHBOARD  –  terminal TUI (curses)
Reads data/bitbucket.json and data/jira.json (written by poll.sh every 60s).
Auto-refreshes every 60 s during 08:00–18:00 Mon–Fri.
Quit: q or Ctrl-C.
"""

import curses
import json
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
BB_JSON     = BASE_DIR / "data" / "bitbucket.json"
JIRA_JSON   = BASE_DIR / "data" / "jira.json"

# ── Colour pair indices (defined once in init_colors) ─────────────────────────
C_DIM    = 1   # amber dim   – section labels, secondary text
C_BRIGHT = 2   # amber bright – titles, values
C_GREEN  = 3   # successful / pass
C_RED    = 4   # failed / fail
C_YELLOW = 5   # in-progress / warning
C_BLUE   = 6   # story type
C_BORDER = 7   # border lines (same as dim but may differ)
C_HEADER = 8   # header background

def _cfg(key: str, default):
    try:
        for line in (BASE_DIR / "config.yaml").read_text().splitlines():
            k, _, v = line.partition(":")
            if k.strip() == key:
                val = v.strip()
                if isinstance(default, bool):
                    return val.lower() not in ("false", "0", "no")
                return type(default)(val)
    except Exception:
        pass
    return default

REFRESH_INTERVAL   = _cfg("tui_refresh_seconds", 60)
WORK_HOURS_ENABLED = _cfg("work_hours_enabled",  True)
WORK_START_H       = _cfg("work_start_hour",      8)
WORK_END_H         = _cfg("work_end_hour",         18)


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_work_hours() -> bool:
    if not WORK_HOURS_ENABLED:
        return True
    now = datetime.now()
    return now.weekday() < 5 and WORK_START_H <= now.hour < WORK_END_H


def fmt_duration(seconds: int) -> str:
    h  = seconds // 3600
    m  = (seconds % 3600) // 60
    s  = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M")
    except Exception:
        return iso[:5]


def time_ago(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        dt  = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        s   = int((now - dt).total_seconds())
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        return f"{s//3600}h ago"
    except Exception:
        return "?"


def trunc(text: str, width: int) -> str:
    """Truncate a string, appending … if needed."""
    if not text:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "…"


def load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# ── Data model ─────────────────────────────────────────────────────────────────

class DashData:
    def __init__(self):
        self.bb:          dict | None = None
        self.jira:        dict | None = None
        self.missing_files: bool = False
        self.last_load:   float = 0.0
        self._lock = threading.Lock()

    def reload(self):
        if not BB_JSON.exists() or not JIRA_JSON.exists():
            with self._lock:
                self.missing_files = True
            return
        bb   = load_json(BB_JSON)
        jira = load_json(JIRA_JSON)
        with self._lock:
            self.bb   = bb
            self.jira = jira
            self.missing_files = False
            self.last_load = time.time()

    def snapshot(self):
        with self._lock:
            return self.bb, self.jira, self.missing_files, self.last_load


# ── Clickable URL registry ─────────────────────────────────────────────────────
# Each entry: (screen_y, x_start, x_end, url). Rebuilt every render cycle.
_url_map: list[tuple[int, int, int, str]] = []

def clear_url_map():
    global _url_map
    _url_map = []

def register_link(win, y: int, x: int, text_len: int, url: str):
    if not url or text_len <= 0:
        return
    beg_y, beg_x = win.getbegyx()
    _url_map.append((beg_y + y, beg_x + x, beg_x + x + text_len, url))

def url_at(screen_y: int, screen_x: int) -> str | None:
    for (sy, x0, x1, url) in _url_map:
        if sy == screen_y and x0 <= screen_x < x1:
            return url
    return None


# ── Curses drawing primitives ──────────────────────────────────────────────────

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    # (fg, bg=-1 means default terminal background)
    curses.init_pair(C_DIM,    178, -1)   # amber dim
    curses.init_pair(C_BRIGHT, 220, -1)   # amber bright
    curses.init_pair(C_GREEN,  76,  -1)   # green
    curses.init_pair(C_RED,    196, -1)   # red
    curses.init_pair(C_YELLOW, 214, -1)   # yellow/orange
    curses.init_pair(C_BLUE,   75,  -1)   # blue
    curses.init_pair(C_BORDER, 136, -1)   # border amber
    curses.init_pair(C_HEADER, 220, -1)   # header (reuse bright)


def ca(pair: int, bold: bool = False) -> int:
    attr = curses.color_pair(pair)
    if bold:
        attr |= curses.A_BOLD
    return attr


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0):
    """addstr that silently ignores out-of-bounds and resize errors."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return
    avail = max_x - x
    if avail <= 0:
        return
    try:
        win.addstr(y, x, text[:avail], attr)
    except curses.error:
        pass


def safe_addlink(win, y: int, x: int, text: str, url: str, attr: int = 0):
    safe_addstr(win, y, x, text, attr | curses.A_UNDERLINE)
    register_link(win, y, x, len(text), url)


def hline(win, y: int, x: int, length: int, char: str = "─"):
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y:
        return
    avail = min(length, max_x - x)
    if avail <= 0:
        return
    try:
        win.addstr(y, x, char * avail, ca(C_BORDER))
    except curses.error:
        pass


def vline_char(win, y: int, x: int, height: int, char: str = "│"):
    for row in range(y, y + height):
        safe_addstr(win, row, x, char, ca(C_BORDER))


# ── Render helpers ─────────────────────────────────────────────────────────────

def make_pr_url(pr: dict, stash_url: str) -> str:
    p, s, i = pr.get("project",""), pr.get("slug",""), pr.get("id","")
    if stash_url and p and s and i:
        return f"{stash_url}/projects/{p}/repos/{s}/pull-requests/{i}"
    return ""


def build_badge(state: str | None) -> tuple[str, int]:
    """Return (text, color_pair) for a build state badge."""
    match state:
        case "SUCCESSFUL": return ("SUCCESSFUL", C_GREEN)
        case "FAILED":     return ("FAILED",     C_RED)
        case "INPROGRESS": return ("BUILDING",   C_YELLOW)
        case _:            return ("NO BUILD",   C_DIM)


def qg_badge(label: str | None) -> tuple[str, int]:
    match label:
        case "PASS": return ("QG PASSED",  C_GREEN)
        case "FAIL": return ("QG FAILED",  C_RED)
        case "WARN": return ("QG WARNING", C_YELLOW)
        case _:      return ("QG UNKNOWN", C_DIM)


def prio_sym(p: str | None) -> tuple[str, int]:
    match p:
        case "High":   return ("▲", C_RED)
        case "Medium": return ("─", C_YELLOW)
        case "Low":    return ("▼", C_DIM)
        case _:        return ("─", C_DIM)


def type_col(t: str | None) -> int:
    match t:
        case "Story": return C_BLUE
        case "Bug":   return C_RED
        case _:       return C_DIM


# ── Section renderers ──────────────────────────────────────────────────────────

def render_header(win, session_start: float, next_refresh_at: float | None):
    max_y, max_x = win.getmaxyx()
    win.erase()
    now_str = datetime.now().strftime("%a %d/%m  %H:%M:%S")
    title   = "◆ DEV-DASHBOARD"
    safe_addstr(win, 0, 1, title, ca(C_BRIGHT, bold=True))

    uptime_s = int(time.time() - session_start)
    uptime   = f"UP {fmt_duration(uptime_s)}"
    safe_addstr(win, 0, len(title) + 3, uptime, ca(C_DIM))

    if next_refresh_at is not None:
        secs = max(0, int(next_refresh_at - time.time()))
        nxt  = f"NEXT {secs:>3}s"
        safe_addstr(win, 0, len(title) + 3 + len(uptime) + 3, nxt, ca(C_DIM))

    safe_addstr(win, 0, max_x - len(now_str) - 1, now_str, ca(C_BRIGHT, bold=True))
    hline(win, 1, 0, max_x)


def render_stats(win, bb: dict, jira: dict):
    max_y, max_x = win.getmaxyx()
    win.erase()

    my_prs   = bb.get("my_prs", [])   if bb   else []
    rev_prs  = bb.get("reviewer_prs", []) if bb else []
    issues   = jira.get("issues", []) if jira else []
    active   = [i for i in issues if i.get("status") != "RESOLVED"]
    failing  = sum(1 for p in my_prs if p.get("build_state") == "FAILED")

    items = [
        ("MY PRs",        str(len(my_prs)),  C_BRIGHT),
        ("FOR REVIEW",    str(len(rev_prs)), C_BRIGHT),
        ("JIRA ACTIVE",   str(len(active)),  C_BRIGHT),
        ("BUILDS FAILING",str(failing),      C_RED if failing else C_GREEN),
    ]
    col_w = max_x // 4
    for i, (label, val, vc) in enumerate(items):
        x = i * col_w
        safe_addstr(win, 0, x + 1, label, ca(C_DIM))
        safe_addstr(win, 0, x + len(label) + 2, str(val), ca(vc, bold=True))
        if i < 3:
            safe_addstr(win, 0, x + col_w - 1, "│", ca(C_BORDER))

    hline(win, 1, 0, max_x)


def render_pr_row(win, y: int, pr: dict, show_author: bool, max_x: int, stash_url: str = "", show_branch: bool = False):
    """Render one PR (two lines: main + detail)."""
    pr_id   = f"#{pr.get('id','?')}"
    repo    = pr.get("repo","")
    author  = trunc(pr.get("author",""), 16) if show_author else ""

    build_txt, build_c = build_badge(pr.get("build_state"))
    qg_txt,    qg_c    = qg_badge(pr.get("qg_label"))
    appr       = pr.get("approvals",  0)
    needs_work = pr.get("needs_work", 0)
    total      = pr.get("reviewer_count", 0)
    tasks      = pr.get("tasks", 0)
    if total == 0:
        appr_str, appr_c = "No reviewers", C_DIM
    elif appr > 0:
        appr_str, appr_c = "APPROVED",     C_GREEN
    else:
        appr_str, appr_c = "Pending",      C_DIM
    nw_txt = " NEEDS WORK" if needs_work > 0 else ""

    # conflict badge
    conflict_txt = " CONFLICT" if pr.get("merge_outcome") == "CONFLICTED" else ""

    # fixed-width right side
    tasks_txt = f" Tasks:{tasks}" if tasks > 0 else ""
    right = f" {appr_str}{nw_txt}{tasks_txt}  {build_txt} {qg_txt}{conflict_txt}"
    right_w = len(right)

    # compute space for title
    left_w = 5 + 1 + len(repo) + 1 + (17 if show_author else 0)
    title_w = max(8, max_x - left_w - right_w - 2)
    title   = trunc(pr.get("title",""), title_w)

    # ── line 1: main row ──
    x = 1
    safe_addstr(win, y, x, pr_id,  ca(C_DIM));         x += 6
    safe_addstr(win, y, x, repo,   ca(C_DIM));         x += len(repo) + 1
    if show_author:
        safe_addstr(win, y, x, author, ca(C_DIM));     x += 17
    url = make_pr_url(pr, stash_url)
    safe_addlink(win, y, x, title, url, ca(C_BRIGHT))

    rx = max_x - right_w - 1
    ax = rx
    safe_addstr(win, y, ax,                             f" {appr_str}", ca(appr_c));  ax += 1 + len(appr_str)
    if nw_txt:
        safe_addstr(win, y, ax,                         " NEEDS WORK",  ca(C_RED));   ax += len(" NEEDS WORK")
    if tasks > 0:
        safe_addstr(win, y, ax + 1,                     f"Tasks:{tasks}", ca(C_YELLOW))
    bx = max_x - len(build_txt) - len(qg_txt) - len(conflict_txt) - 3
    safe_addlink(win, y, bx,                            build_txt,      pr.get("build_url",""),  ca(build_c))
    safe_addstr(win, y, bx + len(build_txt) + 1,        qg_txt,         ca(qg_c))
    if conflict_txt:
        safe_addstr(win, y, bx + len(build_txt) + 1 + len(qg_txt) + 1,
                    "CONFLICT", ca(C_RED))

    # ── line 2: detail ──
    dy = y + 1
    commit  = (pr.get("commit") or "")[:7] or "–"
    bugs    = pr.get("bugs",     0)
    smells  = pr.get("smells",   0)
    vulns   = pr.get("vulns",    0)
    hots    = pr.get("hotspots", 0)
    cmts    = pr.get("comments", 0)

    dx = 7   # indent past the #id column
    safe_addstr(win, dy, dx, commit, ca(C_BLUE));                   dx += len(commit) + 2
    if show_branch:
        branch = trunc(pr.get("branch", ""), 40)
        safe_addstr(win, dy, dx, branch, ca(C_DIM));                dx += len(branch) + 2
    safe_addstr(win, dy, dx, f"bugs:{bugs}",     ca(C_RED    if bugs  > 0 else C_DIM)); dx += len(f"bugs:{bugs}")     + 2
    safe_addstr(win, dy, dx, f"smells:{smells}", ca(C_YELLOW if smells> 0 else C_DIM)); dx += len(f"smells:{smells}") + 2
    safe_addstr(win, dy, dx, f"vulns:{vulns}",   ca(C_RED    if vulns > 0 else C_DIM)); dx += len(f"vulns:{vulns}")   + 2
    safe_addstr(win, dy, dx, f"hots:{hots}",     ca(C_YELLOW if hots  > 0 else C_DIM)); dx += len(f"hots:{hots}")     + 2
    safe_addstr(win, dy, dx, f"{cmts} comments",  ca(C_BRIGHT if cmts  > 0 else C_DIM))


def render_prs_section(win, title: str, prs: list, show_author: bool, stash_url: str = ""):
    max_y, max_x = win.getmaxyx()
    win.erase()
    safe_addstr(win, 0, 1, title, ca(C_BRIGHT, bold=True))
    hline(win, 1, 0, max_x)

    if not prs:
        safe_addstr(win, 2, 2, "no open PRs" if not show_author else "no PRs awaiting review",
                    ca(C_DIM))
        return

    row = 2
    for pr in prs:
        if row + 1 >= max_y:   # need 2 lines
            break
        render_pr_row(win, row, pr, show_author, max_x, stash_url, show_branch=not show_author)
        row += 2


def render_kanban(win, bb: dict | None, jira: dict | None):
    max_y, max_x = win.getmaxyx()
    win.erase()
    safe_addstr(win, 0, 1, "JIRA KANBAN", ca(C_BRIGHT, bold=True))
    hline(win, 1, 0, max_x)

    issues = (jira.get("issues", []) if jira else []) or []

    cols = {
        "OPEN":      [i for i in issues if i.get("status") in ("OPEN", "REOPENED")],
        "IMPLEMENT": [i for i in issues if i.get("status") == "IMPLEMENT"],
        "QA":        [i for i in issues if i.get("status") == "QUALITY ASSURANCE"],
        "BV":        [i for i in issues if i.get("status") == "BUSINESS VALIDATION"],
        "RESOLVED":  [i for i in issues if i.get("status") == "RESOLVED"],
    }
    col_names = ["OPEN", "IMPLEMENT", "QA", "BV", "RESOLVED"]

    col_w = max_x // 5

    # column headers
    for ci, name in enumerate(col_names):
        x     = ci * col_w
        count = len(cols[name])
        label = f" {name} ({count})"
        hattr = ca(C_BRIGHT if name == "REVIEW" else C_DIM)
        safe_addstr(win, 2, x, label, hattr)
        if ci < 4:
            vline_char(win, 2, x + col_w - 1, max_y - 2)

    hline(win, 3, 0, max_x)

    # cards
    for ci, name in enumerate(col_names):
        x = ci * col_w
        card_x = x + 1
        card_w = col_w - 2
        if card_w < 4:
            continue
        row = 4
        for issue in cols[name]:
            if row >= max_y - 1:
                break
            key     = issue.get("key", "?")
            summary = issue.get("summary", "")
            itype   = issue.get("type", "")
            tc      = type_col(itype)
            jira_url = (jira.get("jira_url", "") if jira else "") or ""
            issue_url = f"{jira_url}/browse/{key}" if jira_url else ""

            safe_addlink(win, row, card_x,
                         trunc(key, card_w), issue_url, ca(C_BRIGHT))
            row += 1
            if row >= max_y - 1:
                break
            # summary — one line
            safe_addstr(win, row, card_x,
                        trunc(summary, card_w), ca(C_DIM))
            row += 1
            if row >= max_y - 1:
                break
            # type tag
            safe_addstr(win, row, card_x, trunc(itype, card_w), ca(tc))
            row += 1
            # blank separator
            row += 1


def render_statusbar(win, bb: dict | None, next_at: float | None, session_start: float):
    max_y, max_x = win.getmaxyx()
    win.erase()
    # fill the status bar row with a dim background
    try:
        win.addstr(0, 0, " " * (max_x - 1), ca(C_DIM) | curses.A_REVERSE)
    except curses.error:
        pass

    updated   = (bb.get("updated") if bb else None)
    age_str   = time_ago(updated)
    poll_str  = fmt_time(updated)

    up_s   = int(time.time() - session_start)
    up_str = fmt_duration(up_s)

    if next_at is not None:
        secs    = max(0, int(next_at - time.time()))
        nxt_str = f"{secs}s"
    else:
        nxt_str = "outside hours"

    parts = [
        ("SESSION",   up_str),
        ("LAST POLL", poll_str),
        ("DATA AGE",  age_str),
        ("NEXT",      nxt_str),
    ]

    x = 0
    for label, val in parts:
        seg = f" {label}: "
        safe_addstr(win, 0, x, seg,  ca(C_DIM))
        x += len(seg)
        safe_addstr(win, 0, x, val,  ca(C_BRIGHT))
        x += len(val)
        safe_addstr(win, 0, x, "  │", ca(C_BORDER))
        x += 3

    now_str = datetime.now().strftime("%H:%M:%S")
    safe_addstr(win, 0, max_x - len(now_str) - 1, now_str, ca(C_BRIGHT, bold=True))


def render_no_data(win):
    max_y, max_x = win.getmaxyx()
    win.erase()
    msg1 = "◆ DEV-DASHBOARD"
    msg2 = "no data found — run ./poll.sh to populate data/"
    msg3 = "press q to quit"
    y = max_y // 2 - 2
    safe_addstr(win, y,     (max_x - len(msg1)) // 2, msg1, ca(C_BRIGHT, bold=True))
    safe_addstr(win, y + 2, (max_x - len(msg2)) // 2, msg2, ca(C_DIM))
    safe_addstr(win, y + 3, (max_x - len(msg3)) // 2, msg3, ca(C_DIM))


# ── Layout ─────────────────────────────────────────────────────────────────────

class Layout:
    """Calculates sub-window positions for the current terminal size."""

    def __init__(self, rows: int, cols: int):
        self.rows = rows
        self.cols = cols

        self.header_h    = 2
        self.stats_h     = 2
        inner = rows - self.header_h - self.stats_h
        # Split inner vertically:
        #   my_prs       – fixed, at most min(N+3, max 8) rows
        #   kanban       – flexible
        #   reviewer_prs – fixed, similar
        my_h    = max(3, min(8,  inner // 3))
        rev_h   = max(3, min(8,  inner // 3))
        kanban_h = max(5, inner - my_h - rev_h)

        self.header_y    = 0
        self.stats_y     = self.header_h
        self.my_prs_y    = self.stats_y + self.stats_h
        self.kanban_y    = self.my_prs_y + my_h
        self.rev_prs_y   = self.kanban_y + kanban_h
        self.my_prs_h    = my_h
        self.kanban_h    = kanban_h
        self.rev_prs_h   = max(3, rows - self.rev_prs_y)


def make_windows(stdscr, ly: Layout):
    cols = ly.cols

    def win(h, w, y, x):
        h = max(1, h)
        w = max(1, w)
        return curses.newwin(h, w, y, x)

    header_win    = win(ly.header_h,    cols, ly.header_y,    0)
    stats_win     = win(ly.stats_h,     cols, ly.stats_y,     0)
    my_prs_win    = win(ly.my_prs_h,    cols, ly.my_prs_y,    0)
    kanban_win    = win(ly.kanban_h,    cols, ly.kanban_y,     0)
    rev_prs_win   = win(ly.rev_prs_h,   cols, ly.rev_prs_y,   0)

    return header_win, stats_win, my_prs_win, kanban_win, rev_prs_win


# ── Main loop ──────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(250)   # check for input every 250 ms
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    init_colors()

    data          = DashData()
    session_start = time.time()
    next_at: float | None = None

    # load immediately
    data.reload()
    if is_work_hours():
        next_at = time.time() + REFRESH_INTERVAL

    prev_size = (0, 0)
    wins      = None
    ly        = None

    while True:
        # ── input ──────────────────────────────────────────────────────────
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key in (ord('q'), ord('Q')):
            break
        if key in (ord('r'), ord('R')):
            data.reload()
            next_at = time.time() + REFRESH_INTERVAL if is_work_hours() else None
        if key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                if bstate & curses.BUTTON1_CLICKED:
                    url = url_at(my, mx)
                    if url:
                        subprocess.Popen(["open", url],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            except curses.error:
                pass

        # ── resize check ───────────────────────────────────────────────────
        rows, cols = stdscr.getmaxyx()
        if (rows, cols) != prev_size:
            stdscr.clear()
            stdscr.refresh()
            ly   = Layout(rows, cols)
            wins = make_windows(stdscr, ly)
            prev_size = (rows, cols)

        # ── auto-refresh ───────────────────────────────────────────────────
        if next_at is not None and time.time() >= next_at:
            data.reload()
            next_at = time.time() + REFRESH_INTERVAL if is_work_hours() else None
        elif next_at is None and is_work_hours():
            next_at = time.time() + REFRESH_INTERVAL

        bb, jira, missing, _ = data.snapshot()

        # ── draw ───────────────────────────────────────────────────────────
        if wins is None:
            continue

        (header_win, stats_win, my_prs_win,
         kanban_win, rev_prs_win) = wins

        if missing:
            stdscr.clear()
            render_no_data(stdscr)
            stdscr.refresh()
            time.sleep(0.25)
            continue

        my_prs    = (bb.get("my_prs", [])        if bb else []) or []
        rev_prs   = (bb.get("reviewer_prs", [])  if bb else []) or []
        stash_url = (bb.get("stash_url", "")     if bb else "") or ""

        clear_url_map()
        render_header(header_win, session_start, next_at)
        render_stats(stats_win, bb or {}, jira or {})
        render_prs_section(my_prs_win,  "MY PULL REQUESTS", my_prs,  show_author=False, stash_url=stash_url)
        render_kanban(kanban_win, bb, jira)
        render_prs_section(rev_prs_win, "PRs FOR MY REVIEW", rev_prs, show_author=True,  stash_url=stash_url)

        # refresh all
        for w in wins:
            try:
                w.noutrefresh()
            except curses.error:
                pass
        curses.doupdate()


def run():
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    run()

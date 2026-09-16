#!/usr/bin/env python3
"""Static file server for the dashboard.

Serves static files and handles /api/comments?pr_id=<N> by proxying
to Bitbucket so the browser can fetch reviewer comments without needing
the token directly.
"""
import datetime
import html
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).parent

def _cfg(key: str, default):
    try:
        for line in (ROOT / "config.yaml").read_text().splitlines():
            k, _, v = line.partition(":")
            if k.strip() == key:
                return type(default)(v.strip())
    except Exception:
        pass
    return default

PORT = _cfg("port", 666)
TEST_MODE = "--test" in sys.argv
_port_arg = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--port" and i+1 < len(sys.argv)), None)
if _port_arg:
    PORT = int(_port_arg)
DATA_DIR  = ROOT / ("data-test" if TEST_MODE else "data")
ACTIVE    = DATA_DIR / ".active"
TIMETRACK = DATA_DIR / "timetrack.json"
WORKLOG_LEDGER = DATA_DIR / "jira-worklog.json"
WORKHOURS = DATA_DIR / "work-hours.json"
IDLE_KEY  = "__IDLE__"
LEAVE_TYPES = ("vacation", "sick", "other")

JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _parse_dt(s: str) -> datetime.datetime:
    """Parse an ISO datetime to an aware local-tz datetime.

    Accepts 'Z' (Python 3.9's fromisoformat does not) and naive strings
    (assumed local). Raises ValueError on anything unparseable.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # assume local
    return dt.astimezone()


def _seg_seconds(started_at: str, ended_at: str) -> int:
    delta = _parse_dt(ended_at) - _parse_dt(started_at)
    return int(delta.total_seconds())


def _recompute_total(tracker: dict) -> None:
    tracker["total_seconds"] = sum(int(s.get("seconds", 0)) for s in tracker.get("segments", []))


def _read_timetrack() -> dict:
    try:
        return json.loads(TIMETRACK.read_text())
    except Exception:
        return {"active": None, "trackers": {}, "updated": None}


def _migrate_timetrack() -> None:
    """One-time normalization: backfill segment ids and recompute totals.

    The browser reads data/timetrack.json as a static file, so ids must
    already be present in the file for edit/delete to address rows. Run
    once at startup; only rewrites the file if something actually changed.
    """
    if not TIMETRACK.exists():
        return
    state = _read_timetrack()
    changed = False
    for tracker in state.get("trackers", {}).values():
        for seg in tracker.get("segments", []):
            if not seg.get("id"):
                seg["id"] = _new_id()
                changed = True
            # keep seconds consistent with start/end if both present
            try:
                want = _seg_seconds(seg["started_at"], seg["ended_at"])
                if seg.get("seconds") != want:
                    seg["seconds"] = want
                    changed = True
            except Exception:
                pass
        before = tracker.get("total_seconds")
        _recompute_total(tracker)
        if tracker.get("total_seconds") != before:
            changed = True
    if changed:
        _write_timetrack(state)


def _write_timetrack(state: dict) -> None:
    state["updated"] = _now_iso()
    tmp = TIMETRACK.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.rename(TIMETRACK)


def _read_workhours() -> dict:
    try:
        return json.loads(WORKHOURS.read_text())
    except Exception:
        return {"updated": None, "current_year": None, "years": {}, "leave": []}


def _write_workhours(state: dict) -> None:
    state["updated"] = _now_iso()
    tmp = WORKHOURS.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.rename(WORKHOURS)


def _close_active(state: dict) -> None:
    """Close the running segment for the active tracker. Mutates state in-place."""
    active = state.get("active")
    if not active:
        return
    key = active["key"]
    started_str = active["started_at"]
    now = datetime.datetime.now().astimezone()
    try:
        started = datetime.datetime.fromisoformat(started_str)
        seconds = max(0, int((now - started).total_seconds()))
    except Exception:
        seconds = 0
    now_str = now.isoformat(timespec="seconds")
    tracker = state["trackers"].setdefault(
        key, {"summary": key, "total_seconds": 0, "segments": []}
    )
    tracker["segments"].append(
        {"id": _new_id(), "started_at": started_str, "ended_at": now_str, "seconds": seconds}
    )
    tracker["total_seconds"] = tracker.get("total_seconds", 0) + seconds
    state["active"] = None


def _trim_idle_upto(state: dict, boundary: datetime.datetime) -> None:
    """Truncate/remove Idle segments at or after `boundary`.

    Used when a task's start time is set earlier than it was originally
    recorded and now overlaps time already logged as Idle, so that span
    isn't double-counted as both Idle and tracked work.
    """
    idle = state.get("trackers", {}).get(IDLE_KEY)
    if not idle:
        return
    kept = []
    for seg in idle.get("segments", []):
        try:
            seg_start = _parse_dt(seg["started_at"])
            seg_end = _parse_dt(seg["ended_at"])
        except Exception:
            kept.append(seg)
            continue
        if seg_start >= boundary:
            continue  # entirely reclaimed by the task
        if seg_end > boundary:
            seg["ended_at"] = boundary.isoformat(timespec="seconds")
            seg["seconds"] = int((boundary - seg_start).total_seconds())
        kept.append(seg)
    if kept:
        idle["segments"] = kept
        _recompute_total(idle)
    else:
        del state["trackers"][IDLE_KEY]


def _start_tracker(state: dict, key: str, summary: str) -> None:
    """Open a new segment for key. Caller must call _close_active first."""
    now_str = _now_iso()
    state["active"] = {"key": key, "started_at": now_str}
    tracker = state["trackers"].setdefault(
        key, {"summary": summary or key, "total_seconds": 0, "segments": []}
    )
    if summary:
        tracker["summary"] = summary


def _load_secrets():
    token = (ROOT / "secrets" / "bitbucket-token").read_text().strip()
    stash_url = ""
    for line in (ROOT / "secrets" / "config").read_text().splitlines():
        if line.startswith("STASH_URL="):
            stash_url = line.split("=", 1)[1].strip().strip('"')
    return stash_url, token


# ── Jira worklog sync ────────────────────────────────────────────────────────
_jira_cache: dict = {}


def _jira_secrets():
    token = (ROOT / "secrets" / "jira-token").read_text().strip()
    jira_url = ""
    for line in (ROOT / "secrets" / "config").read_text().splitlines():
        if line.startswith("JIRA_URL="):
            jira_url = line.split("=", 1)[1].strip().strip('"')
    return jira_url.rstrip("/"), token


def _config_var(name: str) -> str:
    """Read a simple NAME="value" (or NAME=value) assignment from secrets/config."""
    for line in (ROOT / "secrets" / "config").read_text().splitlines():
        if line.startswith(name + "="):
            val = line.split("=", 1)[1].strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1].replace('\\"', '"')
            return val
    return ""


# ── Backlog page ─────────────────────────────────────────────────────────────
# Used when secrets/config has no JQL_BACKLOG. The org-specific filter (labels,
# excluded epics, test-case issue types) belongs there, not in the repo.
_DEFAULT_BACKLOG_JQL = ("project in ($JIRA_PROJECTS) AND statusCategory != Done "
                        "AND (assignee is EMPTY OR assignee = currentUser()) AND issuetype != Epic")
_BACKLOG_FIELDS = "summary,status,issuetype,priority,labels,assignee,updated,issuelinks,comment,attachment,parent"


def _backlog_filter() -> str:
    """JQL_BACKLOG minus any ORDER BY: the page adds its own sprint clause and
    Rank order."""
    jql = _config_var("JQL_BACKLOG") or _DEFAULT_BACKLOG_JQL
    jql = jql.replace("$JIRA_PROJECTS", _config_var("JIRA_PROJECTS"))
    return re.sub(r"\s+ORDER\s+BY\s.*$", "", jql, flags=re.I | re.S).strip()


def _jira_fields() -> list:
    """Field definitions (id, name, schema). Custom-field ids differ per
    instance, so they're looked up by name. Cached for the process lifetime."""
    if "fields" not in _jira_cache:
        _jira_cache["fields"] = _jira_request("GET", "/rest/api/2/field")
    return _jira_cache["fields"]


def _field_ids(pattern: str) -> list:
    return [f["id"] for f in _jira_fields() if re.search(pattern, f.get("name") or "", re.I)]


def _first_value(fields: dict, ids: list):
    for fid in ids:
        val = fields.get(fid)
        if val not in (None, "", []):
            return val
    return None


def _textarea_fields() -> list:
    """(id, name) of every multi-line custom text field (acceptance criteria,
    steps to reproduce, …), acceptance criteria first."""
    order = (r"accept", r"steps.*repro", r"expected", r"actual")
    rank = lambda name: next((i for i, p in enumerate(order) if re.search(p, name, re.I)), len(order))
    found = [(f["id"], f.get("name") or f["id"]) for f in _jira_fields()
             if str((f.get("schema") or {}).get("custom", "")).endswith(":textarea")]
    return sorted(found, key=lambda f: (rank(f[1]), f[1].lower()))


def _jira_search_all(jql: str, fields: str, **extra) -> list:
    """Every issue matching jql, following startAt paging (Jira caps a page at 1000)."""
    issues = []
    while True:
        data = _jira_request("GET", "/rest/api/2/search", params={
            "jql": jql, "fields": fields, "startAt": len(issues), "maxResults": 500, **extra})
        page = data.get("issues", [])
        issues.extend(page)
        if not page or len(issues) >= data.get("total", 0):
            return issues


def _issues_by_key(keys: list, fields: str) -> dict:
    """{key: fields}, 100 keys per query. validateQuery=false skips keys that
    vanished or aren't visible instead of failing the whole lookup."""
    out = {}
    for i in range(0, len(keys), 100):
        for it in _jira_search_all(f"key in ({','.join(keys[i:i + 100])})", fields, validateQuery="false"):
            out[it["key"]] = it.get("fields") or {}
    return out


def _agile_values(path: str, **params) -> list:
    """All 'values' of a paged Jira Agile endpoint (boards, sprints)."""
    values = []
    while True:
        data = _jira_request("GET", path, params={**params, "startAt": len(values), "maxResults": 50})
        page = data.get("values", [])
        values.extend(page)
        if data.get("isLast", True) or not page:
            return values


def _team_sprints():
    """(board id, active sprints, future sprints) for the team board.

    Found like poll.sh does — first scrum board of the first configured project
    that has an active sprint — but the sprints are then read from the sprint's
    *origin* board. The board found first can be a copy whose filter covers
    fewer projects, and it then lists fewer sprint buckets than really exist.
    """
    first_project = _config_var("JIRA_PROJECTS").split(",")[0].strip()
    for board in _agile_values("/rest/agile/1.0/board", projectKeyOrId=first_project, type="scrum"):
        active = _agile_values(f"/rest/agile/1.0/board/{board['id']}/sprint", state="active")
        if active:
            board_id = active[0].get("originBoardId") or board["id"]
            return board_id, active, _agile_values(f"/rest/agile/1.0/board/{board_id}/sprint", state="future")
    return None, [], []


def _person(p):
    return {"name": p.get("name", ""), "display": p.get("displayName") or p.get("name", "")} if p else None


def _linked(issue: dict) -> dict:
    """A linked issue / subtask / parent as embedded in another issue's fields."""
    f = issue.get("fields") or {}
    status = f.get("status") or {}
    cat = (status.get("statusCategory") or {}).get("key", "")
    return {"key": issue.get("key", ""), "summary": f.get("summary", ""),
            "status": status.get("name", ""), "status_category": cat, "done": cat == "done"}


def _backlog_issue(raw: dict, epic_ids, points_ids, flag_ids) -> dict:
    f = raw.get("fields") or {}
    status = f.get("status") or {}
    comment = f.get("comment") or {}
    parent = f.get("parent")
    return {
        "key": raw["key"],
        "summary": f.get("summary", ""),
        "status": status.get("name", ""),
        "status_category": (status.get("statusCategory") or {}).get("key", ""),
        "type": (f.get("issuetype") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "labels": f.get("labels") or [],
        "assignee": _person(f.get("assignee")),
        "story_points": _first_value(f, points_ids),
        "epic": _first_value(f, epic_ids),
        "parent": {"key": parent["key"], "summary": _linked(parent)["summary"]} if parent else None,
        "flagged": bool(_first_value(f, flag_ids)),
        "blocked_by": [_linked(link["inwardIssue"]) for link in f.get("issuelinks") or []
                       if link.get("inwardIssue") and (link.get("type") or {}).get("inward") == "is blocked by"],
        "comments": comment.get("total") or len(comment.get("comments") or []),
        "attachments": len(f.get("attachment") or []),
        "updated": f.get("updated", ""),
    }


def _fetch_backlog() -> dict:
    """The backlog page, top to bottom: the active sprint, the next two dated
    sprints, then the rest of the backlog — later sprints, the board's undated
    placeholder "X Backlog" buckets, and issues in no open/future sprint.
    Every section is filtered by JQL_BACKLOG and ordered by Rank, like Jira's
    own backlog view."""
    jira_url, _ = _jira_secrets()
    base = _backlog_filter()
    epic_ids = _field_ids(r"^epic\s*link$")
    points_ids = _field_ids(r"^story\s*points?$")
    flag_ids = _field_ids(r"^flagged$")
    fields = ",".join([_BACKLOG_FIELDS, *epic_ids, *points_ids, *flag_ids])
    warnings = []

    try:
        board_id, active, future = _team_sprints()
    except Exception as e:
        board_id, active, future = None, [], []
        warnings.append(f"Couldn't read the team board's sprints ({e}) — showing one flat backlog.")
    dated = sorted((s for s in future if s.get("startDate")), key=lambda s: s["startDate"])
    upcoming, later = dated[:2], dated[2:] + [s for s in future if not s.get("startDate")]

    sections, jobs = [], []

    def section(kind, title, sprint=None):
        sprint = sprint or {}
        sec = {"id": f"sprint-{sprint['id']}" if sprint else kind, "kind": kind, "title": title,
               "start": (sprint.get("startDate") or "")[:10] or None,
               "end": (sprint.get("endDate") or "")[:10] or None,
               "goal": (sprint.get("goal") or "").strip(), "groups": []}
        sections.append(sec)
        return sec

    def group(sec, title, clause):
        g = {"title": title, "issues": []}
        sec["groups"].append(g)
        jobs.append((sec, g, f"({base}){f' AND {clause}' if clause else ''} ORDER BY Rank ASC"))

    for s in active:
        group(section("active", s.get("name") or "Active sprint", s), None, f"sprint = {s['id']}")
    for s in upcoming:
        group(section("future", s.get("name") or "Next sprint", s), None, f"sprint = {s['id']}")
    rest = section("rest", "Rest of the backlog" if board_id else "Backlog")
    for s in later:
        group(rest, s.get("name") or f"Sprint {s['id']}", f"sprint = {s['id']}")
    # Deliberately a catch-all, and deliberately last: `seen` has already claimed
    # everything the sprint groups matched, so anything else the filter matches
    # lands here instead of silently vanishing (a sprint bucket living on another
    # board, say) — the page can't show fewer issues than the filter matches.
    group(rest, "Not in a sprint / other" if board_id else None, None)

    def run(job):
        try:
            return _jira_search_all(job[2], fields), None
        except urllib.error.HTTPError as e:
            return [], _http_err_msg(e)
        except Exception as e:
            return [], str(e)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(run, jobs))

    seen, issues = set(), []
    for (sec, g, _), (raw, err) in zip(jobs, results):
        if err:
            warnings.append(f"{g['title'] or sec['title']}: {err}")
        for it in raw:
            if it["key"] not in seen:  # an issue lives in the first section that claims it
                seen.add(it["key"])
                issue = _backlog_issue(it, epic_ids, points_ids, flag_ids)
                g["issues"].append(issue)
                issues.append(issue)

    # Sub-tasks carry no Epic Link of their own; they belong to their parent's epic.
    orphans = sorted({i["parent"]["key"] for i in issues if i["parent"] and not i["epic"]})
    if orphans and epic_ids:
        try:
            parents = _issues_by_key(orphans, ",".join(epic_ids))
            for i in issues:
                if i["parent"] and not i["epic"]:
                    i["epic"] = _first_value(parents.get(i["parent"]["key"], {}), epic_ids)
        except Exception as e:
            warnings.append(f"Couldn't look up sub-task parents' epics ({e}).")

    epics = {}
    epic_keys = sorted({i["epic"] for i in issues if i["epic"]})
    if epic_keys:
        name_ids, color_ids = _field_ids(r"^epic\s*name$"), _field_ids(r"^epic\s*colou?r$")
        try:
            for k, ef in _issues_by_key(epic_keys, ",".join(["summary", *name_ids, *color_ids])).items():
                epics[k] = {"key": k, "summary": ef.get("summary", ""),
                            "name": _first_value(ef, name_ids) or ef.get("summary", ""),
                            "color": _first_value(ef, color_ids) or ""}
        except Exception as e:
            warnings.append(f"Couldn't look up epic names ({e}).")

    try:
        me = _my_jira_username()
    except Exception:
        me = ""
    return {
        "jira_url": jira_url, "me": me, "board_id": board_id,
        "board_url": f"{jira_url}/secure/RapidBoard.jspa?rapidView={board_id}&view=planning" if board_id else "",
        "sections": sections, "epics": epics, "warnings": warnings,
    }


def _sprint_names(values) -> list:
    """Sprint field values -> ["Sprint 6 - Team A_PI27 (future)", …]. API v2
    returns strings like "…Sprint@1f[id=7042,…,name=Sprint 6,rapidViewId=655,…,state=FUTURE,…]"."""
    out = []
    for v in values or []:
        if isinstance(v, dict):
            name, state = v.get("name"), v.get("state")
        else:
            m_name = re.search(r"name=(.*?),\w+=", str(v))
            m_state = re.search(r"state=(\w+)", str(v))
            name, state = m_name and m_name.group(1), m_state and m_state.group(1)
        if name:
            out.append(f"{name} ({state.lower()})" if state else name)
    return out


def _proxy_url(url: str) -> str:
    return "/api/jira-file?path=" + urllib.parse.quote(urllib.parse.urlparse(url or "").path, safe="")


def _issue_detail(key: str) -> dict:
    """Everything the backlog page's expanded row shows, with Jira's own
    rendered HTML for description / text fields / comments."""
    data = _jira_request("GET", f"/rest/api/2/issue/{key}", params={"expand": "renderedFields"})
    f, rendered = data.get("fields") or {}, data.get("renderedFields") or {}
    epic_ids = _field_ids(r"^epic\s*link$")
    status = f.get("status") or {}
    parent = f.get("parent")
    epic = _first_value(f, epic_ids)
    if not epic and parent and epic_ids:
        try:
            epic = _first_value(_jira_request("GET", f"/rest/api/2/issue/{parent['key']}",
                                              params={"fields": ",".join(epic_ids)}).get("fields") or {}, epic_ids)
        except Exception:
            pass

    text_fields = [{"name": name, "html": rendered.get(fid) or f"<p>{html.escape(str(f[fid]))}</p>"}
                   for fid, name in _textarea_fields() if f.get(fid) not in (None, "")]

    links = []
    for link in f.get("issuelinks") or []:
        t = link.get("type") or {}
        if link.get("inwardIssue"):
            links.append({**_linked(link["inwardIssue"]), "relation": t.get("inward", "")})
        elif link.get("outwardIssue"):
            links.append({**_linked(link["outwardIssue"]), "relation": t.get("outward", "")})
    links.sort(key=lambda x: (x["relation"] != "is blocked by", x["relation"]))

    comment = f.get("comment") or {}
    rendered_bodies = {c.get("id"): c.get("body") for c in (rendered.get("comment") or {}).get("comments") or []}
    comments = [{"author": (c.get("author") or {}).get("displayName", "?"),
                 "created": c.get("created", ""), "updated": c.get("updated", ""),
                 "html": rendered_bodies.get(c.get("id")) or f"<p>{html.escape(c.get('body') or '')}</p>"}
                for c in comment.get("comments") or []]

    attachments = []
    for a in f.get("attachment") or []:
        is_img = (a.get("mimeType") or "").startswith("image/")
        attachments.append({
            "filename": a.get("filename", ""), "size": a.get("size") or 0, "mime": a.get("mimeType", ""),
            "created": a.get("created", ""), "author": (a.get("author") or {}).get("displayName", ""),
            "url": a.get("content", ""),
            "img": _proxy_url(a.get("content")) if is_img else None,
            "thumb": _proxy_url(a.get("thumbnail")) if is_img and a.get("thumbnail") else None,
        })

    return {
        "key": data.get("key", key), "summary": f.get("summary", ""),
        "status": status.get("name", ""),
        "status_category": (status.get("statusCategory") or {}).get("key", ""),
        "resolution": (f.get("resolution") or {}).get("name", ""),
        "type": (f.get("issuetype") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "assignee": _person(f.get("assignee")), "reporter": _person(f.get("reporter")),
        "created": f.get("created", ""), "updated": f.get("updated", ""), "duedate": f.get("duedate") or "",
        "labels": f.get("labels") or [],
        "components": [c.get("name", "") for c in f.get("components") or []],
        "fix_versions": [v.get("name", "") for v in f.get("fixVersions") or []],
        "affects_versions": [v.get("name", "") for v in f.get("versions") or []],
        "sprints": _sprint_names(_first_value(f, _field_ids(r"^sprint$"))),
        "story_points": _first_value(f, _field_ids(r"^story\s*points?$")),
        "epic": epic,
        "parent": {"key": parent["key"], "summary": _linked(parent)["summary"]} if parent else None,
        "description_html": rendered.get("description") or "",
        "text_fields": text_fields, "links": links,
        "subtasks": [_linked(s) for s in f.get("subtasks") or []],
        "attachments": attachments,
        "comments": comments, "comments_total": comment.get("total", len(comments)),
    }


# ── Jira image proxy ─────────────────────────────────────────────────────────
# The browser has no Jira token and cross-site cookies aren't sent with <img>,
# so images in rendered Jira HTML are fetched through here.
_IMAGE_LIMIT = 25 * 1024 * 1024


def _jira_image_path(path: str) -> str:
    """Canonical Jira path for an image the page may show, else ValueError.
    Attachment/thumbnail paths are rebuilt from the numeric id alone (Jira
    ignores the filename part), so the proxy can't be steered at any other
    Jira URL with our token."""
    m = re.match(r"^/secure/(attachment|thumbnail)/(\d+)(?:/|$)", path or "")
    if m:
        kind, aid = m.groups()
        return f"/secure/attachment/{aid}/" if kind == "attachment" else f"/secure/thumbnail/{aid}/_thumb_{aid}.png"
    if re.fullmatch(r"/images/icons/[\w-]+(?:/[\w-]+)*\.(?:png|gif|jpe?g|svg)", path or ""):
        return path
    raise ValueError("not a Jira image path")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Jira answers a failed attachment fetch with a 302 to its login page


def _fetch_jira_image(path: str):
    """(content type, bytes). A failed fetch comes back from Jira as a redirect
    to an HTML login page, so anything that isn't a direct image is refused."""
    base, token = _jira_secrets()
    req = urllib.request.Request(base + _jira_image_path(path), headers={"Authorization": f"Bearer {token}"})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=20) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        body = resp.read(_IMAGE_LIMIT + 1)
    if not ctype.startswith("image/"):
        raise ValueError(f"not an image ({ctype or 'unknown type'})")
    if len(body) > _IMAGE_LIMIT:
        raise ValueError("image too large")
    return ctype, body


def _jira_request(method: str, path: str, body=None, params=None):
    base, token = _jira_secrets()
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _http_err_msg(err: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(err.read())
        msgs = body.get("errorMessages") or []
        if not msgs and body.get("errors"):
            msgs = [str(body["errors"])]
        if msgs:
            return f"{err.code}: " + "; ".join(msgs)
    except Exception:
        pass
    return f"HTTP {err.code}"


def _text_field_by_name(fields: dict, names: dict, pattern: str) -> str:
    """First non-empty text field whose *display name* matches pattern.

    Custom-field ids differ per Jira instance, so these are looked up by the
    human-readable name (which also lets EN/DA labels both match).
    """
    for fid, fname in names.items():
        if re.search(pattern, fname or "", re.I):
            val = fields.get(fid)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _issue_comments_text(fields: dict) -> str:
    """Comments as 'Author (date): body' blocks, oldest first."""
    blocks = []
    for c in ((fields.get("comment") or {}).get("comments") or []):
        body = (c.get("body") or "").strip()
        if not body:
            continue
        who = ((c.get("author") or {}).get("displayName") or "?").strip()
        when = (c.get("created") or "")[:10]
        blocks.append(f"**{who}** ({when}):\n{body}")
    return "\n\n".join(blocks)


def _issue_prompt_text(key: str) -> str:
    """Issue summary, bug-report fields, description and comments as plain text
    ready to paste into a prompt. Bug fields are only emitted when present, so
    stories come out the same as before."""
    data = _jira_request("GET", f"/rest/api/2/issue/{key}", params={"expand": "names"})
    fields = data.get("fields", {})
    names = data.get("names", {})            # field id -> display name
    summary = (fields.get("summary") or "").strip()
    description = (fields.get("description") or "").strip()

    sections = [
        ("Acceptance Criteria", _text_field_by_name(fields, names, r"accept")),
        ("Steps to Reproduce",  _text_field_by_name(fields, names, r"steps.*repro")),
        ("Expected Results",    _text_field_by_name(fields, names, r"expected\s*result")),
        ("Actual Results",      _text_field_by_name(fields, names, r"actual\s*result")),
        ("Description",         description),
        ("Comments",            _issue_comments_text(fields)),
    ]
    parts = [f"{key}: {summary}" if summary else key]
    parts += [f"## {title}\n{body}" for title, body in sections if body]
    return "\n\n".join(parts)


def _my_jira_username() -> str:
    if "myself" not in _jira_cache:
        _jira_cache["myself"] = _jira_request("GET", "/rest/api/2/myself").get("name", "")
    return _jira_cache["myself"]


def _parse_jira_dt(s: str) -> datetime.datetime:
    """Jira Server worklog 'started' format: 2026-06-04T07:53:58.000+0200."""
    try:
        return datetime.datetime.strptime((s or "").strip(), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return _parse_dt(s)


def _jira_started_fmt(iso: str, date_str: str) -> str:
    try:
        dt = _parse_dt(iso)
    except Exception:
        dt = datetime.datetime.fromisoformat(date_str + "T12:00:00").astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _my_jira_worklog_days(key: str) -> dict:
    """Local date -> total seconds of worklogs authored by me on that issue."""
    data = _jira_request("GET", f"/rest/api/2/issue/{key}/worklog")
    me = _my_jira_username()
    days: dict = {}
    for w in data.get("worklogs", []):
        if ((w.get("author") or {}).get("name", "")) != me:
            continue
        try:
            d = _parse_jira_dt(w.get("started", "")).astimezone().strftime("%Y-%m-%d")
        except Exception:
            continue
        days[d] = days.get(d, 0) + int(w.get("timeSpentSeconds", 0))
    return days


def _round_quarter(seconds: int) -> int:
    """Round to the nearest 15 min: up if >=5 min past a quarter, else down.
    Any block with tracked time bills at least 15 min. Returns seconds."""
    m = int(seconds) // 60
    if m <= 0:
        return 0
    r = m % 15
    m = m - r if r < 5 else m + (15 - r)
    return max(15, m) * 60


def _read_ledger() -> dict:
    try:
        return json.loads(WORKLOG_LEDGER.read_text())
    except Exception:
        return {"written": {}}


def _write_ledger(ledger: dict) -> None:
    tmp = WORKLOG_LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
    tmp.rename(WORKLOG_LEDGER)


def _daily_totals(state: dict, start: str, end: str):
    """(key, date) -> {seconds, first_start} for closed segments in [start, end].

    Segments credit their start day, matching the UI's day panel. Returns the
    totals plus the list of non-Jira keys that had time in range (excluded).
    """
    totals: dict = {}
    invalid = set()
    for key, tracker in (state.get("trackers") or {}).items():
        if key == IDLE_KEY:
            continue
        valid = bool(JIRA_KEY_RE.match(key))
        for seg in tracker.get("segments", []):
            try:
                d = _parse_dt(seg["started_at"]).strftime("%Y-%m-%d")
            except Exception:
                continue
            if d < start or d > end:
                continue
            if not valid:
                invalid.add(key)
                break
            ent = totals.setdefault((key, d), {"seconds": 0, "first_start": seg["started_at"]})
            ent["seconds"] += int(seg.get("seconds", 0))
            if seg["started_at"] < ent["first_start"]:
                ent["first_start"] = seg["started_at"]
    return totals, sorted(invalid)


def _worklog_status(start: str, end: str) -> dict:
    """Per (issue, day) sync state between local tracked time, the ledger and Jira."""
    state = _read_timetrack()
    totals, invalid_keys = _daily_totals(state, start, end)
    written = _read_ledger().get("written", {})

    pairs = set(totals.keys())
    for lk in written:
        key, _, d = lk.partition("|")
        if start <= d <= end:
            pairs.add((key, d))

    jira_days: dict = {}
    jira_errors: dict = {}
    for key in sorted({k for k, _ in pairs}):
        try:
            jira_days[key] = _my_jira_worklog_days(key)
        except urllib.error.HTTPError as e:
            jira_errors[key] = _http_err_msg(e)
        except Exception as e:
            jira_errors[key] = str(e)

    entries = []
    for key, d in sorted(pairs, key=lambda p: (p[1], p[0])):
        t = totals.get((key, d))
        local = t["seconds"] if t else 0
        rounded = _round_quarter(local)
        lentry = written.get(f"{key}|{d}")
        if lentry is None and rounded == 0:
            continue  # under 3 minutes and never written — nothing to do
        jira_secs = jira_days.get(key, {}).get(d, 0)
        if key in jira_errors:
            st = "error"
        elif lentry:
            if rounded == 0:
                st = "orphaned"
            elif int(lentry.get("seconds", -1)) == rounded:
                st = "synced"
            else:
                st = "changed"
        else:
            st = "conflict" if jira_secs > 0 else "unwritten"
        entry = {
            "key": key, "date": d,
            "local_seconds": local, "rounded_seconds": rounded,
            "ledger_seconds": (lentry or {}).get("seconds"),
            "jira_seconds": jira_secs, "state": st,
        }
        if t:
            entry["first_start"] = t["first_start"]
        if key in jira_errors:
            entry["error"] = jira_errors[key]
        entries.append(entry)

    active = state.get("active")
    active_info = None
    if active and active.get("key") != IDLE_KEY:
        try:
            ad = _parse_dt(active["started_at"]).strftime("%Y-%m-%d")
            if start <= ad <= end:
                active_info = {"key": active["key"], "date": ad}
        except Exception:
            pass

    return {"start": start, "end": end, "entries": entries,
            "invalid_keys": invalid_keys, "active": active_info}


def _worklog_write(start: str, end: str) -> dict:
    """Idempotent write: create unwritten days, update changed ones, skip the rest.

    The ledger is rewritten after every successful Jira call so a crash mid-run
    can at worst lose the last response — which the conflict guard then catches.
    """
    status = _worklog_status(start, end)
    ledger = _read_ledger()
    written = ledger.setdefault("written", {})
    results = []
    for e in status["entries"]:
        key, d, rounded = e["key"], e["date"], e["rounded_seconds"]
        lk = f"{key}|{d}"
        if e["state"] == "synced":
            results.append(dict(e, action="skipped"))
        elif e["state"] == "unwritten":
            started_fmt = _jira_started_fmt(e.get("first_start", ""), d)
            try:
                resp = _jira_request(
                    "POST", f"/rest/api/2/issue/{key}/worklog",
                    body={"started": started_fmt, "timeSpentSeconds": rounded},
                    params={"adjustEstimate": "leave"})
                written[lk] = {"worklog_id": str(resp.get("id", "")), "seconds": rounded,
                               "started": started_fmt, "written_at": _now_iso()}
                _write_ledger(ledger)
                results.append(dict(e, action="created"))
            except urllib.error.HTTPError as err:
                results.append(dict(e, action="error", error=_http_err_msg(err)))
            except Exception as err:
                results.append(dict(e, action="error", error=str(err)))
        elif e["state"] == "changed":
            lentry = written.get(lk) or {}
            wid = lentry.get("worklog_id", "")
            started_fmt = lentry.get("started") or _jira_started_fmt(e.get("first_start", ""), d)
            try:
                _jira_request(
                    "PUT", f"/rest/api/2/issue/{key}/worklog/{wid}",
                    body={"started": started_fmt, "timeSpentSeconds": rounded},
                    params={"adjustEstimate": "leave"})
                written[lk] = dict(lentry, seconds=rounded, started=started_fmt,
                                   written_at=_now_iso())
                _write_ledger(ledger)
                results.append(dict(e, action="updated"))
            except urllib.error.HTTPError as err:
                results.append(dict(e, action="error", error=_http_err_msg(err)))
            except Exception as err:
                results.append(dict(e, action="error", error=str(err)))
        else:  # conflict, orphaned, error — never write
            results.append(dict(e, action=e["state"]))
    return {"start": start, "end": end, "results": results,
            "invalid_keys": status["invalid_keys"], "active": status["active"]}


_SEV_RANK = {"BLOCKER": 0, "CRITICAL": 1, "HIGH": 1, "MAJOR": 2, "MEDIUM": 2,
             "MINOR": 3, "LOW": 3, "INFO": 4}
_TYPE_SHORT = {"BUG": "BUG", "VULNERABILITY": "VULN", "CODE_SMELL": "SMELL"}
# Map Clean-Code (MQR) impact severities back to a classic label for display.
_IMPACT_SEV = {"HIGH": "CRITICAL", "MEDIUM": "MAJOR", "LOW": "MINOR",
               "BLOCKER": "BLOCKER", "INFO": "INFO"}


def _sonar_get(url: str, headers: dict) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
        return json.loads(resp.read())


def _issue_severity(iss: dict) -> str:
    """Severity label, tolerant of both classic and Clean-Code (impacts) modes."""
    if iss.get("severity"):
        return iss["severity"]
    impacts = iss.get("impacts") or []
    if impacts:
        sev = (impacts[0].get("severity") or "").upper()
        return _IMPACT_SEV.get(sev, sev or "?")
    return "?"


def _component_paths(data: dict, project_key: str) -> dict:
    """Map component key -> shortest readable path."""
    out = {}
    for c in data.get("components", []):
        path = c.get("path") or c.get("longName") or c.get("key", "")
        if path.startswith(project_key + ":"):
            path = path.split(":", 1)[1]
        out[c.get("key", "")] = path
    return out


def _fetch_sonar_issues(pr_id: str) -> str:
    sonar_token = (ROOT / "secrets" / "sonar-token").read_text().strip()
    bb = json.loads((DATA_DIR / "bitbucket.json").read_text())
    all_prs = bb.get("my_prs", []) + bb.get("reviewer_prs", [])
    pr = next((p for p in all_prs if str(p.get("id")) == str(pr_id)), None)
    if pr is None:
        raise KeyError(f"PR #{pr_id} not found in data/bitbucket.json")

    sonar_url = pr.get("sonar_url", "")
    if not sonar_url:
        raise ValueError(f"No SonarQube URL for PR #{pr_id}")

    parsed = urllib.parse.urlparse(sonar_url)
    sonar_base = f"{parsed.scheme}://{parsed.netloc}"
    qs = urllib.parse.parse_qs(parsed.query)
    project_key = (qs.get("id") or [None])[0]
    branch = (qs.get("branch") or [None])[0] or pr.get("branch", "")

    if not project_key:
        raise ValueError(f"Cannot parse project key from sonar_url: {sonar_url}")

    headers = {"Authorization": f"Bearer {sonar_token}"}

    # Fetch quality gate conditions (reliable — scoped to project+branch)
    qg_params: dict = {"projectKey": project_key}
    if branch:
        qg_params["branch"] = branch
    qg_url = f"{sonar_base}/api/qualitygates/project_status?" + urllib.parse.urlencode(qg_params)
    with urllib.request.urlopen(urllib.request.Request(qg_url, headers=headers), timeout=15) as resp:
        qg_data = json.loads(resp.read())

    conditions = qg_data.get("projectStatus", {}).get("conditions", [])
    failing = [c for c in conditions if c.get("status") == "ERROR"]

    lines = [
        f"PR #{pr['id']}: {pr['repo']}",
        f"Branch: {pr.get('branch', '')}",
        f"Quality Gate: {pr.get('qg_label', '?')}",
        "",
        f"Failing conditions ({len(failing)}):",
    ]
    metric_labels = {
        "new_violations": "New violations",
        "new_bugs": "New bugs",
        "new_vulnerabilities": "New vulnerabilities",
        "new_security_hotspots": "New security hotspots",
        "new_code_smells": "New code smells",
        "new_coverage": "New coverage",
        "new_duplicated_lines_density": "New duplications",
        "reliability_rating": "Reliability rating",
        "security_rating": "Security rating",
        "sqale_rating": "Maintainability rating",
    }
    comparator_labels = {"GT": ">", "LT": "<", "EQ": "=", "NE": "≠"}
    for c in failing:
        metric = c.get("metricKey", "")
        label = metric_labels.get(metric, metric)
        cmp = comparator_labels.get(c.get("comparator", ""), c.get("comparator", ""))
        threshold = c.get("errorThreshold", "?")
        actual = c.get("actualValue", "?")
        lines.append(f"  {label}: {actual} (threshold: {cmp} {threshold})")
    if not failing:
        lines.append("  (none)")

    # ── Actual issues on new code ────────────────────────────────────────
    # The QG section above only gives counts; this lists each issue so it can
    # be copied without opening SonarQube.
    common = {"branch": branch} if branch else {}
    try:
        iss_params = {
            "componentKeys": project_key,
            "inNewCodePeriod": "true",
            "statuses": "OPEN,CONFIRMED,REOPENED",
            "resolved": "false",
            "ps": "100",
            **common,
        }
        iss_url = f"{sonar_base}/api/issues/search?" + urllib.parse.urlencode(iss_params)
        iss_data = _sonar_get(iss_url, headers)
        issues = iss_data.get("issues", [])
        total = iss_data.get("total", len(issues))
        paths = _component_paths(iss_data, project_key)
        issues.sort(key=lambda i: (_SEV_RANK.get(_issue_severity(i).upper(), 9),
                                   paths.get(i.get("component", ""), ""), i.get("line") or 0))
        shown = f" (showing {len(issues)} of {total})" if total > len(issues) else ""
        lines += ["", f"Issues on new code ({total}){shown}:"]
        if not issues:
            lines.append("  (none)")
        for i in issues:
            sev = _issue_severity(i)
            typ = _TYPE_SHORT.get(i.get("type", ""), i.get("type", ""))
            loc = paths.get(i.get("component", ""), i.get("component", ""))
            if i.get("line"):
                loc += f":{i['line']}"
            msg = (i.get("message") or "").strip()
            rule = i.get("rule", "")
            lines.append(f"  [{sev}/{typ}] {loc}")
            lines.append(f"      {msg}  ({rule})")
    except Exception as e:
        lines += ["", f"Issues on new code: (could not fetch — {e})"]

    # ── Security hotspots to review ──────────────────────────────────────
    try:
        hs_params = {
            "projectKey": project_key,
            "status": "TO_REVIEW",
            "inNewCodePeriod": "true",
            "ps": "100",
            **common,
        }
        hs_url = f"{sonar_base}/api/hotspots/search?" + urllib.parse.urlencode(hs_params)
        hs_data = _sonar_get(hs_url, headers)
        hotspots = hs_data.get("hotspots", [])
        hpaths = _component_paths(hs_data, project_key)
        if hotspots:
            lines += ["", f"Security hotspots to review ({len(hotspots)}):"]
            for h in hotspots:
                prob = (h.get("vulnerabilityProbability") or "?").upper()
                loc = hpaths.get(h.get("component", ""), h.get("component", ""))
                if h.get("line"):
                    loc += f":{h['line']}"
                msg = (h.get("message") or "").strip()
                rule = h.get("ruleKey", "")
                cat = h.get("securityCategory", "")
                lines.append(f"  [HOTSPOT/{prob}] {loc}")
                lines.append(f"      {msg}  ({cat} · {rule})")
    except Exception as e:
        lines += ["", f"Security hotspots: (could not fetch — {e})"]

    lines += ["", f"View in SonarQube: {sonar_url}"]
    return "\n".join(lines)


def _fetch_pr_comments(pr_id: str) -> str:
    stash_url, token = _load_secrets()
    headers = {"Authorization": f"Bearer {token}"}

    bb = json.loads((DATA_DIR / "bitbucket.json").read_text())
    all_prs = bb.get("my_prs", []) + bb.get("reviewer_prs", [])
    pr = next((p for p in all_prs if str(p.get("id")) == str(pr_id)), None)
    if pr is None:
        raise KeyError(f"PR #{pr_id} not found in data/bitbucket.json")

    project, slug = pr["project"], pr["slug"]

    activities = []
    start = 0
    while True:
        url = (
            f"{stash_url}/rest/api/1.0/projects/{project}/repos/{slug}"
            f"/pull-requests/{pr_id}/activities?limit=100&start={start}"
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        activities.extend(data.get("values", []))
        if data.get("isLastPage", True):
            break
        start = data.get("nextPageStart", 0)

    threads = [a for a in activities
               if a.get("action") == "COMMENTED"
               and a.get("comment")
               and a["comment"].get("state") != "RESOLVED"
               and a["comment"].get("threadResolved") is not True]

    lines = [
        f"# PR #{pr['id']}: {pr['title']}",
        f"# Repo: {pr['repo']}  |  Branch: {pr.get('branch', '')}",
        f"# Author: {pr['author']}",
        f"# {len(threads)} comment thread(s)",
        "",
    ]

    for act in threads:
        comment = act["comment"]
        anchor = act.get("commentAnchor") or {}
        path = anchor.get("path", "")
        line_no = anchor.get("line")

        loc = (f"File: {path}" + (f" (line {line_no})" if line_no else "")) if path else "General"
        lines.append(f"\n---\n## {loc}")

        author = comment.get("author") or {}
        name = author.get("displayName") or author.get("name", "Unknown")
        tags = (" [BLOCKER]" if comment.get("severity") == "BLOCKER" else "") + \
               (" [RESOLVED]" if comment.get("state") == "RESOLVED" else "")
        lines.append(f"\n**{name}**{tags}:\n{comment['text']}")

        for reply in comment.get("comments", []):
            rauthor = reply.get("author") or {}
            rname = rauthor.get("displayName") or rauthor.get("name", "Unknown")
            rtag = " [RESOLVED]" if reply.get("state") == "RESOLVED" else ""
            lines.append(f"  > **{rname}**{rtag}: {reply['text']}")

    return "\n".join(lines)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/comments"):
            self._handle_comments()
            return
        if self.path.startswith("/api/sonar-issues"):
            self._handle_sonar_issues()
            return
        if self.path.startswith("/api/backlog"):
            self._handle_backlog()
            return
        if self.path.startswith("/api/issue-detail"):
            self._handle_issue_detail()
            return
        if self.path.startswith("/api/jira-file"):
            self._handle_jira_file()
            return
        if self.path.startswith("/api/worklog-status"):
            self._handle_worklog_status()
            return
        if self.path.startswith("/api/issue-text"):
            self._handle_issue_text()
            return
        if self.path.startswith("/data/") and self.path.endswith(".json"):
            if TEST_MODE:
                self._serve_test_data()
                return
            try:
                ACTIVE.touch()
            except OSError:
                pass
        super().do_GET()

    def _serve_test_data(self):
        filename = pathlib.Path(urllib.parse.urlparse(self.path).path).name
        file_path = DATA_DIR / filename
        try:
            content = file_path.read_bytes()
        except FileNotFoundError:
            self._respond(404, f"Test data not found: {filename}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_comments(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pr_id = (qs.get("pr_id") or [None])[0]
        if not pr_id:
            self._respond(400, "Missing pr_id parameter")
            return
        try:
            body = _fetch_pr_comments(pr_id).encode()
        except KeyError as e:
            self._respond(404, str(e))
            return
        except Exception as e:
            self._respond(502, f"Bitbucket error: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_issue_text(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        key = (qs.get("key") or [""])[0]
        if not JIRA_KEY_RE.match(key or ""):
            self._respond(400, "Invalid or missing key")
            return
        try:
            body = _issue_prompt_text(key).encode()
        except urllib.error.HTTPError as e:
            self._respond(502, _http_err_msg(e))
            return
        except Exception as e:
            self._respond(502, f"Jira error: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_backlog(self):
        try:
            self._respond_json(200, _fetch_backlog())
        except urllib.error.HTTPError as e:
            self._respond(502, _http_err_msg(e))
        except Exception as e:
            self._respond(502, f"Jira error: {e}")

    def _handle_issue_detail(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        key = (qs.get("key") or [""])[0]
        if not JIRA_KEY_RE.match(key):
            self._respond(400, "Invalid or missing key")
            return
        try:
            self._respond_json(200, _issue_detail(key))
        except urllib.error.HTTPError as e:
            self._respond(404 if e.code == 404 else 502, _http_err_msg(e))
        except Exception as e:
            self._respond(502, f"Jira error: {e}")

    def _handle_jira_file(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            ctype, body = _fetch_jira_image((qs.get("path") or [""])[0])
        except ValueError as e:
            self._respond(400, str(e))
            return
        except urllib.error.HTTPError as e:
            self._respond(404 if e.code < 500 else 502, f"Jira: HTTP {e.code}")
            return
        except Exception as e:
            self._respond(502, f"Jira error: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox")
        self.end_headers()
        self.wfile.write(body)

    def _handle_sonar_issues(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pr_id = (qs.get("pr_id") or [None])[0]
        if not pr_id:
            self._respond(400, "Missing pr_id parameter")
            return
        try:
            body = _fetch_sonar_issues(pr_id).encode()
        except KeyError as e:
            self._respond(404, str(e))
            return
        except ValueError as e:
            self._respond(400, str(e))
            return
        except Exception as e:
            self._respond(502, f"SonarQube error: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/track":
            self._handle_track()
            return
        if self.path == "/api/worklog-write":
            self._handle_worklog_write()
            return
        if self.path == "/api/leave":
            self._handle_leave()
            return
        if self.path == "/api/poll":
            self._handle_poll()
            return
        if self.path == "/api/poll-copilot":
            self._handle_poll_copilot()
            return
        self._respond(404, "Not found")

    def _handle_poll(self):
        """Run one poll.sh cycle on demand (manual REFRESH) and report the result."""
        script = ROOT / "scripts" / "poll.sh"
        try:
            proc = subprocess.run(
                ["bash", str(script)],
                env={**os.environ, "POLL_ONCE": "1"},
                capture_output=True, text=True, timeout=120)
            ok = proc.returncode == 0
            self._respond_json(200 if ok else 502, {
                "ok": ok, "code": proc.returncode,
                "log": (proc.stdout or "")[-2000:],
                "err": (proc.stderr or "")[-1000:]})
        except subprocess.TimeoutExpired:
            self._respond_json(504, {"ok": False, "error": "poll timed out"})
        except Exception as e:
            self._respond_json(502, {"ok": False, "error": str(e)})

    def _handle_poll_copilot(self):
        """Run one poll-copilot.sh cycle on demand (manual REFRESH) and report the result."""
        script = ROOT / "scripts" / "poll-copilot.sh"
        try:
            proc = subprocess.run(
                ["bash", str(script)],
                capture_output=True, text=True, timeout=60)
            ok = proc.returncode == 0
            self._respond_json(200 if ok else 502, {
                "ok": ok, "code": proc.returncode,
                "log": (proc.stdout or "")[-2000:],
                "err": (proc.stderr or "")[-1000:]})
        except subprocess.TimeoutExpired:
            self._respond_json(504, {"ok": False, "error": "poll timed out"})
        except Exception as e:
            self._respond_json(502, {"ok": False, "error": str(e)})

    def _handle_worklog_status(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        start = (qs.get("start") or [""])[0]
        end = (qs.get("end") or [""])[0]
        if not _DATE_RE.match(start) or not _DATE_RE.match(end) or end < start:
            self._respond(400, "Invalid start/end (expected YYYY-MM-DD)")
            return
        try:
            self._respond_json(200, _worklog_status(start, end))
        except Exception as e:
            self._respond(502, f"Jira error: {e}")

    def _handle_worklog_write(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            self._respond(400, "Invalid JSON")
            return
        start, end = req.get("start", ""), req.get("end", "")
        if not _DATE_RE.match(start) or not _DATE_RE.match(end) or end < start:
            self._respond(400, "Invalid start/end (expected YYYY-MM-DD)")
            return
        try:
            self._respond_json(200, _worklog_write(start, end))
        except Exception as e:
            self._respond(502, f"Jira error: {e}")

    def _handle_leave(self):
        """Add or delete leave records in work-hours.json.

        Body: {"action": "add", "entries": [{date, type, hours, note?}, ...]}
              {"action": "delete", "id": "<leave id>"}
        Vacation is bonusable; sick/other leave is not (the browser applies
        that rule when computing bonus). The server only persists records.
        """
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            self._respond(400, "Invalid JSON")
            return

        action = req.get("action", "")
        state = _read_workhours()
        leave = state.setdefault("leave", [])

        if action == "add":
            added = []
            for e in req.get("entries", []) or []:
                date = str(e.get("date", ""))
                if not _DATE_RE.match(date):
                    continue
                typ = e.get("type", "vacation")
                if typ not in LEAVE_TYPES:
                    typ = "other"
                try:
                    hours = round(float(e.get("hours", 7.4)), 2)
                except (TypeError, ValueError):
                    continue
                if hours < 0:
                    continue
                rec = {
                    "id": _new_id(),
                    "date": date,
                    "type": typ,
                    "hours": hours,
                    "note": str(e.get("note", ""))[:200],
                }
                leave.append(rec)
                added.append(rec)
            leave.sort(key=lambda x: x["date"])
            _write_workhours(state)
            self._respond_json(200, {"added": added, "leave": leave})
            return

        if action == "delete":
            lid = req.get("id", "")
            state["leave"] = [x for x in leave if x.get("id") != lid]
            _write_workhours(state)
            self._respond_json(200, {"leave": state["leave"]})
            return

        self._respond(400, f"Unknown action: {action!r}")

    def _handle_track(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            self._respond(400, "Invalid JSON")
            return

        action = req.get("action", "")
        key = req.get("key", "")
        summary = req.get("summary", "")

        state = _read_timetrack()

        if action == "start":
            if not key:
                self._respond(400, "Missing key")
                return
            _close_active(state)
            reopened = False
            tracker = state.get("trackers", {}).get(key)
            if tracker:
                segs = tracker.get("segments") or []
                if segs:
                    last_seg = max(segs, key=lambda s: s.get("ended_at", ""))
                    try:
                        gap = (datetime.datetime.now().astimezone() - _parse_dt(last_seg["ended_at"])).total_seconds()
                        if gap < 60:
                            segs.remove(last_seg)
                            _recompute_total(tracker)
                            state["active"] = {"key": key, "started_at": last_seg["started_at"]}
                            reopened = True
                    except Exception:
                        pass
            if not reopened:
                _start_tracker(state, key, summary)
        elif action == "idle":
            _close_active(state)
            _start_tracker(state, IDLE_KEY, "Idle")
        elif action in ("stop", "stopall"):
            _close_active(state)
        elif action == "edit_active":
            code, err = self._do_edit_active(state, req)
            if err:
                self._respond(code, err)
                return
        elif action == "add_segment":
            err = self._do_add_segment(state, req)
            if err:
                self._respond(400, err)
                return
        elif action == "edit_segment":
            code, err = self._do_edit_segment(state, req)
            if err:
                self._respond(code, err)
                return
        elif action == "delete_segment":
            code, err = self._do_delete_segment(state, req)
            if err:
                self._respond(code, err)
                return
        else:
            self._respond(400, f"Unknown action: {action!r}")
            return

        _write_timetrack(state)
        self._respond_json(200, state)

    def _do_edit_active(self, state, req):
        """Update started_at of the currently active segment."""
        active = state.get("active")
        if not active:
            return 400, "No active tracking"
        try:
            started = _parse_dt(req.get("started_at", ""))
        except ValueError:
            return 400, "Invalid start datetime"
        if started > datetime.datetime.now().astimezone():
            return 400, "Start time is in the future"
        active["started_at"] = started.isoformat(timespec="seconds")
        if active["key"] != IDLE_KEY:
            _trim_idle_upto(state, started)
        return 200, None

    def _do_add_segment(self, state, req):
        """Append a manual segment, creating the tracker if needed. Returns error str or None."""
        key = req.get("key", "")
        if not key:
            return "Missing key"
        try:
            started = _parse_dt(req.get("started_at", ""))
            ended = _parse_dt(req.get("ended_at", ""))
        except ValueError:
            return "Invalid start/end datetime"
        if ended <= started:
            return "End must be after start"
        tracker = state.setdefault("trackers", {}).setdefault(
            key, {"summary": req.get("summary") or key, "total_seconds": 0, "segments": []}
        )
        if req.get("summary"):
            tracker["summary"] = req["summary"]
        tracker["segments"].append({
            "id": _new_id(),
            "started_at": started.isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds"),
            "seconds": int((ended - started).total_seconds()),
        })
        tracker["segments"].sort(key=lambda s: s.get("started_at", ""))
        _recompute_total(tracker)
        return None

    def _do_edit_segment(self, state, req):
        """Edit an existing segment's start/end. Returns (code, error) or (200, None)."""
        key, seg_id = req.get("key", ""), req.get("id", "")
        if not key or not seg_id:
            return 400, "Missing key or id"
        tracker = state.get("trackers", {}).get(key)
        if not tracker:
            return 404, f"Tracker {key} not found"
        seg = next((s for s in tracker.get("segments", []) if s.get("id") == seg_id), None)
        if seg is None:
            return 404, "Segment not found"
        try:
            started = _parse_dt(req.get("started_at", ""))
            ended = _parse_dt(req.get("ended_at", ""))
        except ValueError:
            return 400, "Invalid start/end datetime"
        if ended <= started:
            return 400, "End must be after start"
        seg["started_at"] = started.isoformat(timespec="seconds")
        seg["ended_at"] = ended.isoformat(timespec="seconds")
        seg["seconds"] = int((ended - started).total_seconds())
        tracker["segments"].sort(key=lambda s: s.get("started_at", ""))
        _recompute_total(tracker)
        return 200, None

    def _do_delete_segment(self, state, req):
        """Delete a segment; drop the tracker if it becomes empty and isn't active."""
        key, seg_id = req.get("key", ""), req.get("id", "")
        if not key or not seg_id:
            return 400, "Missing key or id"
        tracker = state.get("trackers", {}).get(key)
        if not tracker:
            return 404, f"Tracker {key} not found"
        segs = tracker.get("segments", [])
        new_segs = [s for s in segs if s.get("id") != seg_id]
        if len(new_segs) == len(segs):
            return 404, "Segment not found"
        tracker["segments"] = new_segs
        active_key = (state.get("active") or {}).get("key")
        if not new_segs and key != active_key:
            del state["trackers"][key]
        else:
            _recompute_total(tracker)
        return 200, None

    # ── local-only guards ────────────────────────────────────────────────
    _FORBIDDEN_DIRS = {"secrets", "__pycache__"}

    def parse_request(self):
        """Refuse requests addressed to any other host name, so a web page in
        the user's browser can't reach these token-backed endpoints by pointing
        a DNS name at 127.0.0.1 (DNS rebinding)."""
        if not super().parse_request():
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host and host not in ("localhost", "127.0.0.1", "::1"):
            self.send_error(403, "Forbidden host")
            return False
        return True

    def send_head(self):
        """Never serve secrets/, dotfiles (.git, .claude, .envrc) or caches.
        Checked on the *translated* path, so %-encoding, '..', '//' and APFS
        case-folding can't slip past."""
        try:
            parts = pathlib.PurePath(self.translate_path(self.path)).relative_to(self.directory).parts
        except ValueError:
            self.send_error(404)
            return None
        if any(p.startswith(".") or p.lower() in self._FORBIDDEN_DIRS for p in parts):
            self.send_error(404)
            return None
        return super().send_head()

    def _respond(self, code: int, msg: str):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class _LocalOnlyServer(http.server.ThreadingHTTPServer):
    """Serves this machine only.

    The socket still binds 0.0.0.0 because macOS lets a normal user bind a
    privileged port (666) only via INADDR_ANY — binding 127.0.0.1:666 needs
    root. So connections from anywhere else are dropped here instead, which
    keeps secrets/, the Jira image proxy and every token-backed /api endpoint
    off the network.
    """

    def verify_request(self, request, client_address):
        return client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _restart_on_code_change(interval: float = 2.0) -> None:
    """Re-exec when server.py changes on disk, so the long-running launchd
    instance never keeps serving stale code. An edit that doesn't compile is
    skipped (old code keeps running) instead of taking the dashboard down."""
    import threading
    import time
    path = pathlib.Path(__file__).resolve()
    seen = path.stat().st_mtime

    def loop():
        nonlocal seen
        while True:
            time.sleep(interval)
            try:
                mtime = path.stat().st_mtime
                if mtime == seen:
                    continue
                seen = mtime
                compile(path.read_text(), str(path), "exec")
                # Compiling isn't enough: an edit can parse and still blow up at
                # import time, which would leave launchd crash-looping. Load it
                # in a child first (run_name keeps the __main__ block, and the
                # port bind, out of it).
                probe = subprocess.run(
                    [sys.executable, "-c", "import runpy, sys; runpy.run_path(sys.argv[1], run_name='probe')", str(path)],
                    capture_output=True, text=True, timeout=30)
                if probe.returncode:
                    raise ValueError((probe.stderr.strip().splitlines() or [f"exit {probe.returncode}"])[-1])
            except (OSError, SyntaxError, ValueError, subprocess.SubprocessError) as e:
                print(f"server.py changed but can't load — keeping old code: {e}", flush=True)
                continue
            print("server.py changed — restarting", flush=True)
            os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    os.chdir(ROOT)
    DATA_DIR.mkdir(exist_ok=True)
    if TEST_MODE:
        print(f"TEST MODE — data from: {DATA_DIR}", flush=True)
    _migrate_timetrack()
    _restart_on_code_change()
    with _LocalOnlyServer(("", PORT), Handler) as srv:
        print(f"dashboard server listening on :{PORT}", flush=True)
        srv.serve_forever()

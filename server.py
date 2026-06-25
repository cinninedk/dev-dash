#!/usr/bin/env python3
"""Static file server for the dashboard.

Serves static files and handles /api/comments?pr_id=<N> by proxying
to Bitbucket so the browser can fetch reviewer comments without needing
the token directly.
"""
import datetime
import http.server
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

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
IDLE_KEY  = "__IDLE__"

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
    """Round to nearest 15 min: down if minutes%15 < 3, else up. Returns seconds."""
    m = int(seconds) // 60
    r = m % 15
    m = m - r if r < 3 else m + (15 - r)
    return m * 60


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
        if self.path.startswith("/api/worklog-status"):
            self._handle_worklog_status()
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
        self._respond(404, "Not found")

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


if __name__ == "__main__":
    os.chdir(ROOT)
    DATA_DIR.mkdir(exist_ok=True)
    if TEST_MODE:
        print(f"TEST MODE — data from: {DATA_DIR}", flush=True)
    _migrate_timetrack()
    with http.server.HTTPServer(("", PORT), Handler) as srv:
        print(f"dashboard server listening on :{PORT}", flush=True)
        srv.serve_forever()

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
IDLE_KEY  = "__IDLE__"


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
               and a["comment"].get("state") != "RESOLVED"]

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

    def do_POST(self):
        if self.path == "/api/track":
            self._handle_track()
            return
        self._respond(404, "Not found")

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
            _start_tracker(state, key, summary)
        elif action == "idle":
            _close_active(state)
            _start_tracker(state, IDLE_KEY, "Idle")
        elif action in ("stop", "stopall"):
            _close_active(state)
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

#!/usr/bin/env python3
"""Static file server for the dashboard.

Serves static files and handles /api/comments?pr_id=<N> by proxying
to Bitbucket so the browser can fetch reviewer comments without needing
the token directly.
"""
import http.server
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

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
ACTIVE = ROOT / "data" / ".active"


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

    bb = json.loads((ROOT / "data" / "bitbucket.json").read_text())
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
            try:
                ACTIVE.touch()
            except OSError:
                pass
        super().do_GET()

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

    def _respond(self, code: int, msg: str):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.chdir(ROOT)
    with http.server.HTTPServer(("", PORT), Handler) as srv:
        print(f"dashboard server listening on :{PORT}", flush=True)
        srv.serve_forever()

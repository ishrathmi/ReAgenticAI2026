#!/usr/bin/env python3
"""LitVault local server: the board UI plus a small API to save profiles and start retrieval.

    python3 serve.py [--project /path/to/LitVault] [--port 8780]

Then open http://localhost:8780. Listens on 127.0.0.1 only.

  GET  /                      latest board (or an empty board to set up a first topic)
  GET  /new?profile=<name>    empty board for a profile (or a blank one for a new topic)
  GET  /api/profiles          [{name, topic, updated, board}]
  GET  /api/profile?name=     {name, md}
  POST /api/profile           {md, name?} -> writes profiles/<name>.md (name defaults to the topic slug)
  POST /api/autofill          {md} -> {topic?, description?, via}: fills blank fields from the keywords
  POST /api/run               {md?, name} -> saves, then runs pipeline.py in the background
  GET  /api/job               status of the current/last run
  POST /api/download          {topic, end, vaults:[{name, papers}]} -> {id}: fetch PDFs and zip them by vault
  GET  /api/download?id=      progress {total, done, ok, failed, ready}
  GET  /api/download/file?id= the finished zip
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, threading, time, uuid
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from profile_md import parse
from build_board import empty_board
from pipeline import autofill
from vault_zip import build_zip
from workspace import default_workspace, ensure

BLANK = """---
topic:
sources: biorxiv
days: 7
high: 70
medium: 40
vaults: Must read, Methods & protocols, Background, Discard
---
#

## Description

## Keywords

## Exclude

## Preferred categories

## Notes for the scorer
"""
JOB = {"proc": None, "status": None, "name": None}
LOCK = threading.Lock()
DOWNLOADS = {}  # id -> {"total","done","ok","failed","ready","error","filename","zip"}


def slugify(s):
    """URL/file-safe topic name; long names are cut at a word boundary."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s if len(s) <= 60 else s[:60].rsplit("-", 1)[0]


class App(SimpleHTTPRequestHandler):
    project = "."

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=self.project, **kw)

    def log_message(self, fmt, *args):
        if "/api/job" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)

    # ---- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def profiles_dir(self):
        d = os.path.join(self.project, "profiles")
        os.makedirs(d, exist_ok=True)
        return d

    def profile_path(self, name):
        name = slugify(name)
        if not name:
            raise ValueError("Give the profile a topic first")
        return name, os.path.join(self.profiles_dir(), name + ".md")

    def latest_board(self, name):
        topic = parse(open(os.path.join(self.profiles_dir(), name + ".md")).read()).get("topic", "")
        hits = sorted(glob.glob(os.path.join(self.project, "digests", f"*_{slugify(topic)}", "board.html")))
        return "/" + os.path.relpath(hits[-1], self.project) if hits else None

    def save_profile(self, md, name=None):
        prof = parse(md)
        if not (name or prof.get("topic")) and any(g["terms"] for g in prof["concepts"]):
            # no topic yet: park it under a temporary name; the pipeline names it from the keywords
            name = "untitled-" + time.strftime("%Y%m%d-%H%M%S")
        name, path = self.profile_path(name or prof.get("topic", ""))
        with open(path, "w") as f:
            f.write(md if md.endswith("\n") else md + "\n")
        return name, path

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                boards = sorted(glob.glob(os.path.join(self.project, "digests", "*", "board.html")), key=os.path.getmtime)
                if boards:
                    self.send_response(302)
                    self.send_header("Location", "/" + os.path.relpath(boards[-1], self.project))
                    self.end_headers()
                else:
                    self.send_html(empty_board(BLANK, ""))
            elif u.path == "/new":
                name = slugify(q.get("profile", ""))
                path = os.path.join(self.profiles_dir(), name + ".md")
                if name and os.path.exists(path):
                    self.send_html(empty_board(open(path).read(), name + ".md"))
                else:
                    self.send_html(empty_board(BLANK, ""))
            elif u.path == "/api/ping":
                self.send_json({"ok": True, "claude": bool(shutil.which("claude"))})
            elif u.path == "/api/profiles":
                out = []
                for p in sorted(glob.glob(os.path.join(self.profiles_dir(), "*.md"))):
                    name = os.path.basename(p)[:-3]
                    prof = parse(open(p).read())
                    out.append({"name": name, "topic": prof.get("topic") or name, "updated": prof.get("updated", ""),
                                "board": self.latest_board(name)})
                self.send_json(out)
            elif u.path == "/api/profile":
                name, path = self.profile_path(q.get("name", ""))
                if not os.path.exists(path):
                    return self.send_json({"error": "not found"}, 404)
                self.send_json({"name": name, "md": open(path).read(), "board": self.latest_board(name)})
            elif u.path == "/api/job":
                self.send_json(self.job_status())
            elif u.path == "/api/download":
                d = DOWNLOADS.get(q.get("id", ""))
                if not d:
                    return self.send_json({"error": "unknown download"}, 404)
                self.send_json({k: v for k, v in d.items() if k != "zip"})
            elif u.path == "/api/download/file":
                d = DOWNLOADS.get(q.get("id", ""))
                if not d or not d.get("zip"):
                    return self.send_json({"error": "not ready"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{d["filename"]}"')
                self.send_header("Content-Length", str(len(d["zip"])))
                self.end_headers()
                self.wfile.write(d["zip"])
            else:
                super().do_GET()
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self.body()
            if u.path == "/api/profile":
                name, path = self.save_profile(b["md"], b.get("name"))
                self.send_json({"name": name, "path": os.path.relpath(path, self.project)})
            elif u.path == "/api/download":
                vaults = [v for v in b.get("vaults", []) if v.get("papers")]
                if not vaults:
                    return self.send_json({"error": "No papers in your vaults yet"}, 400)
                did = uuid.uuid4().hex[:12]
                job = DOWNLOADS[did] = {"total": sum(len(v["papers"]) for v in vaults), "done": 0, "ok": 0,
                                        "failed": [], "ready": False, "error": None}
                for old in list(DOWNLOADS)[:-3]:  # keep only the last few zips in memory
                    DOWNLOADS.pop(old, None)
                cache = os.path.join(self.project, "digests", "_cache", "pdfs")

                def work():
                    try:
                        job["zip"] = build_zip(job, b.get("topic", ""), b.get("end", ""), vaults, cache)
                        job["ready"] = True
                    except Exception as e:
                        job["error"] = str(e)
                threading.Thread(target=work, daemon=True).start()
                self.send_json({"id": did, "total": job["total"]})
            elif u.path == "/api/autofill":
                prof = parse(b["md"])
                got, via = autofill(prof, bool(shutil.which("claude")))
                self.send_json({**got, "via": via})
            elif u.path == "/api/run":
                with LOCK:
                    if JOB["proc"] and JOB["proc"].poll() is None:
                        return self.send_json({"error": f"A retrieval for '{JOB['name']}' is already running"}, 409)
                    if b.get("md"):
                        name, path = self.save_profile(b["md"], b.get("name"))
                    else:
                        name, path = self.profile_path(b.get("name", ""))
                    prof = parse(open(path).read())
                    if not prof["concepts"] and not prof.get("description"):
                        return self.send_json({"error": "Add some keywords or a description first"}, 400)
                    status = os.path.join(self.project, "digests", "_cache", "job_status.json")
                    os.makedirs(os.path.dirname(status), exist_ok=True)
                    if os.path.exists(status):
                        os.remove(status)
                    log = open(os.path.join(self.project, "digests", "_cache", "job.log"), "w")
                    cmd = [sys.executable, os.path.join(HERE, "pipeline.py"), "--profile", path, "--status", status]
                    JOB.update(proc=subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=self.project),
                               status=status, name=name)
                self.send_json({"ok": True, "name": name})
            else:
                self.send_json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

    def job_status(self):
        if not JOB["status"]:
            return {"running": False}
        d = {}
        if os.path.exists(JOB["status"]):
            try:
                d = json.load(open(JOB["status"]))
            except json.JSONDecodeError:
                pass
        running = JOB["proc"].poll() is None
        if not running and not d.get("done"):
            d.update(done=True, error=d.get("error") or "The retrieval stopped unexpectedly; see digests/_cache/job.log")
        if d.get("board"):
            d["board"] = "/" + d["board"]
        return {"running": running, "name": d.get("profile") or JOB["name"], **d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=default_workspace(),
                    help="LitVault folder holding profiles/ and digests/ (default: $LITVAULT_HOME, ./ if it has profiles/, else ~/LitVault)")
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    App.project = ensure(os.path.abspath(os.path.expanduser(a.project)))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), App)
    print(f"LitVault on http://localhost:{a.port}  (project: {App.project})")
    srv.serve_forever()


if __name__ == "__main__":
    main()

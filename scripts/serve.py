#!/usr/bin/env python3
"""Usage: python3 scripts/serve.py [--port 8765]

A small local server for the jobs page. Standard library only, bound to 127.0.0.1.

  GET  /                          web/index.html
  GET  /static/<file>             files inside web/ only
  GET  /api/view?range=&from=&sort=&show=&status=&model=&q=
                                  the left nav cards, graph series, counts (scripts/jobs_query.py)
  GET  /api/job/<id>              the full job record
  GET  /api/rollup/<date>         that day's boost rollup (rollups.json)
  POST /api/job/<id>/status       {"status": "applied" | "skipped" | null}: the only write; it
                                  changes user_status of that one job in jobs.json

Every request must carry a Host header of localhost or 127.0.0.1. A corrupt jobs.json stops the
server from starting and makes requests fail with a 500 and a message, never an empty page.
"""
import argparse
import json
import mimetypes
import re
import shutil
import sys
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs_query as jq  # noqa: E402
from jobslock import jobs_lock  # noqa: E402
from clean import write_json_atomic  # noqa: E402
from jobs_query import JobsError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765
MAX_BODY = 10_000
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
VIEW_CHOICES = {"range": {"today", "yesterday", "from"}, "sort": {"score", "recent"},
                "show": {"all", "passed", "rejected", "flagged", "boost"},
                "status": {"any", "new", "applied", "skipped"}}
STATUS_VALUES = ("applied", "skipped", None)
mimetypes.add_type("application/javascript", ".js")


class JobsStore:
    """jobs.json and rollups.json, re-read when the file changes, with one safe write."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "jobs.json"
        self._lock = threading.Lock()
        self._key = None
        self._cache = []

    def _stamp(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    def jobs(self):
        with self._lock:
            stamp = self._stamp()
            if stamp != self._key or self._key is None:
                self._cache = jq.load_jobs(self.path)  # raises JobsError if corrupt
                self._key = stamp
            return self._cache

    def get(self, job_id):
        return next((j for j in self.jobs() if str(j["id"]) == str(job_id)), None)

    def set_status(self, job_id, status):
        if status not in STATUS_VALUES:
            raise ValueError("status must be one of applied, skipped, null")
        with self._lock, jobs_lock(self.root):
            jobs = jq.load_jobs(self.path)  # fresh read: never write over a stale copy
            job = next((j for j in jobs if str(j["id"]) == str(job_id)), None)
            if job is None:
                raise KeyError(job_id)
            job["user_status"] = status
            if self.path.exists():
                shutil.copyfile(self.path, self.root / "jobs.json.bak")
            write_json_atomic(self.path, jobs)
            self._key = None
            return job["user_status"]

    def rollup(self, run_date):
        path = self.root / "rollups.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise JobsError("rollups.json is not valid JSON (%s)" % e)
        return data.get(run_date) if isinstance(data, dict) else None


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def make_handler(store, web_dir, quiet, today=None):
    web_dir = Path(web_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "JobsPage"

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write("%s\n" % (fmt % args))

        # ----- plumbing -----
        def send_bytes(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, data):
            self.send_bytes(status, json.dumps(data).encode("utf-8"), "application/json")

        def handle_request(self, method):
            try:
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ALLOWED_HOSTS:
                    raise HttpError(403, "unexpected Host header")
                path = urlsplit(self.path).path
                query = parse_qs(urlsplit(self.path).query)
                if method == "GET":
                    self.route_get(path, query)
                elif method == "POST":
                    self.route_post(path)
                else:
                    raise HttpError(405, "method not allowed")
            except HttpError as e:
                self.send_json(e.status, {"error": e.message})
            except JobsError as e:
                self.send_json(500, {"error": str(e)})
            except Exception as e:  # never leave the browser hanging
                self.send_json(500, {"error": "%s: %s" % (type(e).__name__, e)})

        do_GET = lambda self: self.handle_request("GET")  # noqa: E731
        do_POST = lambda self: self.handle_request("POST")  # noqa: E731
        do_PUT = do_DELETE = do_PATCH = lambda self: self.handle_request("OTHER")  # noqa: E731

        # ----- routes -----
        def route_get(self, path, query):
            if path == "/":
                return self.send_file(web_dir / "index.html")
            if path.startswith("/static/"):
                return self.send_static(unquote(path[len("/static/"):]))
            if path == "/api/view":
                return self.send_json(200, self.view(query))
            m = re.fullmatch(r"/api/job/([^/]+)", path)
            if m:
                job = store.get(unquote(m.group(1)))
                if job is None:
                    raise HttpError(404, "no such job")
                return self.send_json(200, job)
            m = re.fullmatch(r"/api/rollup/([^/]+)", path)
            if m:
                run_date = unquote(m.group(1))
                self.check_date(run_date)
                rollup = store.rollup(run_date)
                if rollup is None:
                    raise HttpError(404, "no rollup for %s" % run_date)
                return self.send_json(200, rollup)
            raise HttpError(404, "not found")

        def route_post(self, path):
            m = re.fullmatch(r"/api/job/([^/]+)/status", path)
            if not m:
                raise HttpError(404, "not found")
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                raise HttpError(415, "send application/json")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpError(400, "bad Content-Length")
            if length > MAX_BODY:
                raise HttpError(413, "request body too large")
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise HttpError(400, "body is not valid JSON")
            if not isinstance(body, dict) or "status" not in body:
                raise HttpError(400, 'body must be an object with a "status" key')
            job_id = unquote(m.group(1))
            try:
                status = store.set_status(job_id, body["status"])
            except ValueError as e:
                raise HttpError(400, str(e))
            except KeyError:
                raise HttpError(404, "no such job")
            self.send_json(200, {"id": job_id, "user_status": status})

        def check_date(self, value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HttpError(400, "date must be YYYY-MM-DD")

        def view(self, query):
            args = {}
            for key, allowed in VIEW_CHOICES.items():
                value = (query.get(key) or [None])[0]
                if value is not None:
                    if value not in allowed:
                        raise HttpError(400, "%s must be one of %s" % (key, ", ".join(sorted(allowed))))
                    args[key] = value
            from_date = (query.get("from") or [None])[0]
            if from_date:
                self.check_date(from_date)
            elif args.get("range") == "from":
                raise HttpError(400, "range=from needs from=YYYY-MM-DD")
            kwargs = {"range": args.get("range", "today"), "from_date": from_date, "sort": args.get("sort", "score"),
                      "show": args.get("show", "all"), "status": args.get("status", "any"),
                      "model": (query.get("model") or [jq.DEFAULT_MODEL])[0],
                      "q": (query.get("q") or [""])[0], "today": today or date.today().isoformat()}
            return jq.query(store.jobs(), **kwargs)

        # ----- files -----
        def send_static(self, rel):
            parts = Path(rel).parts
            if not rel or rel.startswith(("/", "\\")) or "\x00" in rel or ".." in parts:
                raise HttpError(404, "not found")
            target = (web_dir / rel).resolve()
            if web_dir not in target.parents:
                raise HttpError(404, "not found")
            self.send_file(target)

        def send_file(self, target):
            if not target.is_file():
                raise HttpError(404, "not found")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_bytes(200, target.read_bytes(), ctype)

    return Handler


def create_server(root, port=DEFAULT_PORT, quiet=True, today=None):
    """`today` (YYYY-MM-DD) fixes the date "Today" means; tests use it."""
    root = Path(root)
    store = JobsStore(root)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, root / "web", quiet, today))


def main(argv):
    parser = argparse.ArgumentParser(description="Serve the jobs page on localhost.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        JobsStore(ROOT).jobs()  # refuse to start on a corrupt jobs.json
    except JobsError as e:
        sys.exit("serve.py: %s" % e)
    try:
        server = create_server(ROOT, args.port, quiet=False)
    except OSError as e:
        sys.exit("serve.py: cannot listen on port %d (%s). If the page is already running it is at "
                 "http://localhost:%d/ ; otherwise pick another port with --port." % (args.port, e.strerror or e, args.port))
    print(json.dumps({"url": "http://localhost:%d/" % server.server_address[1]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])

"""Requests from the jobs page ("Give me boost") to plan one specific job.

One file per job in queue/boost_requests/<id>.json: {id, run_date, state, requested_at, error?}.
States: waiting, running, failed. A finished request is deleted (the plan is then in jobs.json).
The page server's BoostRunner takes waiting requests one at a time and runs
`boost.py <run date> --job <id>`; that command exits 3 while a worker is running, and the request
then simply goes back to waiting until the worker has finished.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean import write_json_atomic  # noqa: E402

STATES = ("waiting", "running", "failed")
RUN_TIMEOUT_SECONDS = 3600


class RequestError(Exception):
    pass


def _dir(root):
    return Path(root) / "queue" / "boost_requests"


def _path(root, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"\d+", job_id):
        raise RequestError("job id must be digits, got %r" % (job_id,))
    return _dir(root) / ("%s.json" % job_id)


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise RequestError("%s is not valid JSON (%s)" % (path, e))
    if not isinstance(data, dict) or data.get("state") not in STATES:
        raise RequestError("%s has the wrong shape" % path)
    return data


def read(root, job_id):
    path = _path(root, job_id)
    return _load(path) if path.exists() else None


def submit(root, job_id, run_date):
    """Create a waiting request; return the existing one if it is waiting or running; retry a failed one."""
    path = _path(root, job_id)
    existing = _load(path) if path.exists() else None
    if existing and existing["state"] in ("waiting", "running"):
        return existing
    request = {"id": job_id, "run_date": run_date, "state": "waiting",
               "requested_at": (existing or {}).get("requested_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, request)
    return request


def set_state(root, job_id, state, error=None):
    path = _path(root, job_id)
    request = _load(path)
    request["state"] = state
    request.pop("error", None)
    if error:
        request["error"] = error
    write_json_atomic(path, request)


def clear(root, job_id):
    try:
        _path(root, job_id).unlink()
    except FileNotFoundError:
        pass


def next_waiting(root):
    if not _dir(root).exists():
        return None
    waiting = [r for r in (_load(p) for p in _dir(root).glob("*.json")) if r["state"] == "waiting"]
    return min(waiting, key=lambda r: (r["requested_at"], r["id"])) if waiting else None


def reset_running(root):
    """After a crash: a request left running goes back to waiting."""
    if not _dir(root).exists():
        return 0
    count = 0
    for path in _dir(root).glob("*.json"):
        request = _load(path)
        if request["state"] == "running":
            request["state"] = "waiting"
            write_json_atomic(path, request)
            count += 1
    return count


class BoostRunner:
    def __init__(self, root, python=None, run=subprocess.run):
        self.root = Path(root)
        self.python = Path(python) if python else self.root / ".venv" / "bin" / "python"
        self.run = run

    def step(self):
        """Handle the oldest waiting request. Returns "done", "failed", "busy" (a worker is running) or None."""
        request = next_waiting(self.root)
        if request is None:
            return None
        job_id = request["id"]
        if not self.python.exists():
            set_state(self.root, job_id, "failed", "%s not found. Create the venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" % self.python)
            return "failed"
        set_state(self.root, job_id, "running")
        cmd = [str(self.python), str(self.root / "scripts" / "boost.py"), request["run_date"], "--job", job_id]
        try:
            result = self.run(cmd, capture_output=True, text=True, cwd=str(self.root), timeout=RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            set_state(self.root, job_id, "failed", "boost timed out after %d minutes" % (RUN_TIMEOUT_SECONDS // 60))
            return "failed"
        except OSError as e:
            set_state(self.root, job_id, "failed", str(e))
            return "failed"
        if result.returncode == 0:
            clear(self.root, job_id)
            return "done"
        if result.returncode == 3:
            set_state(self.root, job_id, "waiting")
            return "busy"
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        set_state(self.root, job_id, "failed", (lines[-1] if lines else "boost exited with code %d" % result.returncode)[:300])
        return "failed"

    def loop(self, stop, idle=3.0, busy=15.0):
        """Run step() until `stop` (a threading.Event) is set."""
        reset_running(self.root)
        while not stop.is_set():
            try:
                outcome = self.step()
            except Exception as e:  # keep the page server alive whatever one request does
                sys.stderr.write("boost runner: %s: %s\n" % (type(e).__name__, e))
                outcome = None
            stop.wait(busy if outcome == "busy" else idle if outcome is None else 0.5)

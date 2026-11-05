"""The job spool and run state: queue/pending, queue/processing, queue/failed and queue/run.json.

One JSON file per job; moving a file between folders is atomic, and files are written under a temp
name first, so a reader never sees a half-written job. (Named jobqueue, not queue, so it does not
shadow the standard library.)
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean import write_json_atomic  # noqa: E402

STATES = ("pending", "processing", "failed")


class QueueError(Exception):
    pass


def _dir(root, state=None):
    path = Path(root) / "queue"
    if state:
        path = path / state
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise QueueError("%s is not valid JSON: %s (%s)" % (what, path, e))


# ---------- run state ----------

def _run_path(root):
    return _dir(root) / "run.json"


def read_run(root):
    path = _run_path(root)
    if not path.exists():
        return None
    run = _read(path, "run.json")
    if not isinstance(run, dict) or "date" not in run:
        raise QueueError("run.json has the wrong shape: %s" % path)
    return run


def begin_run(root, today=None):
    run = read_run(root)
    if run is None:
        run = {"date": today or date.today().isoformat(), "stopping": False}
        write_json_atomic(_run_path(root), run)
    return run


def set_stopping(root):
    run = begin_run(root)
    run["stopping"] = True
    write_json_atomic(_run_path(root), run)
    return run


def end_run(root):
    try:
        _run_path(root).unlink()
    except FileNotFoundError:
        pass


# ---------- jobs ----------

def put(root, job_id, job):
    pending = _dir(root, "pending")
    write_json_atomic(_dir(root) / (".%s.tmp" % job_id), job)  # temp name: never listed as a job
    os.replace(_dir(root) / (".%s.tmp" % job_id), pending / ("%s.json" % job_id))


def state_of(root, job_id):
    for state in STATES:
        if (_dir(root, state) / ("%s.json" % job_id)).exists():
            return state
    return None


def claim_next(root):
    pending = _dir(root, "pending")
    files = sorted(pending.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    if not files:
        return None
    source = files[0]
    job = _read(source, "queued job")
    os.replace(source, _dir(root, "processing") / source.name)
    return source.stem, job


def complete(root, job_id):
    try:
        (_dir(root, "processing") / ("%s.json" % job_id)).unlink()
    except FileNotFoundError:
        pass


def fail(root, job_id, error):
    source = _dir(root, "processing") / ("%s.json" % job_id)
    job = _read(source, "queued job")
    write_json_atomic(_dir(root, "failed") / ("%s.json" % job_id), {"job": job, "error": str(error)})
    source.unlink()


def requeue_failed(root, job_id):
    source = _dir(root, "failed") / ("%s.json" % job_id)
    if not source.exists():
        return False
    job = _read(source, "failed job")["job"]
    put(root, job_id, job)
    source.unlink()
    return True


def release(root, job_id):
    """Put one job the worker has taken back in pending (used when the cause was Ollama, not the job)."""
    source = _dir(root, "processing") / ("%s.json" % job_id)
    if source.exists():
        os.replace(source, _dir(root, "pending") / source.name)


def recover(root):
    moved = 0
    for source in _dir(root, "processing").glob("*.json"):
        os.replace(source, _dir(root, "pending") / source.name)
        moved += 1
    return moved


def counts(root):
    return {state: len(list(_dir(root, state).glob("*.json"))) for state in STATES}

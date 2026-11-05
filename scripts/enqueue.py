#!/usr/bin/env python3
"""Usage: python3 scripts/enqueue.py '<job json>'      or      python3 scripts/enqueue.py -   (JSON on stdin)

Hands one scraped job to the pipeline. Validates it, appends it to runs/<run date>/raw.json (the
record of what was scraped) and drops it in queue/pending/ for the worker.

Prints one JSON line: {"status": "queued" | "duplicate" | "requeued", "id", "date", "pending"}.
A duplicate is a job already in jobs.json or in the queue (that is normal, not an error). A failed
job with the same id is put back in the queue. On any error prints {"status": "error", "error": ...}
and exits 1 with nothing queued: bad JSON, a missing required field, a corrupt jobs.json or raw.json,
or a run that is already stopping.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobqueue  # noqa: E402
from clean import validate, write_json_atomic  # noqa: E402
from jobslock import jobs_lock  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class EnqueueError(Exception):
    pass


def _load_list(path, what):
    if not Path(path).exists():
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise EnqueueError("%s is not valid JSON: %s (%s)" % (what, path, e))
    if not isinstance(data, list):
        raise EnqueueError("%s must contain a JSON list: %s" % (what, path))
    return data


def enqueue(root, job, today=None):
    root = Path(root)
    job_id, reason = validate(job)
    if reason:
        raise EnqueueError("invalid job: %s" % reason)
    job = {**job, "id": job_id}
    try:
        with jobs_lock(root):
            run = jobqueue.begin_run(root, today)
            if run["stopping"]:
                raise EnqueueError("run is stopping: no more jobs are accepted")
            known = {str(j.get("id")) for j in _load_list(root / "jobs.json", "jobs.json")}
            state = jobqueue.state_of(root, job_id)
            if job_id in known or state in ("pending", "processing"):
                status = "duplicate"
            elif state == "failed":
                jobqueue.requeue_failed(root, job_id)
                status = "requeued"
            else:
                raw_path = root / "runs" / run["date"] / "raw.json"
                raw = _load_list(raw_path, "raw.json")
                if not any(str(j.get("id")) == job_id for j in raw):
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    write_json_atomic(raw_path, raw + [job])
                jobqueue.put(root, job_id, job)
                status = "queued"
            return {"status": status, "id": job_id, "date": run["date"], "pending": jobqueue.counts(root)["pending"]}
    except jobqueue.QueueError as e:
        raise EnqueueError(str(e))


def main(argv):
    if len(argv) != 1:
        print(json.dumps({"status": "error", "error": "usage: enqueue.py '<job json>' | -"}))
        sys.exit(1)
    text = sys.stdin.read() if argv[0] == "-" else argv[0]
    try:
        try:
            job = json.loads(text)
        except ValueError as e:
            raise EnqueueError("not valid JSON (%s)" % e)
        if not isinstance(job, dict):
            raise EnqueueError("the job must be a JSON object")
        result = enqueue(ROOT, job)
    except EnqueueError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])

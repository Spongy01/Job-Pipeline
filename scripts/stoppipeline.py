#!/usr/bin/env python3
"""Usage: python3 scripts/stoppipeline.py

Tells the pipeline that no more jobs are coming. The worker finishes every queued job, then runs
boost, the rollup and the final merge, and exits. The web page is not touched. Prints one JSON line:
{"status": "stopping", "date", "pending", "processing"}; on error {"status": "error", ...} and exit 1.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobqueue  # noqa: E402
from jobslock import jobs_lock  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def stop(root):
    with jobs_lock(root):  # same lock as enqueue, so a job cannot slip in half-way through
        run = jobqueue.set_stopping(root)
    counts = jobqueue.counts(root)
    return {"status": "stopping", "date": run["date"], "pending": counts["pending"], "processing": counts["processing"]}


def main(argv):
    if argv:
        print(json.dumps({"status": "error", "error": "usage: stoppipeline.py"}))
        sys.exit(1)
    try:
        print(json.dumps(stop(ROOT)))
    except jobqueue.QueueError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])

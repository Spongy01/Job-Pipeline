#!/usr/bin/env python3
"""Usage: python3 seen.py <job_id> [<job_id> ...]
One id   -> prints "true" if already in jobs.json, else "false".
Many ids -> prints a JSON list of the ids NOT yet in jobs.json.
"""
import json
import sys
from pathlib import Path

JOBS_FILE = Path(__file__).resolve().parent.parent / "jobs.json"

def load_seen():
    if not JOBS_FILE.exists():
        return set()  # first run: nothing seen yet
    data = json.loads(JOBS_FILE.read_text())  # a corrupt file raises, on purpose
    if isinstance(data, dict):
        return {str(k) for k in data}
    return {str(job["id"]) for job in data}


if __name__ == "__main__":
    ids = [str(i) for i in sys.argv[1:]]
    if not ids:
        sys.exit("pass at least one job id")
    seen = load_seen()
    if len(ids) == 1:
        print("true" if ids[0] in seen else "false")
    else:
        print(json.dumps([i for i in ids if i not in seen]))
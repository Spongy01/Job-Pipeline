#!/usr/bin/env python3
"""Usage: python3 scripts/merge.py [YYYY-MM-DD]   (default: today)

Merges one run into the master files:
  jobs.json     a list; one record per job with all its data (enriched fields, filter result,
                boost plan, stage, run_date, user_status)
  rollups.json  an object keyed by run date with that day's boost rollup

Reads runs/<date>/enriched.json, filtered.json, boost/<id>.json and boost_rollup.json.
Never overwrites existing data: a job already in jobs.json only gets sections that are missing
or null (for example a boost plan that arrived a day later, or an enrichment model that was not
there). It never touches user_status or run_date. Nothing is rewritten when nothing changed.
jobs.json.bak is written first whenever jobs.json is rewritten.
Prints one JSON line of counts; exits non-zero and writes nothing on missing or corrupt input.
"""
import json
import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean import write_json_atomic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NEVER_TOUCH = ("user_status", "run_date")


class MergeError(Exception):
    pass


def load_json(path, what, default=None):
    path = Path(path)
    if not path.exists():
        if default is not None:
            return default
        raise MergeError("%s not found: %s" % (what, path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise MergeError("%s is not valid JSON: %s (%s)" % (what, path, e))


def build_record(job, filter_record, boost_record, run_date):
    record = dict(job)
    record["filter"] = ({k: filter_record.get(k) for k in ("stage", "reasons", "years_required", "years_model")}
                        if filter_record else None)
    record["boost"] = boost_record
    record["stage"] = filter_record["stage"] if filter_record else "enriched"
    record["run_date"] = run_date
    record["user_status"] = None
    return record


def fill_missing(existing, new):
    """Copy into `existing` what it lacks from `new`. Returns True if anything was added."""
    changed = False
    for key, value in new.items():
        if key in NEVER_TOUCH or value is None:
            continue
        if key == "enrichment":
            have = existing.get("enrichment")
            if not isinstance(have, dict):
                existing["enrichment"], changed = dict(value), True
                continue
            for model, entry in value.items():
                if model not in have:
                    have[model], changed = entry, True
        elif existing.get(key) is None:
            existing[key], changed = value, True
    return changed


def merge_run(root, run_date):
    root = Path(root)
    run_dir = root / "runs" / run_date
    enriched = load_json(run_dir / "enriched.json", "enriched.json")
    filtered = load_json(run_dir / "filtered.json", "filtered.json")
    if not isinstance(enriched, list) or not isinstance(filtered, list):
        raise MergeError("enriched.json and filtered.json must contain JSON lists")
    filters = {str(f["id"]): f for f in filtered}
    jobs = load_json(root / "jobs.json", "jobs.json", default=[])
    if not isinstance(jobs, list):
        raise MergeError("jobs.json must contain a JSON list")
    rollups = load_json(root / "rollups.json", "rollups.json", default={})
    if not isinstance(rollups, dict):
        raise MergeError("rollups.json must contain a JSON object")

    boosts = {}
    for job in enriched:
        path = run_dir / "boost" / (str(job["id"]) + ".json")
        if path.exists():
            record = load_json(path, "boost record")
            if record.get("ok"):
                boosts[str(job["id"])] = record
    rollup_record = load_json(run_dir / "boost_rollup.json", "boost_rollup.json", default={})

    index = {str(j["id"]): j for j in jobs}
    counts = {"date": run_date, "jobs_in_run": len(enriched), "added": 0, "filled": 0, "unchanged": 0}
    for job in enriched:
        job_id = str(job["id"])
        record = build_record(job, filters.get(job_id), boosts.get(job_id), run_date)
        if job_id not in index:
            jobs.append(record)
            index[job_id] = record
            counts["added"] += 1
        elif fill_missing(index[job_id], record):
            counts["filled"] += 1
        else:
            counts["unchanged"] += 1

    if rollup_record.get("ok") and run_date not in rollups:
        rollups[run_date], counts["rollup"] = rollup_record, "added"
    else:
        counts["rollup"] = "kept" if run_date in rollups else "none"

    if counts["added"] or counts["filled"]:
        jobs_path = root / "jobs.json"
        if jobs_path.exists():
            shutil.copyfile(jobs_path, root / "jobs.json.bak")
        write_json_atomic(jobs_path, jobs)
    if counts["rollup"] == "added":
        write_json_atomic(root / "rollups.json", rollups)
    return counts


def main(argv):
    if len(argv) > 1:
        sys.exit("usage: merge.py [YYYY-MM-DD]")
    run_date = argv[0] if argv else date.today().isoformat()
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        sys.exit("merge.py: date must be YYYY-MM-DD, got %r" % run_date)
    try:
        print(json.dumps(merge_run(ROOT, run_date)))
    except MergeError as e:
        sys.exit("merge.py: %s" % e)


if __name__ == "__main__":
    main(sys.argv[1:])

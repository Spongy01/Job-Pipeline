#!/usr/bin/env python3
"""Usage: python3 scripts/filter.py [YYYY-MM-DD] [--model MODEL] [--input NAME]

Reads runs/<date>/enriched.json and the "rules" in config.json. Writes
runs/<date>/filtered.json (one decision per job) and runs/<date>/filter_report.json.
Never changes config.json, jobs.json or enriched.json. Prints one JSON line of counts.

Rules applied (config.json "rules"):
  experience_years {min, max}      reject when the JD's required overall years are above max
                                   (years_min > max) or below min (years_max < min).
                                   A JD that states no years passes.
  exclude_us_citizenship_required  reject when jd_flags.us_citizenship_required is true
  exclude_clearance                reject when jd_flags.clearance is true
  exclude_sponsorship_denied       reject when jd_flags.sponsorship is false (explicitly refused);
                                   not mentioned (null) passes.
Rules in the config that this script does not apply yet (must_have_stacks, avoid_stacks,
locations, work_models, exclude_keywords) are listed in the report when they are set.

--model picks whose years of experience to trust (default qwen3.5:9b). If that model has no
usable result for a job, the first other model with one is used.
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean import write_json_atomic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "qwen3.5:9b"
NOT_APPLIED_RULES = ("must_have_stacks", "avoid_stacks", "locations", "work_models", "exclude_keywords")


class FilterError(Exception):
    pass


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FilterError("%s not found: %s" % (what, path))
    except (ValueError, UnicodeDecodeError) as e:
        raise FilterError("%s is not valid JSON: %s (%s)" % (what, path, e))


def required_years(job, preferred_model):
    """Return (years_min, years_max, model_used) of the JD's required overall years, or None."""
    enrichment = job.get("enrichment") or {}
    order = [preferred_model] + [m for m in enrichment if m != preferred_model]
    for model in order:
        entry = enrichment.get(model)
        if not entry or not entry.get("ok"):
            continue
        items = [e for e in entry["data"].get("experience", [])
                 if e.get("level") == "required" and str(e.get("area", "")).strip().lower() == "overall"]
        if not items:
            return None, model  # this model found no stated years
        return (max(e["years_min"] for e in items), max(e["years_max"] for e in items)), model
    return None, None


def evaluate(job, rules, preferred_model=DEFAULT_MODEL):
    """Return the decision record for one job."""
    record = {"id": str(job["id"]), "company": job.get("company"), "posting_name": job.get("posting_name"),
              "stage": "passed", "reasons": [], "years_required": None, "years_model": None}
    if job.get("closed"):
        record["stage"] = "closed"
        return record
    reasons = record["reasons"]

    years, model = required_years(job, preferred_model)
    record["years_model"] = model
    if years:
        record["years_required"] = {"min": years[0], "max": years[1]}
        limits = rules.get("experience_years") or {}
        if "max" in limits and years[0] > limits["max"]:
            reasons.append({"rule": "experience_years",
                            "detail": "asks for %g years, config allows up to %g" % (years[0], limits["max"]),
                            "evidence": "%s: %g to %g years required" % (model, years[0], years[1])})
        elif "min" in limits and years[1] < limits["min"]:
            reasons.append({"rule": "experience_years",
                            "detail": "asks for at most %g years, config wants at least %g" % (years[1], limits["min"]),
                            "evidence": "%s: %g to %g years required" % (model, years[0], years[1])})

    flags = job.get("jd_flags") or {}
    if rules.get("exclude_us_citizenship_required") and flags.get("us_citizenship_required"):
        reasons.append({"rule": "exclude_us_citizenship_required", "detail": "US citizenship or US person status required",
                        "evidence": flags.get("us_citizenship_evidence", "")})
    if rules.get("exclude_clearance") and flags.get("clearance"):
        reasons.append({"rule": "exclude_clearance", "detail": "security clearance or classified access mentioned",
                        "evidence": flags.get("clearance_evidence", "")})
    if rules.get("exclude_sponsorship_denied") and flags.get("sponsorship") is False:
        reasons.append({"rule": "exclude_sponsorship_denied", "detail": "JD says it does not sponsor visas",
                        "evidence": flags.get("sponsorship_evidence", "")})

    if reasons:
        record["stage"] = "rejected"
    return record


def run(root, run_date, model=DEFAULT_MODEL, input_name="enriched.json"):
    root = Path(root)
    run_dir = root / "runs" / run_date
    config = load_json(root / "config.json", "config.json")
    rules = config.get("rules")
    if not isinstance(rules, dict):
        raise FilterError('config.json has no "rules" object')
    jobs = load_json(run_dir / input_name, input_name)
    if not isinstance(jobs, list):
        raise FilterError("%s must contain a JSON list" % input_name)

    records = [evaluate(job, rules, model) for job in jobs]
    by_rule = {}
    for r in records:
        for reason in r["reasons"]:
            by_rule[reason["rule"]] = by_rule.get(reason["rule"], 0) + 1
    counts = {"input": len(records),
              "passed": sum(r["stage"] == "passed" for r in records),
              "rejected": sum(r["stage"] == "rejected" for r in records),
              "closed": sum(r["stage"] == "closed" for r in records)}
    report = {
        "counts": counts,
        "rejected_by_rule": by_rule,
        "years_model": model,
        "rules_applied": {k: rules[k] for k in ("experience_years", "exclude_us_citizenship_required",
                                                 "exclude_clearance", "exclude_sponsorship_denied") if k in rules},
        "rules_set_but_not_applied": {k: rules[k] for k in NOT_APPLIED_RULES if rules.get(k)},
        "rejected": [{"id": r["id"], "company": r["company"], "posting_name": r["posting_name"],
                      "reasons": [x["rule"] for x in r["reasons"]]} for r in records if r["stage"] == "rejected"],
    }
    write_json_atomic(run_dir / "filtered.json", records)
    write_json_atomic(run_dir / "filter_report.json", report)
    return {"date": run_date, **counts, "rejected_by_rule": by_rule}


def main(argv):
    p = argparse.ArgumentParser(description="Filter enriched jobs with the rules in config.json.")
    p.add_argument("date", nargs="?", default=date.today().isoformat())
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--input", default="enriched.json")
    args = p.parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        p.error("date must be YYYY-MM-DD")
    try:
        summary = run(ROOT, args.date, args.model, args.input)
    except FilterError as e:
        sys.exit("filter.py: %s" % e)
    print(json.dumps(summary))


if __name__ == "__main__":
    main(sys.argv[1:])

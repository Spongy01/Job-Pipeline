"""Everything the jobs page shows is computed here: filters, sorting, graph series, counts.

Pure functions over the list in jobs.json, so they are tested without a browser or a server.
"""
import json
from datetime import date, timedelta
from pathlib import Path

DEFAULT_MODEL = "qwen3.5:9b"
MODELS_DIFFER_BY = 25


class JobsError(Exception):
    pass


def load_jobs(path):
    """The list in jobs.json. A missing file is an empty list; a corrupt one raises."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise JobsError("jobs.json is not valid JSON (%s)" % e)
    if not isinstance(data, list):
        raise JobsError("jobs.json must contain a JSON list")
    return data


def models_in(jobs):
    return sorted({m for j in jobs for m in (j.get("enrichment") or {})})


def _ok_scores(job):
    return [e["data"]["score"] for e in (job.get("enrichment") or {}).values() if e.get("ok")]


def job_score(job, model):
    """A model's score for the job, the rounded mean of the ok models for "average", else None."""
    if model == "average":
        scores = _ok_scores(job)
        return round(sum(scores) / len(scores)) if scores else None
    entry = (job.get("enrichment") or {}).get(model)
    return entry["data"]["score"] if entry and entry.get("ok") else None


def _required_points(job, model):
    entry = (job.get("enrichment") or {}).get(model)
    if not entry or not entry.get("ok"):
        return 0
    return entry["data"].get("score_breakdown", {}).get("required_skills", 0)


def is_flagged(job):
    flags = job.get("jd_flags") or {}
    return bool(flags.get("sponsorship") is False or flags.get("clearance")
                or flags.get("us_citizenship_required") or job.get("review"))


def card(job, models):
    scores = {m: job_score(job, m) for m in models}
    present = [s for s in scores.values() if s is not None]
    return {
        "id": str(job["id"]), "title": job.get("posting_name"), "company": job.get("company"),
        "location": job.get("location"), "work_model": job.get("work_model"),
        "scraped_at": job.get("scraped_at"), "run_date": job.get("run_date"), "stage": job.get("stage"),
        "scores": scores, "boost": bool(job.get("boost")), "user_status": job.get("user_status"),
        "badges": {
            "models_differ": len(present) > 1 and max(present) - min(present) >= MODELS_DIFFER_BY,
            "no_apply_url": job.get("apply_type") == "external" and not job.get("apply_url"),
            "flagged": is_flagged(job),
        },
    }


def _search_text(job):
    parts = [job.get("posting_name") or "", job.get("company") or ""]
    for entry in (job.get("enrichment") or {}).values():
        if entry.get("ok"):
            data = entry["data"]
            parts += [s.get("skill", "") for s in data.get("required_skills", []) + data.get("preferred_skills", [])]
    return " ".join(parts).lower()


def _dates(range_, from_date, today):
    day = date.fromisoformat(today)
    if range_ == "yesterday":
        y = (day - timedelta(days=1)).isoformat()
        return y, y
    if range_ == "from":
        return from_date or today, None
    return today, today


def query(jobs, range="today", from_date=None, today=None, sort="score", show="all", status="any",
          model=DEFAULT_MODEL, q=""):
    """The page's data for one combination of controls."""
    today = today or date.today().isoformat()
    start, end = _dates(range, from_date, today)
    in_range = [j for j in jobs if (j.get("run_date") or "") >= start and (end is None or j.get("run_date") == end)]
    base = in_range
    if status != "any":
        wanted = None if status == "new" else status
        base = [j for j in base if j.get("user_status") == wanted]
    needle = (q or "").strip().lower()
    if needle:
        base = [j for j in base if needle in _search_text(j)]
    counts = {"all": len(base),
              "passed": sum(j.get("stage") == "passed" for j in base),
              "rejected": sum(j.get("stage") == "rejected" for j in base),
              "flagged": sum(is_flagged(j) for j in base),
              "boost": sum(bool(j.get("boost")) for j in base)}
    shown = {"passed": lambda j: j.get("stage") == "passed", "rejected": lambda j: j.get("stage") == "rejected",
             "flagged": is_flagged, "boost": lambda j: bool(j.get("boost"))}.get(show)
    listed = [j for j in base if shown(j)] if shown else list(base)

    if sort == "recent":
        listed.sort(key=lambda j: str(j["id"]))
        listed.sort(key=lambda j: j.get("scraped_at") or "", reverse=True)
    else:
        def key(j):
            score = job_score(j, model)
            return (score is None, -(score or 0), -_required_points(j, model), str(j["id"]))
        listed.sort(key=key)

    models = models_in(jobs)
    series = {m: [s for s in (job_score(j, m) for j in listed) if s is not None] for m in models}
    return {
        "cards": [card(j, models) for j in listed],
        "models": models,
        "series": series,
        "averages": {m: (round(sum(v) / len(v), 1) if v else None) for m, v in series.items()},
        "counts": counts,
        "range": {"from": start, "to": end},
        "latest_run_date": max((j["run_date"] for j in jobs if j.get("run_date")), default=None),
        "empty": not in_range,
    }

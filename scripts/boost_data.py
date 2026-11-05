"""Pure data helpers for the boost step: pick jobs, consolidate gaps, tally skills, check plans.

Nothing in here calls a model or touches files, so it is tested with plain dicts.
"""
import hashlib
import re


# ---------- selecting jobs ----------

def description_hash(text):
    return hashlib.sha1(" ".join((text or "").lower().split()).encode("utf-8")).hexdigest()


def passed_jobs(enriched, filtered):
    """Jobs the filter passed, in enriched order, identical postings removed (first one kept)."""
    passed_ids = {str(f["id"]) for f in filtered if f.get("stage") == "passed"}
    seen, out = set(), []
    for job in enriched:
        if str(job["id"]) not in passed_ids:
            continue
        h = description_hash(job.get("description"))
        if h in seen:
            continue
        seen.add(h)
        out.append(job)
    return out


def _score_of(job, model):
    """(score, model_used, required_points) from `model`, else from the first other ok model."""
    enrichment = job.get("enrichment") or {}
    for name in [model] + [m for m in enrichment if m != model]:
        entry = enrichment.get(name)
        if entry and entry.get("ok"):
            data = entry["data"]
            return data["score"], name, data.get("score_breakdown", {}).get("required_skills", 0)
    return None


def select_top(jobs, model, top_n):
    """Rank jobs by `model`'s score (fallback: another model's). Returns the best `top_n`."""
    scored = []
    for job in jobs:
        found = _score_of(job, model)
        if found:
            score, used, req = found
            scored.append((job, score, used, req))
    scored.sort(key=lambda t: (-t[1], -t[3], str(t[0]["id"])))
    return [{"job": job, "rank": i + 1, "score": score, "score_model": used, "fallback": used != model}
            for i, (job, score, used, _) in enumerate(scored[:top_n])]


# ---------- gaps ----------

def skill_key(name):
    """Comparable form of a skill name: lowercase, no parentheses, punctuation as spaces."""
    text = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    text = re.sub(r"[^a-z0-9+#.]+", " ", text)
    return " ".join(text.split()).strip(".")


def consolidate_gaps(job):
    """Skills the models say are missing (in_resume "no" or "similar"), merged across models."""
    enrichment = job.get("enrichment") or {}
    ok_models = [m for m, e in enrichment.items() if e.get("ok")]
    gaps = {}
    for model in ok_models:
        data = enrichment[model]["data"]
        for kind, items in (("required", data.get("required_skills", [])), ("preferred", data.get("preferred_skills", []))):
            for item in items:
                if item["in_resume"] == "yes":
                    continue
                key = skill_key(item["skill"])
                if not key:
                    continue
                gap = gaps.setdefault(key, {"skill": item["skill"], "kind": kind, "status": "similar",
                                            "flagged_by": [], "learnable_votes": 0})
                if kind == "required":
                    gap["kind"] = "required"
                if item["in_resume"] == "no":
                    gap["status"] = "no"
                if model not in gap["flagged_by"]:
                    gap["flagged_by"].append(model)
                    if item.get("learnable_in_a_day"):
                        gap["learnable_votes"] += 1
    out = []
    for gap in gaps.values():
        gap["flagged_by"] = sorted(gap["flagged_by"])
        gap["models_total"] = len(ok_models)
        gap["single_model"] = len(gap["flagged_by"]) == 1 and len(ok_models) > 1
        out.append(gap)
    return out

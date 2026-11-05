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


# ---------- tally ----------

def tally_gaps(jobs):
    """Count, over `jobs`, how many jobs each missing skill appears in."""
    rows = {}
    for job in jobs:
        for gap in consolidate_gaps(job):
            key = skill_key(gap["skill"])
            row = rows.setdefault(key, {"names": {}, "job_ids": [], "models_flagged_max": 0,
                                        "learnable": 0, "votes": 0, "required_count": 0, "preferred_count": 0})
            row["names"][gap["skill"]] = row["names"].get(gap["skill"], 0) + 1
            row["job_ids"].append(str(job["id"]))
            row["models_flagged_max"] = max(row["models_flagged_max"], len(gap["flagged_by"]))
            row["learnable"] += gap["learnable_votes"]
            row["votes"] += len(gap["flagged_by"])
            row[gap["kind"] + "_count"] += 1
    out = []
    for key, row in rows.items():
        name = max(row["names"].items(), key=lambda kv: kv[1])[0]  # ties: first seen (dict order)
        out.append({"skill": name, "jobs": len(row["job_ids"]), "job_ids": row["job_ids"],
                    "models_flagged_max": row["models_flagged_max"],
                    "learnable_share": row["learnable"] / row["votes"] if row["votes"] else 0.0,
                    "required_count": row["required_count"], "preferred_count": row["preferred_count"]})
    out.sort(key=lambda r: (-r["jobs"], skill_key(r["skill"])))
    return out


# ---------- projects and plan checks ----------

def parse_projects(projects_md):
    """Project names from a `Projects: A | B | C` line, else from `## N. Name (...)` headings."""
    for line in projects_md.splitlines():
        if line.strip().lower().startswith("projects:"):
            return [n.strip() for n in line.split(":", 1)[1].split("|") if n.strip()]
    names = []
    for line in projects_md.splitlines():
        m = re.match(r"##\s+\d+\.\s+(.+?)\s*(?:\(.*\))?\s*$", line)
        if m:
            names.append(m.group(1).strip())
    return names


def _norm(text):
    return " ".join((text or "").lower().split())


def check_plan(plan, resume_text, project_names):
    """Flag, never drop. Returns (copy of plan with check flags, warning strings)."""
    checked = {**plan,
               "rewordings": [dict(r) for r in plan.get("rewordings", [])],
               "project_extensions": [dict(e) for e in plan.get("project_extensions", [])]}
    resume = _norm(resume_text)
    known = {n.lower() for n in project_names}
    warnings = []
    for r in checked["rewordings"]:
        quote = _norm(r.get("current_text"))
        r["current_text_found"] = bool(quote) and quote in resume
        if not r["current_text_found"]:
            warnings.append("Reworded text not found in the resume: %r" % (r.get("current_text") or "")[:120])
    for e in checked["project_extensions"]:
        e["project_known"] = (e.get("project") or "").strip().lower() in known
        if not e["project_known"]:
            warnings.append("Project not found in projects.md: %r" % (e.get("project") or ""))
    return checked, warnings


def strip_em_dashes(value):
    """Replace every U+2014 in all strings, recursively. Project rule: no em dashes in text to copy."""
    if isinstance(value, str):
        return value.replace("—", " - ")
    if isinstance(value, list):
        return [strip_em_dashes(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_em_dashes(v) for k, v in value.items()}
    return value

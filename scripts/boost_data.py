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


CLOUD_DISPLAY = "Cloud platform (AWS, GCP or Azure)"
# (regex on skill_key, display name, terms to look for in the project summaries). Skills that
# are the same thing under different wordings are counted as one row. Add a row to merge more.
SKILL_ALIASES = [(re.compile(r"\b(aws|gcp|azure|cloud)\b"), CLOUD_DISPLAY, ["aws", "gcp", "azure"])]


def gap_key(name):
    key = skill_key(name)
    for pattern, display, _ in SKILL_ALIASES:
        if pattern.search(key):
            return skill_key(display)
    return key


def gap_display(name):
    key = skill_key(name)
    for pattern, display, _ in SKILL_ALIASES:
        if pattern.search(key):
            return display
    return name


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
                key = gap_key(item["skill"])
                if not key:
                    continue
                gap = gaps.setdefault(key, {"skill": gap_display(item["skill"]), "kind": kind, "status": "similar",
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
            key = gap_key(gap["skill"])
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
    out.sort(key=lambda r: (-r["jobs"], gap_key(r["skill"])))
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


def check_plan(plan, resume_text, project_names, gaps=None, projects_md=""):
    """Flag, never drop. Returns (copy of plan with check flags, warning strings).

    Flags: rewordings get current_text_found, adds_unsupported and adds_from_repo; extensions get
    project_known and (when `gaps` is given) covers_gap. Numbers in resume bullets must come
    from the resume or the project summaries.
    """
    checked = {**plan,
               "rewordings": [dict(r) for r in plan.get("rewordings", [])],
               "project_extensions": [dict(e) for e in plan.get("project_extensions", [])]}
    resume = _norm(resume_text)
    known = {n.lower() for n in project_names}
    repo_text = _norm(" ".join(l for lines in projects_index(projects_md).values() for l in lines))
    allowed_numbers = _numbers(resume_text) | _numbers(repo_text)
    warnings = []
    for r in checked["rewordings"]:
        quote = _norm(r.get("current_text"))
        r["current_text_found"] = bool(quote) and quote in resume
        if not r["current_text_found"]:
            warnings.append("Reworded text not found in the resume: %r" % (r.get("current_text") or "")[:120])
        new_words = [t for t in r.get("jd_skills_aligned", [])
                     if _has_term(_norm(r.get("suggested_text")), skill_key(t))
                     and not _has_term(_norm(r.get("current_text")), skill_key(t))]
        r["adds_from_repo"] = [t for t in new_words if not _has_term(resume, skill_key(t)) and _has_term(repo_text, skill_key(t))]
        r["adds_unsupported"] = [t for t in new_words if not _has_term(resume, skill_key(t)) and not _has_term(repo_text, skill_key(t))]
        for t in r["adds_unsupported"]:
            warnings.append("The rewording adds %r, which is not on your resume or in your project summaries." % t)
    for e in checked["project_extensions"]:
        e["project_known"] = (e.get("project") or "").strip().lower() in known
        if not e["project_known"]:
            warnings.append("Project not found in projects.md: %r" % (e.get("project") or ""))
        if gaps is not None:
            covered = e.get("jd_skills_covered", [])
            e["covers_gap"] = any(_same_skill(c, g["skill"]) for c in covered for g in gaps)
            if not e["covers_gap"]:
                warnings.append("The %s extension covers none of the listed gaps (%s)." % (e.get("project"), ", ".join(covered) or "no skills named"))
        for n in sorted(_numbers(e.get("resume_bullet_after_building")) - allowed_numbers):
            warnings.append("The suggested %s bullet contains %r, which is not in your resume or repos. "
                            "Replace it with your own measured result." % (e.get("project"), n))
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


# ---------- text for prompts ----------

def format_gaps(gaps):
    if not gaps:
        return "(none: the models found no clear gaps)"
    lines = []
    for g in gaps:
        status = "missing" if g["status"] == "no" else "only a similar skill is on the resume"
        line = "- %s | %s | %s | flagged by %d of %d models | learnable in a day: %d of %d" % (
            g["skill"], g["kind"], status, len(g["flagged_by"]), g["models_total"],
            g["learnable_votes"], len(g["flagged_by"]))
        if g["single_model"]:
            line = line.replace(" | learnable", " (flagged by one model, check it against the resume) | learnable")
        lines.append(line)
    return "\n".join(lines)


def format_tally(rows, limit=25):
    lines = []
    for r in rows[:limit]:
        line = "- %s: %d jobs (%d required, %d preferred), flagged by up to %d models, learnable in a day for %d%% of mentions" % (
            r["skill"], r["jobs"], r["required_count"], r["preferred_count"], r["models_flagged_max"],
            round(r["learnable_share"] * 100))
        if r.get("in_repos") and not r.get("on_resume"):
            line += ", already in your repos (%s) but not on the resume" % ", ".join(r["in_repos"])
        lines.append(line)
    return "\n".join(lines)


# ---------- what the repos already show ----------

def projects_index(projects_md):
    """{short project name: [lines]} of what each project IS, from resume/projects.md.

    Everything under a project's "**Not in the repo" line is left out, because it names
    things the project does not have. Other repos are the "- **name**" bullets of the
    "Other repos" section.
    """
    index, current, in_other, stop = {}, None, False, False
    for line in projects_md.splitlines():
        numbered = re.match(r"##\s+\d+\.\s+(.+?)\s*(?:\(.*\))?\s*$", line)
        if numbered:
            current, in_other, stop = numbered.group(1).split(",")[0].strip(), False, False
            index[current] = []
            continue
        if line.startswith("## "):
            current, in_other = None, True
            continue
        if in_other:
            bullet = re.match(r"-\s+\*\*(.+?)\*\*", line)
            if bullet:
                current = bullet.group(1).strip()
                index[current] = [line]
            elif current and line.strip():
                index[current].append(line)
            continue
        if current is None:
            continue
        if line.startswith("**Not in the repo"):
            stop = True
        if not stop:
            index[current].append(line)
    return index


def _has_term(text_lower, term):
    return len(term) >= 3 and bool(re.search(r"(?<![a-z0-9+#.])%s(?![a-z0-9+#])" % re.escape(term), text_lower))


def repo_mentions(skill, index, terms=None):
    """Names of the projects whose summary mentions the skill (or any of `terms`)."""
    terms = terms or [skill_key(skill)]
    return [name for name, lines in index.items()
            if any(_has_term(" ".join(lines).lower(), t) for t in terms)]


def annotate_repos(rows, projects_md, resume_text):
    """Copy of the tally rows with `in_repos` (projects that show the skill) and `on_resume`."""
    index = projects_index(projects_md)
    resume = " ".join((resume_text or "").lower().split())
    out = []
    for row in rows:
        terms = next((t for _, display, t in SKILL_ALIASES if row["skill"] == display), None) or [skill_key(row["skill"])]
        out.append({**row, "in_repos": repo_mentions(row["skill"], index, terms),
                    "on_resume": any(_has_term(resume, t) for t in terms)})
    return out


# ---------- cleaning a plan ----------

BULLET_PREFIX = "Write this only after you have built it: "


def _same_skill(a, b):
    ta, tb = set(gap_key(a).split()), set(gap_key(b).split())
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def clean_plan(plan):
    """Deterministic fixes for habits of small models. Returns a cleaned copy."""
    out = {**plan}
    out["rewordings"] = [dict(r) for r in plan.get("rewordings", [])
                         if _norm(r.get("suggested_text")) != _norm(r.get("current_text"))]
    extensions = []
    for e in plan.get("project_extensions", []):
        e = dict(e)
        e["pathway"] = [{**step, "step": re.sub(r"^\s*\d+\s*[-.):]\s*", "", step.get("step", ""))}
                        for step in e.get("pathway", [])]
        bullet = (e.get("resume_bullet_after_building") or "").strip()
        if bullet and not bullet.lower().startswith(BULLET_PREFIX.strip().lower()):
            e["resume_bullet_after_building"] = BULLET_PREFIX + bullet
        extensions.append(e)
    out["project_extensions"] = extensions
    covered = [s for e in extensions for s in e.get("jd_skills_covered", [])]
    if plan.get("new_project"):
        covered += plan["new_project"].get("jd_skills_covered", [])
    out["skipped_gaps"] = [g for g in plan.get("skipped_gaps", [])
                           if not any(_same_skill(g.get("skill", ""), c) for c in covered)]
    return out


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%?")


def _numbers(text):
    return {n.replace(" ", "").rstrip(",") for n in _NUMBER.findall(text or "")}

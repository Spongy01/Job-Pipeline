"""Render the readable daily boost report (boost.md) from the per-job records and the rollup."""


def _hours(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    return ("%g" % value) + " h"


def _list(items):
    return ", ".join(str(i) for i in items) if items else "none"


def _rollup_section(rollup):
    lines = ["## Across today's jobs", ""]
    if not rollup:
        return lines + ["No rollup was produced.", ""]
    if rollup.get("ok") is False:
        return lines + ["The rollup failed: %s" % rollup.get("error", "unknown error"), ""]
    if rollup.get("summary"):
        lines += [rollup["summary"], ""]
    tally = rollup.get("tally") or []
    if tally:
        lines += ["| Skill | Jobs | Models | Learnable in a day | In your repos |", "|---|---|---|---|---|"]
        for row in tally[:15]:
            lines.append("| %s | %d | %d | %d%% | %s |" % (
                row["skill"], row["jobs"], row["models_flagged_max"], round(row["learnable_share"] * 100),
                ", ".join(row.get("in_repos") or []) or "no"))
        lines.append("")
        add = [r for r in tally if r.get("in_repos") and not r.get("on_resume")]
        if add:
            lines += ["### Already in your repos, not on your resume", "",
                      "These need no new project. Check them against the repo and add them to the Technical Skills line.", ""]
            lines += ["- %s: asked for in %d job(s); shown in %s" % (r["skill"], r["jobs"], ", ".join(r["in_repos"])) for r in add]
            lines.append("")
    else:
        lines += ["No missing skills were found across the day's jobs.", ""]
    gp = rollup.get("general_project")
    if gp:
        what = "Upgrade %s" % gp["project"] if gp.get("kind") == "extension" and gp.get("project") else "New project"
        lines += ["### General project: %s (%s, about %s)" % (gp["name"], what, _hours(gp.get("effort_hours"))), ""]
        if gp.get("one_liner"):
            lines += [gp["one_liner"], ""]
        lines += ["Covers: %s" % _list(gp.get("jd_skills_covered")), ""]
        if gp.get("why"):
            lines += ["Why: %s" % gp["why"], ""]
        if rollup.get("spec_file"):
            lines += ["Spec: `%s`" % rollup["spec_file"], ""]
    for w in rollup.get("warnings", []):
        lines.append("- Check: %s" % w)
    if rollup.get("warnings"):
        lines.append("")
    return lines


def _job_section(record):
    job = record.get("job", {})
    head = "## %s. %s, %s" % (record.get("rank", "?"), job.get("company", "?"), job.get("posting_name", "?"))
    lines = [head, ""]
    meta = "Score %s (%s)" % (record.get("score", "?"), record.get("score_model", "?"))
    if record.get("fallback"):
        meta += ", ranked by a fallback model because the main model had no result"
    lines.append(meta)
    if job.get("linkedin_url"):
        lines.append("")
        lines.append(job["linkedin_url"])
    lines.append("")
    if not record.get("ok"):
        return lines + ["This job failed: %s" % record.get("error", "unknown error"), "",
                        "It will be retried on the next run.", ""]
    plan = record["plan"]
    lines += ["**%s**" % plan.get("verdict", ""), ""]

    if record.get("resume_additions"):
        lines += ["### Add to your resume (your repos already show these)", ""]
        lines += ["- %s: shown in %s" % (a["skill"], ", ".join(a["projects"])) for a in record["resume_additions"]]
        lines.append("")
    if plan.get("rewordings"):
        lines += ["### Rewordings", ""]
        for r in plan["rewordings"]:
            where = r.get("resume_location") or "Resume"
            lines += ["- **%s**" % where, "  - Now: %s" % r.get("current_text", ""),
                      "  - Suggested: %s" % r.get("suggested_text", "")]
            if r.get("added_text"):
                lines.append("  - New in this wording: %s" % r["added_text"])
            if r.get("jd_skills_aligned"):
                lines.append("  - Speaks to: %s" % _list(r["jd_skills_aligned"]))
            if r.get("why"):
                lines.append("  - Why: %s" % r["why"])
        lines.append("")
    if plan.get("project_extensions"):
        lines += ["### Extend an existing project", ""]
        for e in plan["project_extensions"]:
            lines += ["#### %s: %s (about %s)" % (e.get("project", "?"), e.get("what_to_build", ""), _hours(e.get("effort_hours"))), ""]
            lines.append("Adds: %s" % _list(e.get("jd_skills_covered")))
            if e.get("why_it_fits"):
                lines += ["", "Why it fits: %s" % e["why_it_fits"]]
            if e.get("pathway"):
                lines += ["", "Pathway:"]
                for i, step in enumerate(e["pathway"], 1):
                    detail = (" - " + step["detail"]) if step.get("detail") else ""
                    lines.append("%d. %s%s" % (i, step["step"], detail))
            if e.get("resume_bullet_after_building"):
                lines += ["", "Resume bullet: %s" % e["resume_bullet_after_building"]]
            lines.append("")
    np_ = plan.get("new_project")
    if np_:
        lines += ["### New project: %s (about %s)" % (np_["name"], _hours(np_.get("effort_hours"))), ""]
        if np_.get("one_liner"):
            lines += [np_["one_liner"], ""]
        lines.append("Covers: %s" % _list(np_.get("jd_skills_covered")))
        if np_.get("why_no_existing_project_fits"):
            lines += ["", "Why a new one: %s" % np_["why_no_existing_project_fits"]]
        if record.get("spec_file"):
            lines += ["", "Spec: `%s`" % record["spec_file"]]
        lines.append("")
    if plan.get("skipped_gaps"):
        lines += ["### Skipped gaps", ""]
        lines += ["- %s: %s" % (g["skill"], g.get("reason", "")) for g in plan["skipped_gaps"]]
        lines.append("")
    if record.get("warnings"):
        lines += ["### Check these", ""] + ["- %s" % w for w in record["warnings"]] + [""]
    return lines


def render_report(run_date, rollup, plans, notes):
    lines = ["# Boost report, %s" % run_date, ""]
    for n in notes:
        lines.append("- %s" % n)
    if notes:
        lines.append("")
    lines += _rollup_section(rollup)
    if not plans:
        lines += ["## Jobs", "", "No jobs were selected for a plan today.", ""]
    for record in plans:
        lines += _job_section(record)
    return "\n".join(lines).rstrip() + "\n"

#!/usr/bin/env python3
"""Usage: .venv/bin/python scripts/boost.py [YYYY-MM-DD] [options]

The boost step. After enrich and filter, takes the best jobs of the day and writes, with a
local Ollama model, a plan for each (rewordings, project extensions, a new project with a spec)
plus a daily rollup of the skills that keep coming up as missing.

Reads  runs/<date>/filtered.json, runs/<date>/enriched.json, config.json (resume_path, "models":
       {"booster": ...} and an optional "boost" block {"type": "highest" | "all", "booster_count": 5}),
       resume/projects.md.
Writes runs/<date>/boost/<job_id>.json, runs/<date>/boost/specs/*.md,
       runs/<date>/boost_rollup.json, runs/<date>/boost.md.
Never changes any input file. Prints one JSON line of counts; progress goes to stderr.
Exit is non-zero if an input is missing or corrupt, the model is not downloaded, or any
plan or the rollup failed.

Options:
  --top-n N            jobs to plan (default: config boost.type/booster_count: the best 5, or all passed jobs)
  --model M            Ollama model (default: config models.booster, else qwen3.5:9b)
  --prompt-version V   prompts/boost/<V>.py (default v1)
  --force              redo plans and the rollup that already exist for this prompt version
  --num-ctx N          context window (default 16384)
  --retries N          extra attempts when a reply is invalid (default 1)
  --think              let reasoning models think (off by default)
  --keep-loaded        do not unload the model when finished
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boost_data as bd  # noqa: E402
import boost_report  # noqa: E402
from clean import write_json_atomic  # noqa: E402
from enrich import load_resume_text  # noqa: E402
from llm import (LLMError, check_models, extract_json, generate_json, generate_text,  # noqa: E402
                 load_prompt, models_from_config, unload_model)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_TOP_N = 5
DEFAULT_NUM_CTX = 16384


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LLMError("%s not found: %s" % (what, path))
    except (ValueError, UnicodeDecodeError) as e:
        raise LLMError("%s is not valid JSON: %s (%s)" % (what, path, e))


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, data)


def write_text_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def slugify(text, limit=40):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit] or "project"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_spec_text(text):
    """Strip a wrapping code fence the model sometimes adds, and em dashes."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return bd.strip_em_dashes(text).strip() + "\n"


class Context:
    """Everything the model calls need, so the functions below stay short."""

    def __init__(self, client, model, prompt, resume_text, projects_md, num_ctx, think, retries):
        self.client, self.model, self.prompt = client, model, prompt
        self.resume_text, self.projects_md = resume_text, projects_md
        self.project_names = bd.parse_projects(projects_md)
        self.num_ctx, self.think, self.retries = num_ctx, think, retries

    def json_call(self, system, user, parse):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return generate_json(self.client, self.model, messages, parse, self.num_ctx, self.think, self.retries)

    def spec_call(self, context, outline):
        user = self.prompt.USER_TEMPLATE_SPEC.format(resume=self.resume_text, projects=self.projects_md,
                                                     context=context, outline=outline)
        messages = [{"role": "system", "content": self.prompt.SYSTEM_SPEC}, {"role": "user", "content": user}]
        return clean_spec_text(generate_text(self.client, self.model, messages, self.num_ctx, self.think))


def plan_job(ctx, selected):
    """Plan for one selected job. Returns (record, spec_text or None)."""
    job = selected["job"]
    gaps = bd.annotate_repos(bd.consolidate_gaps(job), ctx.projects_md, ctx.resume_text)
    record = {"prompt_version": ctx.prompt.VERSION, "generated_at": now(),
              "job": {"id": str(job["id"]), "company": job.get("company"), "posting_name": job.get("posting_name"),
                      "linkedin_url": job.get("linkedin_url")},
              "rank": selected["rank"], "score": selected["score"], "score_model": selected["score_model"],
              "fallback": selected["fallback"], "gaps": gaps,
              "resume_additions": bd.resume_additions(gaps)}
    started = time.time()
    user = ctx.prompt.USER_TEMPLATE_PLAN.format(
        resume=ctx.resume_text, projects=ctx.projects_md, title=job.get("posting_name") or "",
        company=job.get("company") or "", description=job.get("description") or "",
        gaps=bd.format_gaps(gaps), output_format=ctx.prompt.PLAN_FORMAT)
    plan, error, attempts = ctx.json_call(
        ctx.prompt.SYSTEM_PLAN, user, lambda raw: ctx.prompt.Plan.model_validate_json(extract_json(raw)))
    record.update({"seconds": round(time.time() - started, 1), "attempts": attempts})
    if plan is None:
        record.update({"ok": False, "error": error})
        return record, None
    cleaned = bd.clean_plan(bd.strip_em_dashes(plan.model_dump()))
    checked, warnings = bd.check_plan(cleaned, ctx.resume_text, ctx.project_names, gaps=gaps,
                                      projects_md=ctx.projects_md)
    spec_text = None
    if checked.get("new_project"):
        np_ = checked["new_project"]
        context = ("Job: %s at %s. Skills to cover: %s. %s" % (
            job.get("posting_name"), job.get("company"), ", ".join(np_.get("jd_skills_covered", [])),
            np_.get("why_no_existing_project_fits", "")))
        try:
            spec_text = ctx.spec_call(context, json.dumps(np_, indent=2))
            record["spec_file"] = "boost/specs/%s-%s.md" % (record["job"]["id"], slugify(np_["name"]))
        except Exception as e:
            warnings.append("The spec for the new project could not be written: %s" % e)
    record.update({"ok": True, "plan": checked, "warnings": warnings})
    return record, spec_text


def build_rollup(ctx, passed, tally_hash, tally):
    """Rollup record and spec text (or None). The tally is already computed in code."""
    record = {"prompt_version": ctx.prompt.VERSION, "generated_at": now(), "tally_hash": tally_hash,
              "tally": tally, "jobs_counted": len(passed)}
    if not tally:
        record.update({"ok": True, "summary": "", "general_project": None, "warnings": []})
        return record, None
    user = ctx.prompt.USER_TEMPLATE_ROLLUP.format(
        resume=ctx.resume_text, projects=ctx.projects_md, tally=bd.format_tally(tally),
        output_format=ctx.prompt.ROLLUP_FORMAT)
    result, error, attempts = ctx.json_call(
        ctx.prompt.SYSTEM_ROLLUP, user, lambda raw: ctx.prompt.Rollup.model_validate_json(extract_json(raw)))
    record["attempts"] = attempts
    if result is None:
        record.update({"ok": False, "error": error})
        return record, None
    data = bd.strip_em_dashes(result.model_dump())
    gp, warnings, spec_text = data["general_project"], [], None
    if gp["kind"] == "extension" and gp["project"].strip().lower() not in {n.lower() for n in ctx.project_names}:
        warnings.append("Project not found in projects.md: %r" % gp["project"])
    try:
        spec_text = ctx.spec_call("Rollup of the skills missing across today's jobs: %s. %s" % (
            ", ".join(gp["jd_skills_covered"]), gp["why"]), json.dumps(gp, indent=2))
        record["spec_file"] = "boost/specs/rollup-%s.md" % slugify(gp["name"])
    except Exception as e:
        warnings.append("The spec for the general project could not be written: %s" % e)
    from_repos = [r for r in tally if r.get("in_repos") and not r.get("on_resume")]
    for skill in gp["jd_skills_covered"]:
        for row in from_repos:
            if bd._same_skill(skill, row["skill"]):
                warnings.append("The general project covers %r, which is already in your repos (%s): add it to the "
                                "resume instead of building it." % (skill, ", ".join(row["in_repos"])))
    record.update({"ok": True, "summary": data["summary"], "general_project": gp, "warnings": warnings})
    return record, spec_text


def reusable(path, version, force):
    """The saved record at `path` if it is a finished result of this prompt version."""
    if force or not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return record if record.get("ok") and record.get("prompt_version") == version else None


def resolve_selection(boost_cfg, cli_top_n=None):
    """How many jobs get a plan: an int, or None for "all passed jobs".

    config.json "boost": {"type": "highest" | "all", "booster_count": N}. type "highest" (the default)
    plans the N best-scored passed jobs (booster_count, else the older top_n, else 5); "all" plans every
    passed job and ignores the count. A count given on the command line (--top-n) wins over the config.
    """
    if cli_top_n:
        return cli_top_n
    if not isinstance(boost_cfg, dict):
        raise LLMError('config.json "boost" must be an object like {"type": "highest", "booster_count": 5}')
    kind = boost_cfg.get("type", "highest")
    if kind not in ("highest", "all"):
        raise LLMError('config.json boost.type must be "highest" or "all", got %r' % (kind,))
    if kind == "all":
        return None
    count = boost_cfg.get("booster_count", boost_cfg.get("top_n", DEFAULT_TOP_N))
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise LLMError("config.json boost.booster_count must be a whole number of 1 or more, got %r" % (count,))
    return count


def run(root, run_date, top_n=None, model=None, prompt_version="v1", force=False, num_ctx=DEFAULT_NUM_CTX,
        retries=1, think=False, client=None, resume_text=None, unload=True, log=lambda m: None):
    root = Path(root)
    run_dir = root / "runs" / run_date
    config = load_json(root / "config.json", "config.json")
    boost_cfg = config.get("boost") or {}
    top_n = resolve_selection(boost_cfg, top_n)
    model = model or models_from_config(config)[1] or DEFAULT_MODEL
    filtered = load_json(run_dir / "filtered.json", "filtered.json")
    enriched = load_json(run_dir / "enriched.json", "enriched.json")
    if not isinstance(filtered, list) or not isinstance(enriched, list):
        raise LLMError("filtered.json and enriched.json must contain JSON lists")
    projects_path = root / "resume" / "projects.md"
    if not projects_path.is_file():
        raise LLMError("project summaries not found: %s" % projects_path)
    projects_md = projects_path.read_text(encoding="utf-8")
    prompt = load_prompt(prompt_version, root, "boost")
    if resume_text is None:
        resume_text = load_resume_text(root / config["resume_path"])
    if client is None:
        import ollama
        client = ollama.Client()
    check_models(client, [model])

    passed = bd.passed_jobs(enriched, filtered)
    if top_n is None:  # boost.type "all"
        top_n = len(passed)
    selection = bd.select_top(passed, model, top_n)
    notes = []
    if not passed:
        notes.append("No jobs passed the filter, so there is nothing to boost.")
    elif len(selection) < top_n:
        notes.append("Only %d job(s) could be planned (asked for %d)." % (len(selection), top_n))
    for s in selection:
        if s["fallback"]:
            notes.append("%s, %s was ranked by %s because %s had no result." % (
                s["job"].get("company"), s["job"].get("posting_name"), s["score_model"], model))

    ctx = Context(client, model, prompt, resume_text, projects_md, num_ctx, think, retries)
    boost_dir = run_dir / "boost"
    counts = {"selected": len(selection), "done": 0, "skipped_existing": 0, "failed": 0}
    records = []
    try:
        for s in selection:
            job_id = str(s["job"]["id"])
            path = boost_dir / (job_id + ".json")
            record = reusable(path, prompt.VERSION, force)
            if record:
                counts["skipped_existing"] += 1
                records.append(record)
                continue
            log("plan | %s | %s | %s" % (job_id, s["job"].get("company"), s["job"].get("posting_name")))
            record, spec_text = plan_job(ctx, s)
            if spec_text:
                write_text_atomic(run_dir / record["spec_file"], spec_text)
            write_json(path, record)
            counts["done" if record["ok"] else "failed"] += 1
            log("   %s in %ss" % ("ok" if record["ok"] else "FAILED: " + str(record.get("error")), record["seconds"]))
            records.append(record)

        rollup_path = run_dir / "boost_rollup.json"
        tally = bd.annotate_repos(bd.tally_gaps(passed), projects_md, resume_text)
        tally_hash = hashlib.sha1(json.dumps(tally, sort_keys=True).encode("utf-8")).hexdigest()
        rollup = reusable(rollup_path, prompt.VERSION, force)
        if rollup and rollup.get("tally_hash") == tally_hash:
            rollup_state = "skipped"
        else:
            log("rollup | %d jobs, %d missing skills" % (len(passed), len(tally)))
            rollup, spec_text = build_rollup(ctx, passed, tally_hash, tally)
            if spec_text:
                write_text_atomic(run_dir / rollup["spec_file"], spec_text)
            write_json(rollup_path, rollup)
            rollup_state = "done" if rollup["ok"] else "failed"
    finally:
        if unload:
            unload_model(client, model)
            log("unloaded %s" % model)

    write_text_atomic(run_dir / "boost.md", boost_report.render_report(run_date, rollup, records, notes))
    return {"date": run_date, "prompt_version": prompt.VERSION, "model": model, **counts, "rollup": rollup_state}


def main(argv):
    p = argparse.ArgumentParser(description="Plan resume and project boosts for the best jobs of the day.")
    p.add_argument("date", nargs="?", default=date.today().isoformat())
    p.add_argument("--top-n", type=int)
    p.add_argument("--model")
    p.add_argument("--prompt-version", default="v1")
    p.add_argument("--force", action="store_true")
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--think", action="store_true")
    p.add_argument("--keep-loaded", action="store_true")
    args = p.parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        p.error("date must be YYYY-MM-DD")
    try:
        summary = run(ROOT, args.date, top_n=args.top_n, model=args.model, prompt_version=args.prompt_version,
                      force=args.force, num_ctx=args.num_ctx, retries=args.retries, think=args.think,
                      unload=not args.keep_loaded, log=lambda m: print(m, file=sys.stderr, flush=True))
    except LLMError as e:
        sys.exit("boost.py: %s" % e)
    print(json.dumps(summary))
    sys.exit(1 if summary["failed"] or summary["rollup"] == "failed" else 0)


if __name__ == "__main__":
    main(sys.argv[1:])

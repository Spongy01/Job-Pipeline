#!/usr/bin/env python3
"""Usage: .venv/bin/python scripts/enrich.py [YYYY-MM-DD] [--models M1 [M2 ...]] [options]

Reads runs/<date>/clean.json, asks each local Ollama model to extract a summary,
experience, required/preferred skills (checked against the resume) and a rubric
score for every job, and stores the results in runs/<date>/enriched.json.

Models come from --models, or else from "models": {"enricher": ...} in config.json (a name or a list).

Options:
  --limit N            only the first N jobs
  --prompt-version V   prompt file prompts/enrich/<V>.py (default: v1)
  --input NAME         file inside runs/<date>/ to read (default: clean.json)
  --out-dir DIR        write enriched.json here instead of runs/<date>/
  --force              redo (job, model, prompt version) entries that already exist
  --num-ctx N          context window passed to Ollama (default: 8192)
  --retries N          extra attempts when a model returns invalid output (default: 1)
  --think              let reasoning models think (off by default, much faster)
  --keep-loaded        do not unload each model from memory when it is finished

enriched.json is a list of the clean.json jobs. Each gets an "enrichment" object
keyed by model name. Existing entries are kept, so models can be added in
separate runs. Prints one line of JSON (counts). Exit code is non-zero if the
server is unreachable, a model is missing, or any job/model pair failed.
"""
import argparse
import inspect
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean import write_json_atomic  # noqa: E402
from jd_flags import detect as detect_jd_flags  # noqa: E402
from llm import (LLMError, call_model, check_models, extract_json, generate_json,  # noqa: E402,F401
                 load_prompt as _load_prompt, models_from_config, unload_model)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NUM_CTX = 8192


EnrichError = LLMError


def load_prompt(version, root=ROOT):
    return _load_prompt(version, root, "enrich")


# ---------- inputs ----------

def clean_resume_text(text):
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # the PDF font kerning splits some words after a capital F or T ("F AISS", "LLM T raining")
    text = re.sub(r"\b([FT]) (?=[A-Za-z]{2,})", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_resume_text(pdf_path):
    from pypdf import PdfReader  # only needed here
    reader = PdfReader(str(pdf_path))
    text = clean_resume_text("\n".join((page.extract_text() or "") for page in reader.pages))
    if not text:
        raise EnrichError("no text could be extracted from %s" % pdf_path)
    return text


def resolve_models(cli_models, root=ROOT):
    """--models if given, else models.enricher from config.json."""
    if cli_models:
        return cli_models
    names, _ = models_from_config(load_json(Path(root) / "config.json", "config.json"))
    if not names:
        raise EnrichError('no models: pass --models, or set "models": {"enricher": [...]} in config.json')
    return names


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EnrichError("%s not found: %s" % (what, path))
    except (ValueError, UnicodeDecodeError) as e:
        raise EnrichError("%s is not valid JSON: %s (%s)" % (what, path, e))


# ---------- model call ----------

def build_messages(prompt, resume_text, job):
    user = prompt.USER_TEMPLATE.format(
        resume=resume_text,
        title=job.get("posting_name") or "",
        company=job.get("company") or "",
        location=job.get("location") or "unknown",
        work_model=job.get("work_model") or "unknown",
        description=job.get("description") or "",
        output_format=prompt.OUTPUT_FORMAT,
    )
    return [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": user}]


def enrich_one(client, model, prompt, resume_text, job, num_ctx, think, retries):
    messages = build_messages(prompt, resume_text, job)
    started = time.time()

    def parse(content):
        output = prompt.Output.model_validate_json(extract_json(content))
        if hasattr(prompt, "postprocess"):
            takes_job = len(inspect.signature(prompt.postprocess).parameters) > 1
            output = prompt.postprocess(output, job) if takes_job else prompt.postprocess(output)
        data = output.model_dump()
        data["score"], data["score_breakdown"] = prompt.score(output)
        del data["scores"]
        return data

    data, error, attempts = generate_json(client, model, messages, parse, num_ctx, think, retries)
    result = {
        "ok": data is not None,
        "prompt_version": prompt.VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.time() - started, 1),
        "attempts": attempts,
    }
    if data is not None:
        result["data"] = data
    else:
        result["error"] = error
    return result


# ---------- main flow ----------

def run(root, run_date, models, limit=None, prompt_version="v1", input_name="clean.json",
        out_dir=None, force=False, num_ctx=DEFAULT_NUM_CTX, retries=1, think=False,
        client=None, resume_text=None, unload=True, log=lambda msg: None):
    root = Path(root)
    run_dir = root / "runs" / run_date
    jobs = load_json(run_dir / input_name, input_name)
    if not isinstance(jobs, list):
        raise EnrichError("%s must contain a JSON list" % input_name)
    prompt = load_prompt(prompt_version, root)

    if resume_text is None:
        config = load_json(root / "config.json", "config.json")
        resume_text = load_resume_text(root / config["resume_path"])
    if client is None:
        import ollama
        client = ollama.Client()
    check_models(client, models)

    out_path = Path(out_dir or run_dir) / "enriched.json"
    existing = {}
    if out_path.exists():
        for job in load_json(out_path, "enriched.json"):
            existing[str(job["id"])] = job

    selected = jobs[:limit] if limit else jobs
    counts = {"jobs": len(selected), "done": 0, "skipped_existing": 0, "failed": 0, "skipped_closed": 0}
    entries = []
    for job in selected:
        entry = dict(job)
        entry["enrichment"] = dict(existing.get(str(job["id"]), {}).get("enrichment", {}))
        entries.append(entry)
    for job, entry in zip(selected, entries):
        if not job.get("closed") and job.get("description"):
            entry["jd_flags"] = detect_jd_flags(job["description"])  # keyword flags, no model
    counts["skipped_closed"] = sum(1 for j in selected if j.get("closed") or not j.get("description"))

    # one model at a time, so only one model has to be in memory
    for model in models:
        for job, entry in zip(selected, entries):
            if job.get("closed") or not job.get("description"):
                continue
            prev = entry["enrichment"].get(model)
            if prev and prev.get("ok") and prev.get("prompt_version") == prompt.VERSION and not force:
                counts["skipped_existing"] += 1
                continue
            log("%s | %s | %s" % (job["id"], model, job["posting_name"]))
            res = enrich_one(client, model, prompt, resume_text, job, num_ctx, think, retries)
            entry["enrichment"][model] = res
            if res["ok"]:
                counts["done"] += 1
                log("   score %s in %ss" % (res["data"]["score"], res["seconds"]))
            else:
                counts["failed"] += 1
                log("   FAILED: %s" % res["error"])
        if unload:
            unload_model(client, model)
            log("unloaded %s" % model)
    result_jobs = entries

    # keep previously enriched jobs that were not part of this run
    handled = {str(j["id"]) for j in result_jobs}
    result_jobs += [j for i, j in existing.items() if i not in handled]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out_path, result_jobs)
    return {"date": run_date, "prompt_version": prompt.VERSION, "models": models,
            "out": str(out_path), **counts}


def main(argv):
    p = argparse.ArgumentParser(description="Enrich jobs with local Ollama models.")
    p.add_argument("date", nargs="?", default=date.today().isoformat())
    p.add_argument("--models", nargs="+", help='default: models.enricher in config.json')
    p.add_argument("--limit", type=int)
    p.add_argument("--prompt-version", default="v1")
    p.add_argument("--input", default="clean.json")
    p.add_argument("--out-dir")
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
        summary = run(ROOT, args.date, resolve_models(args.models), limit=args.limit, prompt_version=args.prompt_version,
                      input_name=args.input, out_dir=args.out_dir, force=args.force,
                      num_ctx=args.num_ctx, retries=args.retries, think=args.think,
                      unload=not args.keep_loaded,
                      log=lambda m: print(m, file=sys.stderr, flush=True))
    except EnrichError as e:
        sys.exit("enrich.py: %s" % e)
    print(json.dumps(summary))
    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])

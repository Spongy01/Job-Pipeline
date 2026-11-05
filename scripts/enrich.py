#!/usr/bin/env python3
"""Usage: .venv/bin/python scripts/enrich.py [YYYY-MM-DD] --models M1 [M2 ...] [options]

Reads runs/<date>/clean.json, asks each local Ollama model to extract a summary,
experience, required/preferred skills (checked against the resume) and a rubric
score for every job, and stores the results in runs/<date>/enriched.json.

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
import importlib.util
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

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NUM_CTX = 8192


class EnrichError(Exception):
    pass


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


def load_prompt(version, root=ROOT):
    path = Path(root) / "prompts" / "enrich" / (version + ".py")
    if not re.fullmatch(r"v\d+", version) or not path.is_file():
        raise EnrichError("prompt version %r not found (expected %s)" % (version, path))
    spec = importlib.util.spec_from_file_location("enrich_prompt_" + version, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def call_model(client, model, messages, num_ctx, think):
    # No `format=` here: the MLX runner rejects structured output (HTTP 501),
    # so the JSON shape is requested in the prompt and validated afterwards.
    kwargs = dict(model=model, messages=messages,
                  options={"temperature": 0, "num_ctx": num_ctx}, think=think)
    try:
        resp = client.chat(**kwargs)
    except Exception as e:
        # some models reject the think flag; retry once without it
        if "think" in str(e).lower():
            kwargs.pop("think")
            resp = client.chat(**kwargs)
        else:
            raise
    return resp["message"]["content"] if isinstance(resp, dict) else resp.message.content


def extract_json(text):
    """Pull the JSON object out of a reply, tolerating code fences and chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return text[start:end + 1]


def enrich_one(client, model, prompt, resume_text, job, num_ctx, think, retries):
    messages = build_messages(prompt, resume_text, job)
    started = time.time()
    last_error = None
    for attempt in range(retries + 1):
        content = None
        try:
            content = call_model(client, model, messages, num_ctx, think)
            output = prompt.Output.model_validate_json(extract_json(content))
            if hasattr(prompt, "postprocess"):
                takes_job = len(inspect.signature(prompt.postprocess).parameters) > 1
                output = prompt.postprocess(output, job) if takes_job else prompt.postprocess(output)
            data = output.model_dump()
            data["score"], data["score_breakdown"] = prompt.score(output)
            del data["scores"]
            return {
                "ok": True,
                "prompt_version": prompt.VERSION,
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "seconds": round(time.time() - started, 1),
                "attempts": attempt + 1,
                "data": data,
            }
        except Exception as e:  # invalid JSON, schema violation, transport error
            last_error = "%s: %s" % (type(e).__name__, str(e)[:500])
            if content:  # repair turn: show the model what was wrong
                messages = messages[:2] + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "That reply was not valid. Problem: %s\nReturn the corrected JSON object only." % last_error},
                ]
    return {
        "ok": False,
        "prompt_version": prompt.VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.time() - started, 1),
        "attempts": retries + 1,
        "error": last_error,
    }


def unload_model(client, model):
    """Ask Ollama to drop the model from memory. Failure is not an error."""
    try:
        client.chat(model=model, messages=[], keep_alive=0)
    except Exception:
        pass


def check_models(client, models):
    try:
        listed = client.list()
    except Exception as e:
        raise EnrichError("cannot reach the Ollama server (is `ollama serve` running?): %s" % e)
    entries = listed["models"] if isinstance(listed, dict) else listed.models
    names = set()
    for m in entries:
        name = m["model"] if isinstance(m, dict) else m.model
        names.add(name)
        if name.endswith(":latest"):
            names.add(name[: -len(":latest")])
    missing = [m for m in models if m not in names and (m + ":latest") not in names]
    if missing:
        raise EnrichError("model(s) not downloaded: %s (have: %s)" % (", ".join(missing), ", ".join(sorted(names))))


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
    p.add_argument("--models", nargs="+", required=True)
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
        summary = run(ROOT, args.date, args.models, limit=args.limit, prompt_version=args.prompt_version,
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

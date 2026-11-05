#!/usr/bin/env python3
"""Usage: .venv/bin/python scripts/worker.py [--poll 2] [--prompt-version v2]

The streaming worker. Takes jobs from queue/pending one at a time, runs each through clean, enrich
(every model in config.json models.enricher), filter and a merge into jobs.json, and keeps going until
stoppipeline.py has been called and the queue is empty. Then it runs the end-of-run steps: the filter
and clean reports, boost, and the final merge (boost plans and the daily rollup), removes
queue/run.json and exits.

A job that fails twice goes to queue/failed/ and the worker carries on. If an end-of-run step fails,
queue/run.json stays, so starting the worker again retries only the end-of-run steps.
Needs Ollama running. Progress goes to stderr; prints one JSON line when it exits:
{"date", "processed", "failed", "end_steps": "ok" | "failed" | "skipped", "error"?}
Exit is non-zero if any job failed or an end-of-run step failed.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boost  # noqa: E402
import clean  # noqa: E402
import filter as jobfilter  # noqa: E402
import jobqueue  # noqa: E402
import merge  # noqa: E402
from enrich import load_resume_text, resolve_models  # noqa: E402
from llm import LLMError, check_models, load_prompt, unload_model  # noqa: E402
from jobslock import AlreadyRunning, worker_lock  # noqa: E402
from pipeline_job import JobPipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_VERSION = "v2"  # what the existing jobs in jobs.json were enriched with
ATTEMPTS = 2


def _load_config(root):
    try:
        return json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LLMError("config.json not found")
    except (ValueError, UnicodeDecodeError) as e:
        raise LLMError("config.json is not valid JSON (%s)" % e)


def _process_with_retry(pipeline, job_id, job, log):
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            outcome = pipeline.process(job)
            if outcome["failed_models"] and attempt < ATTEMPTS:
                log("   job %s: model(s) %s failed, trying them again" % (job_id, ", ".join(outcome["failed_models"])))
                continue
            return outcome, None
        except Exception as e:  # a model crash, a bad reply, a corrupt file: this job fails, the worker goes on
            last = "%s: %s" % (type(e).__name__, e)
            log("   job %s attempt %d failed: %s" % (job_id, attempt, last))
    return None, last


def _end_steps(root, run_date, client, resume_text, boost_run, log):
    """Returns ("ok" | "failed" | "skipped", error text or None)."""
    root = Path(root)
    run_dir = root / "runs" / run_date
    if not (run_dir / "filtered.json").exists():
        return "skipped", None
    errors = []

    def step(name, fn):
        log("end of run | " + name)
        try:
            return fn()
        except Exception as e:
            errors.append("%s: %s: %s" % (name, type(e).__name__, e))
            log("   FAILED " + errors[-1])
            return None

    step("filter report", lambda: jobfilter.rebuild_report(root, run_date))

    def clean_report():
        raw_path = run_dir / "raw.json"
        if raw_path.exists():
            clean.write_json_atomic(run_dir / "clean_report.json", clean.clean_all(json.loads(raw_path.read_text("utf-8")))[1])
    step("clean report", clean_report)

    result = step("boost", lambda: boost_run(root, run_date, client=client, resume_text=resume_text, log=log))
    if isinstance(result, dict) and (result.get("failed") or result.get("rollup") == "failed"):
        errors.append("boost: %s plan(s) failed, rollup %s" % (result.get("failed"), result.get("rollup")))
    step("final merge", lambda: merge.merge_run(root, run_date))
    return ("failed", "; ".join(errors)) if errors else ("ok", None)


def run_worker(root, **kwargs):
    """See _run_worker. Only one worker can run per project: a second one raises AlreadyRunning."""
    with worker_lock(root):
        return _run_worker(root, **kwargs)


def _run_worker(root, client=None, resume_text=None, boost_run=None, poll_seconds=2.0, sleep=time.sleep,
                log=lambda m: None, prompt=None, prompt_version=DEFAULT_PROMPT_VERSION):
    root = Path(root)
    boost_run = boost_run or boost.run
    # preflight: everything that can be wrong is checked before the queue is touched
    config = _load_config(root)
    models = resolve_models(None, root)
    rules = config.get("rules")
    if not isinstance(rules, dict):
        raise LLMError('config.json has no "rules" object')
    if prompt is None:
        prompt = load_prompt(prompt_version, root, "enrich")
    if resume_text is None:
        resume_text = load_resume_text(root / config["resume_path"])
    if client is None:
        import ollama
        client = ollama.Client()
    check_models(client, models)

    run = jobqueue.begin_run(root)
    run_date = run["date"]
    recovered = jobqueue.recover(root)
    if recovered:
        log("recovered %d job(s) left half-done by an earlier run" % recovered)
    pipeline = JobPipeline(root, run_date, client, models, prompt, resume_text, rules)
    summary = {"date": run_date, "processed": 0, "failed": 0}
    try:
        while True:
            current = jobqueue.read_run(root)
            stopping = bool(current and current["stopping"])  # read first: nothing new is accepted once it is set
            claimed = jobqueue.claim_next(root)
            if claimed:
                job_id, job = claimed
                log("job %s | %s | %s" % (job_id, job.get("company"), job.get("posting_name")))
                outcome, error = _process_with_retry(pipeline, job_id, job, log)
                if outcome:
                    jobqueue.complete(root, job_id)
                    summary["processed"] += 1
                    log("   %s (%s)" % (outcome["stage"], outcome["merge"]))
                else:
                    try:
                        check_models(client, models)
                    except LLMError as down:  # Ollama or a model is gone: this is not the job's fault
                        jobqueue.release(root, job_id)
                        log("   Ollama is not usable (%s); job %s stays queued, waiting" % (down, job_id))
                        sleep(poll_seconds * 5)
                        continue
                    jobqueue.fail(root, job_id, error)
                    pipeline.discard(job_id)
                    summary["failed"] += 1
                continue
            if stopping:
                break
            sleep(poll_seconds)

        status, error = _end_steps(root, run_date, client, resume_text, boost_run, log)
        summary["end_steps"] = status
        if error:
            summary["error"] = error
        else:
            jobqueue.end_run(root)
    finally:
        for model in models:
            unload_model(client, model)
    return summary


def main(argv):
    p = argparse.ArgumentParser(description="Process queued jobs until stoppipeline.py is called.")
    p.add_argument("--poll", type=float, default=2.0, help="seconds to wait when the queue is empty")
    p.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    args = p.parse_args(argv)
    try:
        summary = run_worker(ROOT, poll_seconds=args.poll, prompt_version=args.prompt_version,
                             log=lambda m: print(m, file=sys.stderr, flush=True))
    except (LLMError, jobqueue.QueueError, AlreadyRunning) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    print(json.dumps(summary))
    sys.exit(1 if summary["failed"] or summary.get("end_steps") == "failed" else 0)


if __name__ == "__main__":
    main(sys.argv[1:])

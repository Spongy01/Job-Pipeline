"""Process one scraped job through clean, enrich, filter and merge (used by the worker).

Reuses the batch scripts' own functions, and writes the same per-run files (clean.json,
enriched.json, filtered.json) one job at a time, replacing the entry with the same id so a
retry never duplicates. Models that already have an ok result for this prompt version are not
run again, so a job that failed half-way resumes where it stopped.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filter as jobfilter  # noqa: E402
from clean import clean_job, validate, write_json_atomic  # noqa: E402
from enrich import enrich_one  # noqa: E402
from jd_flags import detect as detect_jd_flags  # noqa: E402
from merge import merge_one  # noqa: E402


class PipelineError(Exception):
    pass


def _read_list(path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise PipelineError("%s is not valid JSON: %s" % (path.name, e))
    if not isinstance(data, list):
        raise PipelineError("%s must contain a JSON list" % path.name)
    return data


def _find(entries, job_id):
    return next((e for e in entries if str(e.get("id")) == job_id), None)


def _upsert(path, entry):
    entries = _read_list(path)
    for i, existing in enumerate(entries):
        if str(existing.get("id")) == entry["id"]:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, entries)


class JobPipeline:
    def __init__(self, root, run_date, client, models, prompt, resume_text, rules,
                 filter_model=jobfilter.DEFAULT_MODEL, num_ctx=8192, retries=1, think=False):
        self.root = Path(root)
        self.run_date = run_date
        self.run_dir = self.root / "runs" / run_date
        self.client, self.models, self.prompt = client, list(models), prompt
        self.resume_text, self.rules, self.filter_model = resume_text, rules, filter_model
        self.num_ctx, self.retries, self.think = num_ctx, retries, think

    def process(self, raw_job):
        job_id, reason = validate(raw_job)
        if reason:
            raise PipelineError("invalid job: %s" % reason)
        clean, _ = clean_job(raw_job, job_id)
        _upsert(self.run_dir / "clean.json", clean)

        enriched_path = self.run_dir / "enriched.json"
        previous = _find(_read_list(enriched_path), job_id) or {}
        entry = dict(clean)
        entry["enrichment"] = dict(previous.get("enrichment") or {})
        needs_models = not clean.get("closed") and bool(clean.get("description"))
        if needs_models:
            entry["jd_flags"] = detect_jd_flags(clean["description"])
            try:
                for model in self.models:
                    prev = entry["enrichment"].get(model)
                    if prev and prev.get("ok") and prev.get("prompt_version") == self.prompt.VERSION:
                        continue
                    entry["enrichment"][model] = enrich_one(
                        self.client, model, self.prompt, self.resume_text, clean,
                        self.num_ctx, self.think, self.retries)
            finally:
                _upsert(enriched_path, entry)  # keep the models that finished, even if a later one raised
            if not any(r.get("ok") for r in entry["enrichment"].values()):
                raise PipelineError("no model produced a usable result for job %s" % job_id)
        else:
            _upsert(enriched_path, entry)

        decision = jobfilter.evaluate(entry, self.rules, self.filter_model)
        _upsert(self.run_dir / "filtered.json", decision)
        merged = merge_one(self.root, self.run_date, entry, decision)
        return {"id": job_id, "stage": decision["stage"], "merge": merged}

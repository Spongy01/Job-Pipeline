import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pipeline_job  # noqa: E402

DATE = "2026-11-05"
M9, MG = "qwen3.5:9b", "gemma4:e4b"
RULES = {"experience_years": {"min": 0, "max": 3}, "exclude_us_citizenship_required": True,
         "exclude_clearance": True, "exclude_sponsorship_denied": True}


class Prompt:
    VERSION = "v-test"


def good(years=((("overall"), "required", 1, 1),)):
    return {"ok": True, "prompt_version": "v-test", "generated_at": "t", "seconds": 0.1, "attempts": 1,
            "data": {"score": 77, "score_breakdown": {}, "required_skills": [], "preferred_skills": [],
                     "experience": [{"area": a, "level": lv, "years_min": lo, "years_max": hi, "evidence": "e"}
                                    for a, lv, lo, hi in years]}}


def bad():
    return {"ok": False, "prompt_version": "v-test", "generated_at": "t", "seconds": 0.1, "attempts": 2, "error": "nope"}


def raw_job(i="100", **over):
    job = {"id": i, "posting_name": "Engineer", "company": "Acme", "linkedin_url": "https://x/" + i,
           "description": "Build things at Acme. " * 20, "scraped_at": "2026-11-05T10:00:00Z",
           "posted_text": "2 hours ago", "apply_type": "external", "apply_url": "https://acme.com/apply"}
    job.update(over)
    return job


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []
        self.results = {}  # (model) -> result or exception
        self.orig = pipeline_job.enrich_one
        pipeline_job.enrich_one = self.fake_enrich
        self.addCleanup(setattr, pipeline_job, "enrich_one", self.orig)

    def fake_enrich(self, client, model, prompt, resume_text, job, num_ctx, think, retries):
        self.calls.append((job["id"], model))
        outcome = self.results.get(model, good())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def pipe(self, models=(M9, MG), rules=RULES):
        return pipeline_job.JobPipeline(self.tmp, DATE, client=object(), models=list(models), prompt=Prompt,
                                        resume_text="resume", rules=rules)

    def run_file(self, name):
        return json.loads((self.tmp / "runs" / DATE / name).read_text())

    def jobs(self):
        return json.loads((self.tmp / "jobs.json").read_text())


class ProcessTests(PipelineTestCase):
    def test_open_job_runs_every_model_and_lands_in_jobs_json(self):
        out = self.pipe().process(raw_job())
        self.assertEqual(out, {"id": "100", "stage": "passed", "merge": "added"})
        self.assertEqual(self.calls, [("100", M9), ("100", MG)])
        record = self.jobs()[0]
        self.assertEqual(sorted(record["enrichment"]), sorted([M9, MG]))
        self.assertEqual((record["stage"], record["run_date"], record["user_status"]), ("passed", DATE, None))
        self.assertIn("jd_flags", record)
        for name in ("clean.json", "enriched.json", "filtered.json"):
            self.assertEqual([j["id"] for j in self.run_file(name)], ["100"])

    def test_files_updated_without_duplicates_when_processed_twice(self):
        p = self.pipe()
        p.process(raw_job())
        out = p.process(raw_job())
        self.assertEqual(out["merge"], "unchanged")
        for name in ("clean.json", "enriched.json", "filtered.json"):
            self.assertEqual(len(self.run_file(name)), 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_retry_skips_models_already_ok(self):
        self.results[MG] = RuntimeError("ollama down")
        with self.assertRaises(RuntimeError):
            self.pipe().process(raw_job())
        self.calls.clear()
        self.results.clear()
        self.pipe().process(raw_job())
        self.assertEqual(self.calls, [("100", MG)])

    def test_closed_job_makes_no_model_call_and_lands_closed(self):
        out = self.pipe().process(raw_job(closed=True))
        self.assertEqual((self.calls, out["stage"]), ([], "closed"))
        self.assertEqual(self.jobs()[0]["stage"], "closed")

    def test_job_without_description_makes_no_model_call(self):
        self.pipe().process(raw_job(description=""))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.jobs()), 1)

    def test_all_models_failed_raises_pipeline_error(self):
        self.results = {M9: bad(), MG: bad()}
        with self.assertRaises(pipeline_job.PipelineError):
            self.pipe().process(raw_job())
        self.assertFalse((self.tmp / "jobs.json").exists())

    def test_one_model_failing_still_lands_the_job(self):
        self.results = {M9: bad()}
        self.assertEqual(self.pipe().process(raw_job())["merge"], "added")

    def test_rejected_job_is_in_jobs_json_with_stage_rejected(self):
        self.results = {M9: good(years=(("overall", "required", 6, 8),))}
        out = self.pipe().process(raw_job())
        self.assertEqual(out["stage"], "rejected")
        self.assertEqual(self.jobs()[0]["stage"], "rejected")
        self.assertEqual(self.jobs()[0]["filter"]["reasons"][0]["rule"], "experience_years")

    def test_invalid_job_raises(self):
        job = raw_job()
        del job["company"]
        with self.assertRaises(pipeline_job.PipelineError):
            self.pipe().process(job)

    def test_discard_removes_partial_entries_from_run_files(self):
        self.pipe().process(raw_job("100"))
        self.results[MG] = RuntimeError("down")
        p = self.pipe()
        with self.assertRaises(RuntimeError):
            p.process(raw_job("200"))
        p.discard("200")
        for name in ("clean.json", "enriched.json", "filtered.json"):
            self.assertEqual([j["id"] for j in self.run_file(name)], ["100"])

    def test_corrupt_run_file_raises_and_is_not_replaced(self):
        (self.tmp / "runs" / DATE).mkdir(parents=True)
        (self.tmp / "runs" / DATE / "clean.json").write_text("[{corrupt")
        with self.assertRaises(pipeline_job.PipelineError):
            self.pipe().process(raw_job())
        self.assertEqual((self.tmp / "runs" / DATE / "clean.json").read_text(), "[{corrupt")


if __name__ == "__main__":
    unittest.main()

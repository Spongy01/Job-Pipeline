import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jobqueue  # noqa: E402
import llm  # noqa: E402
import pipeline_job  # noqa: E402
import worker  # noqa: E402

DATE = "2026-11-05"
M9, MG = "qwen3.5:9b", "gemma4:e4b"
RULES = {"experience_years": {"min": 0, "max": 3}, "exclude_us_citizenship_required": True,
         "exclude_clearance": True, "exclude_sponsorship_denied": True}


class Prompt:
    VERSION = "v-test"


def good():
    return {"ok": True, "prompt_version": "v-test", "generated_at": "t", "seconds": 0.1, "attempts": 1,
            "data": {"score": 77, "score_breakdown": {}, "required_skills": [], "preferred_skills": [],
                     "experience": [{"area": "overall", "level": "required", "years_min": 1, "years_max": 1, "evidence": "e"}]}}


def raw_job(i, **over):
    job = {"id": i, "posting_name": "Engineer " + i, "company": "Acme", "linkedin_url": "https://x/" + i,
           "description": "Build things at Acme. " * 20, "scraped_at": "2026-11-05T10:00:00Z", "posted_text": "1 hour ago",
           "apply_type": "external", "apply_url": "https://acme.com/apply"}
    job.update(over)
    return job


class FakeClient:
    def __init__(self, fail_list=False):
        self.fail_list = fail_list
        self.unloaded = []

    def list(self):
        if self.fail_list:
            raise ConnectionError("server down")
        return {"models": [{"model": M9}, {"model": MG}]}

    def chat(self, model, messages, keep_alive):
        self.unloaded.append(model)


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "config.json").write_text(json.dumps({
            "resume_path": "resume/r.pdf", "models": {"enricher": [M9, MG], "booster": M9}, "rules": RULES}))
        jobqueue.begin_run(self.tmp, DATE)
        self.client = FakeClient()
        self.enrich_calls = []
        self.broken_ids = set()
        self.boost_calls = []
        self.boost_error = None
        self.boost_result = {"failed": 0, "rollup": "done"}
        orig = pipeline_job.enrich_one
        pipeline_job.enrich_one = self.fake_enrich
        self.addCleanup(setattr, pipeline_job, "enrich_one", orig)

    def fake_enrich(self, client, model, prompt, resume_text, job, num_ctx, think, retries):
        self.enrich_calls.append((job["id"], model))
        if job["id"] in self.broken_ids:
            raise RuntimeError("model crashed")
        return good()

    def fake_boost(self, root, run_date, **kwargs):
        self.boost_calls.append(run_date)
        if self.boost_error:
            raise self.boost_error
        return self.boost_result

    def queue(self, *ids):
        for i in ids:
            jobqueue.put(self.tmp, i, raw_job(i))

    def go(self, stop_after_sleeps=1, client=None, on_sleep=None):
        sleeps = []

        def sleep(_):
            sleeps.append(len(self.boost_calls))
            if on_sleep:
                on_sleep(len(sleeps))
            if len(sleeps) >= stop_after_sleeps:
                jobqueue.set_stopping(self.tmp)

        self.sleeps = sleeps
        return worker.run_worker(self.tmp, client=client or self.client, resume_text="resume", prompt=Prompt,
                                 boost_run=self.fake_boost, poll_seconds=0, sleep=sleep)

    def jobs(self):
        return json.loads((self.tmp / "jobs.json").read_text())

    def run_file(self, name):
        return json.loads((self.tmp / "runs" / DATE / name).read_text())


class WorkerTests(WorkerTestCase):
    def test_processes_queued_jobs_then_runs_end_steps_after_stop(self):
        self.queue("1", "2", "3")
        result = self.go()
        self.assertEqual((result["processed"], result["failed"], result["end_steps"]), (3, 0, "ok"))
        self.assertEqual(sorted(j["id"] for j in self.jobs()), ["1", "2", "3"])
        self.assertEqual(self.boost_calls, [DATE])
        for name in ("clean.json", "enriched.json", "filtered.json"):
            self.assertEqual(len(self.run_file(name)), 0 + 3)
        self.assertTrue((self.tmp / "runs" / DATE / "filter_report.json").exists())
        self.assertIsNone(jobqueue.read_run(self.tmp))

    def test_end_steps_wait_for_stop(self):
        self.queue("1")
        self.go(stop_after_sleeps=3)
        self.assertEqual(self.sleeps, [0, 0, 0])  # boost had not run during any idle wait
        self.assertEqual(self.boost_calls, [DATE])

    def test_job_enqueued_before_stop_is_never_dropped(self):
        self.queue("1", "2")
        jobqueue.set_stopping(self.tmp)
        result = self.go()
        self.assertEqual(result["processed"], 2)
        self.assertEqual(len(self.jobs()), 2)

    def test_crash_recovery_job_in_processing_is_done_once(self):
        self.queue("1")
        jobqueue.claim_next(self.tmp)  # worker died holding it
        pipeline_job.JobPipeline(self.tmp, DATE, self.client, [M9, MG], Prompt, "r", RULES).process(raw_job("1"))
        self.go()
        for name in ("clean.json", "enriched.json", "filtered.json"):
            self.assertEqual(len(self.run_file(name)), 1)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(jobqueue.counts(self.tmp), {"pending": 0, "processing": 0, "failed": 0})

    def test_job_failing_twice_goes_to_failed_and_others_continue(self):
        self.broken_ids = {"2"}
        self.queue("1", "2", "3")
        result = self.go()
        self.assertEqual((result["processed"], result["failed"]), (2, 1))
        self.assertEqual(jobqueue.state_of(self.tmp, "2"), "failed")
        self.assertEqual(sorted(j["id"] for j in self.jobs()), ["1", "3"])
        self.assertEqual(result["end_steps"], "ok")
        self.assertEqual(self.enrich_calls.count(("2", M9)), 2)  # tried twice

    def test_preflight_failure_leaves_queue_untouched(self):
        self.queue("1")
        with self.assertRaises(llm.LLMError):
            self.go(client=FakeClient(fail_list=True))
        self.assertEqual(jobqueue.counts(self.tmp), {"pending": 1, "processing": 0, "failed": 0})
        self.assertEqual(self.enrich_calls, [])

    def test_end_step_failure_keeps_run_json_and_second_start_retries_end_steps_only(self):
        self.queue("1")
        self.boost_error = RuntimeError("boost exploded")
        result = self.go()
        self.assertEqual(result["end_steps"], "failed")
        self.assertIn("boost exploded", result["error"])
        self.assertEqual(jobqueue.read_run(self.tmp), {"date": DATE, "stopping": True})
        calls_before = len(self.enrich_calls)
        self.boost_error = None
        result = self.go()
        self.assertEqual(result["end_steps"], "ok")
        self.assertEqual(len(self.enrich_calls), calls_before)
        self.assertIsNone(jobqueue.read_run(self.tmp))

    def test_boost_reporting_failed_plans_counts_as_end_step_failure(self):
        self.queue("1")
        self.boost_result = {"failed": 1, "rollup": "done"}
        result = self.go()
        self.assertEqual(result["end_steps"], "failed")
        self.assertIsNotNone(jobqueue.read_run(self.tmp))
        self.assertEqual(len(self.jobs()), 1)  # the merge still ran

    def test_success_removes_run_json(self):
        self.queue("1")
        self.go()
        self.assertIsNone(jobqueue.read_run(self.tmp))

    def test_stop_with_empty_queue_and_no_jobs_skips_end_steps(self):
        jobqueue.set_stopping(self.tmp)
        result = self.go()
        self.assertEqual((result["processed"], result["end_steps"]), (0, "skipped"))
        self.assertEqual(self.boost_calls, [])
        self.assertIsNone(jobqueue.read_run(self.tmp))

    def test_models_unloaded_on_exit(self):
        self.queue("1")
        self.go()
        self.assertEqual(sorted(self.client.unloaded), sorted([M9, MG]))

    def test_jobs_arriving_while_waiting_are_picked_up(self):
        self.queue("1")
        self.go(stop_after_sleeps=2, on_sleep=lambda n: self.queue("2") if n == 1 else None)
        self.assertEqual(sorted(j["id"] for j in self.jobs()), ["1", "2"])


if __name__ == "__main__":
    unittest.main()

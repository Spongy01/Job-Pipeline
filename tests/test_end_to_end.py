import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import enqueue  # noqa: E402
import jobqueue  # noqa: E402
import pipeline_job  # noqa: E402
import serve  # noqa: E402
import stoppipeline  # noqa: E402
import worker  # noqa: E402

DATE = "2026-11-05"
M9, MG = "qwen3.5:9b", "gemma4:e4b"
RULES = {"experience_years": {"min": 0, "max": 3}, "exclude_us_citizenship_required": True,
         "exclude_clearance": True, "exclude_sponsorship_denied": True}


class Prompt:
    VERSION = "v-test"


class Client:
    def __init__(self):
        self.unloaded = []

    def list(self):
        return {"models": [{"model": M9}, {"model": MG}]}

    def chat(self, model, messages, keep_alive):
        self.unloaded.append(model)


def result(years):
    return {"ok": True, "prompt_version": "v-test", "generated_at": "t", "seconds": 0.1, "attempts": 1,
            "data": {"score": 70, "score_breakdown": {}, "required_skills": [], "preferred_skills": [],
                     "experience": [{"area": "overall", "level": "required", "years_min": years, "years_max": years, "evidence": "e"}]}}


def raw_job(i, **over):
    job = {"id": i, "posting_name": "Engineer " + i, "company": "Acme", "linkedin_url": "https://x/" + i,
           "description": "Build things at Acme. " * 20, "scraped_at": "2026-11-05T10:00:00Z", "posted_text": "1 hour ago",
           "apply_type": "external", "apply_url": "https://acme.com/apply"}
    job.update(over)
    return job


class EndToEndTests(unittest.TestCase):
    def test_enqueue_worker_stop_roundtrip(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "config.json").write_text(json.dumps({
            "resume_path": "resume/r.pdf", "models": {"enricher": [M9, MG], "booster": M9}, "rules": RULES}))
        orig = pipeline_job.enrich_one
        pipeline_job.enrich_one = lambda client, model, prompt, resume, job, *a: result(7 if job["id"] == "2" else 1)
        self.addCleanup(setattr, pipeline_job, "enrich_one", orig)
        boost_calls = []

        def fake_boost(root, run_date, **kwargs):
            # boost must only run after every job is in jobs.json
            boost_calls.append((run_date, len(json.loads((Path(root) / "jobs.json").read_text()))))
            return {"failed": 0, "rollup": "done"}

        client, summary, errors = Client(), {}, []

        def run():
            try:
                summary.update(worker.run_worker(tmp, client=client, resume_text="r", prompt=Prompt, boost_run=fake_boost,
                                                 poll_seconds=0.01))
            except Exception as e:  # surfaced below
                errors.append(e)

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 10)
        for job in (raw_job("1"), raw_job("2"), raw_job("3", closed=True, description=None)):
            self.assertEqual(enqueue.enqueue(tmp, job, today=DATE)["status"], "queued")

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            path = tmp / "jobs.json"
            if path.exists() and len(json.loads(path.read_text())) == 3:
                break
            time.sleep(0.02)
        self.assertEqual(boost_calls, [])  # not before stop
        serve.JobsStore(tmp).set_status("1", "applied")  # the page writes while the worker is still running
        self.assertEqual(stoppipeline.stop(tmp)["status"], "stopping")
        with self.assertRaises(enqueue.EnqueueError):
            enqueue.enqueue(tmp, raw_job("4"), today=DATE)
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

        jobs = {j["id"]: j for j in json.loads((tmp / "jobs.json").read_text())}
        self.assertEqual({i: j["stage"] for i, j in jobs.items()}, {"1": "passed", "2": "rejected", "3": "closed"})
        self.assertEqual(jobs["1"]["user_status"], "applied")  # survived the worker's later writes
        self.assertEqual(boost_calls, [(DATE, 3)])
        run_dir = tmp / "runs" / DATE
        for name in ("raw.json", "clean.json", "enriched.json", "filtered.json"):
            self.assertEqual(len(json.loads((run_dir / name).read_text())), 3, name)
        self.assertTrue((run_dir / "filter_report.json").exists())
        self.assertTrue((run_dir / "clean_report.json").exists())
        self.assertIsNone(jobqueue.read_run(tmp))
        self.assertEqual((summary["processed"], summary["failed"], summary["end_steps"]), (3, 0, "ok"))
        self.assertEqual(sorted(client.unloaded), sorted([M9, MG]))


if __name__ == "__main__":
    unittest.main()

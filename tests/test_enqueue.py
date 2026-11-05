import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import enqueue  # noqa: E402
import jobqueue  # noqa: E402
import stoppipeline  # noqa: E402

DATE = "2026-11-05"


def raw_job(i="100", **over):
    job = {"id": i, "posting_name": "Engineer", "company": "Acme", "linkedin_url": "https://linkedin.com/jobs/view/%s" % i,
           "description": "Build things.", "scraped_at": "2026-11-05T10:00:00Z"}
    job.update(over)
    return job


class EnqueueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "scripts").mkdir()
        for name in ("enqueue.py", "stoppipeline.py", "jobqueue.py", "clean.py", "jobslock.py"):
            shutil.copy(SCRIPTS / name, self.tmp / "scripts" / name)

    def cli(self, script, *args, stdin=None):
        return subprocess.run([sys.executable, str(self.tmp / "scripts" / script), *args], input=stdin,
                              capture_output=True, text=True, cwd="/", timeout=30)

    def raw(self, date=DATE):
        return json.loads((self.tmp / "runs" / date / "raw.json").read_text())


class EnqueueTests(EnqueueTestCase):
    def test_queued_job_in_raw_and_pending(self):
        result = enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertEqual(result, {"status": "queued", "id": "100", "date": DATE, "pending": 1})
        self.assertEqual([j["id"] for j in self.raw()], ["100"])
        self.assertEqual(jobqueue.state_of(self.tmp, "100"), "pending")

    def test_id_is_normalized_to_a_string(self):
        enqueue.enqueue(self.tmp, raw_job(100), today=DATE)
        self.assertEqual(self.raw()[0]["id"], "100")
        self.assertEqual(jobqueue.state_of(self.tmp, "100"), "pending")

    def test_duplicate_in_jobs_json(self):
        (self.tmp / "jobs.json").write_text(json.dumps([{"id": "100"}]))
        self.assertEqual(enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)["status"], "duplicate")
        self.assertFalse((self.tmp / "runs").exists())
        self.assertIsNone(jobqueue.state_of(self.tmp, "100"))

    def test_duplicate_in_queue(self):
        enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertEqual(enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)["status"], "duplicate")
        self.assertEqual(len(self.raw()), 1)

    def test_failed_job_is_requeued(self):
        enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        jobqueue.claim_next(self.tmp)
        jobqueue.fail(self.tmp, "100", "boom")
        self.assertEqual(enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)["status"], "requeued")
        self.assertEqual(jobqueue.state_of(self.tmp, "100"), "pending")
        self.assertEqual(len(self.raw()), 1)

    def test_invalid_json_exits_nonzero_and_writes_nothing(self):
        r = self.cli("enqueue.py", "{not json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["status"], "error")
        self.assertFalse((self.tmp / "queue").exists() and any((self.tmp / "queue" / "pending").glob("*")))
        self.assertFalse((self.tmp / "runs").exists())

    def test_missing_required_field(self):
        job = raw_job("100")
        del job["company"]
        with self.assertRaises(enqueue.EnqueueError):
            enqueue.enqueue(self.tmp, job, today=DATE)
        self.assertFalse((self.tmp / "runs").exists())

    def test_refused_when_stopping(self):
        jobqueue.begin_run(self.tmp, DATE)
        jobqueue.set_stopping(self.tmp)
        with self.assertRaises(enqueue.EnqueueError) as ctx:
            enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertIn("stopping", str(ctx.exception))

    def test_corrupt_jobs_json_is_an_error(self):
        (self.tmp / "jobs.json").write_text("[{corrupt")
        with self.assertRaises(enqueue.EnqueueError):
            enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertFalse((self.tmp / "runs").exists())

    def test_corrupt_raw_json_is_an_error(self):
        (self.tmp / "runs" / DATE).mkdir(parents=True)
        (self.tmp / "runs" / DATE / "raw.json").write_text("[{corrupt")
        with self.assertRaises(enqueue.EnqueueError):
            enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertIsNone(jobqueue.state_of(self.tmp, "100"))

    def test_raw_not_duplicated_on_retry_after_crash(self):
        (self.tmp / "runs" / DATE).mkdir(parents=True)
        (self.tmp / "runs" / DATE / "raw.json").write_text(json.dumps([raw_job("100")]))
        result = enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(self.raw()), 1)

    def test_run_date_comes_from_run_json_not_clock(self):
        jobqueue.begin_run(self.tmp, "2026-01-01")
        result = enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        self.assertEqual(result["date"], "2026-01-01")
        self.assertEqual(self.raw("2026-01-01")[0]["id"], "100")

    def test_stdin_job_with_quotes_newlines_unicode_and_100kb_description(self):
        nasty = 'He said "hi" \\ back\\slash\nline2\té中文 ' + ("x" * 100_000)
        r = self.cli("enqueue.py", "-", stdin=json.dumps(raw_job("100", description=nasty)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertEqual(json.loads(r.stdout)["status"], "queued")
        queued = json.loads((self.tmp / "queue" / "pending" / "100.json").read_text())
        self.assertEqual(queued["description"], nasty)
        self.assertEqual(self.raw(date=jobqueue.read_run(self.tmp)["date"])[0]["description"], nasty)

    def test_concurrent_same_id_queues_once(self):
        payload = json.dumps(raw_job("100"))
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.cli("enqueue.py", "-", stdin=payload), range(4)))
        statuses = sorted(json.loads(r.stdout)["status"] for r in results)
        self.assertEqual(statuses, ["duplicate", "duplicate", "duplicate", "queued"])
        self.assertEqual(len(self.raw(date=jobqueue.read_run(self.tmp)["date"])), 1)


class StopTests(EnqueueTestCase):
    def test_stop_marks_run_and_reports_pending(self):
        enqueue.enqueue(self.tmp, raw_job("100"), today=DATE)
        enqueue.enqueue(self.tmp, raw_job("101"), today=DATE)
        jobqueue.claim_next(self.tmp)
        self.assertEqual(stoppipeline.stop(self.tmp),
                         {"status": "stopping", "date": DATE, "pending": 1, "processing": 1})
        self.assertTrue(jobqueue.read_run(self.tmp)["stopping"])

    def test_stop_without_run_creates_one(self):
        result = stoppipeline.stop(self.tmp)
        self.assertEqual((result["status"], result["pending"]), ("stopping", 0))
        self.assertTrue(jobqueue.read_run(self.tmp)["stopping"])

    def test_stop_cli_prints_one_json_line(self):
        r = self.cli("stoppipeline.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "stopping")

    def test_stop_cli_fails_loudly_on_corrupt_run_json(self):
        (self.tmp / "queue").mkdir()
        (self.tmp / "queue" / "run.json").write_text("{nope")
        r = self.cli("stoppipeline.py")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["status"], "error")


if __name__ == "__main__":
    unittest.main()

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jobqueue as jq  # noqa: E402


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def put(self, i, **over):
        job = {"id": i, "posting_name": "Job " + i}
        job.update(over)
        jq.put(self.tmp, i, job)
        return job


class QueueTests(QueueTestCase):
    def test_put_and_claim_is_fifo(self):
        self.put("1")
        self.put("2")
        self.put("3")
        order = [jq.claim_next(self.tmp)[0] for _ in range(3)]
        self.assertEqual(order, ["1", "2", "3"])

    def test_claim_moves_to_processing_and_complete_deletes(self):
        self.put("1")
        job_id, job = jq.claim_next(self.tmp)
        self.assertEqual((job_id, job["posting_name"]), ("1", "Job 1"))
        self.assertEqual(jq.state_of(self.tmp, "1"), "processing")
        jq.complete(self.tmp, "1")
        self.assertIsNone(jq.state_of(self.tmp, "1"))

    def test_empty_claim_returns_none(self):
        self.assertIsNone(jq.claim_next(self.tmp))

    def test_recover_returns_processing_to_pending(self):
        self.put("1")
        jq.claim_next(self.tmp)
        self.assertEqual(jq.recover(self.tmp), 1)
        self.assertEqual(jq.state_of(self.tmp, "1"), "pending")

    def test_fail_then_requeue_failed_roundtrip(self):
        job = self.put("1", description="x \"quoted\" é")
        jq.claim_next(self.tmp)
        jq.fail(self.tmp, "1", "boom")
        self.assertEqual(jq.state_of(self.tmp, "1"), "failed")
        stored = json.loads((self.tmp / "queue" / "failed" / "1.json").read_text())
        self.assertEqual(stored["error"], "boom")
        self.assertTrue(jq.requeue_failed(self.tmp, "1"))
        self.assertEqual(jq.claim_next(self.tmp), ("1", job))
        self.assertFalse(jq.requeue_failed(self.tmp, "1"))

    def test_state_of_each_state(self):
        self.put("1")
        self.put("2")
        self.put("3")
        jq.claim_next(self.tmp)
        jq.claim_next(self.tmp)
        jq.fail(self.tmp, "2", "e")
        self.assertEqual([jq.state_of(self.tmp, i) for i in "123"], ["processing", "failed", "pending"])
        self.assertIsNone(jq.state_of(self.tmp, "9"))

    def test_counts(self):
        self.put("1")
        self.put("2")
        jq.claim_next(self.tmp)
        self.assertEqual(jq.counts(self.tmp), {"pending": 1, "processing": 1, "failed": 0})

    def test_put_leaves_no_partial_file(self):
        self.put("1")
        leftovers = [p.name for p in (self.tmp / "queue" / "pending").iterdir()]
        self.assertEqual(leftovers, ["1.json"])

    def test_corrupt_job_file_raises(self):
        self.put("1")
        (self.tmp / "queue" / "pending" / "1.json").write_text("{corrupt")
        with self.assertRaises(jq.QueueError):
            jq.claim_next(self.tmp)


class RunStateTests(QueueTestCase):
    def test_no_run_reads_none(self):
        self.assertIsNone(jq.read_run(self.tmp))

    def test_begin_run_is_idempotent_and_keeps_date(self):
        first = jq.begin_run(self.tmp, "2026-01-01")
        second = jq.begin_run(self.tmp, "2026-02-02")
        self.assertEqual(first, {"date": "2026-01-01", "stopping": False})
        self.assertEqual(second, first)

    def test_set_stopping_without_run_creates_one(self):
        run = jq.set_stopping(self.tmp)
        self.assertTrue(run["stopping"])
        self.assertEqual(jq.read_run(self.tmp), run)

    def test_set_stopping_keeps_date(self):
        jq.begin_run(self.tmp, "2026-01-01")
        self.assertEqual(jq.set_stopping(self.tmp), {"date": "2026-01-01", "stopping": True})

    def test_end_run_removes_file(self):
        jq.begin_run(self.tmp)
        jq.end_run(self.tmp)
        self.assertIsNone(jq.read_run(self.tmp))
        jq.end_run(self.tmp)  # no run: not an error

    def test_corrupt_run_json_raises(self):
        (self.tmp / "queue").mkdir()
        (self.tmp / "queue" / "run.json").write_text("{nope")
        with self.assertRaises(jq.QueueError):
            jq.read_run(self.tmp)


if __name__ == "__main__":
    unittest.main()

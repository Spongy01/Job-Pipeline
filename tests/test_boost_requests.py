import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import boost_requests as br  # noqa: E402

DATE = "2026-11-05"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".venv" / "bin").mkdir(parents=True)
        (self.tmp / ".venv" / "bin" / "python").write_text("")
        self.calls = []
        self.result = subprocess.CompletedProcess([], 0, "", "")

    def fake_run(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self.result

    def runner(self):
        return br.BoostRunner(self.tmp, run=self.fake_run)


class RequestTests(Base):
    def test_submit_creates_a_waiting_request(self):
        req = br.submit(self.tmp, "100", DATE)
        self.assertEqual((req["id"], req["state"], req["run_date"]), ("100", "waiting", DATE))
        self.assertEqual(br.read(self.tmp, "100")["state"], "waiting")
        self.assertIsNone(br.read(self.tmp, "999"))

    def test_submit_is_idempotent_while_waiting_or_running(self):
        first = br.submit(self.tmp, "100", DATE)
        br.set_state(self.tmp, "100", "running")
        again = br.submit(self.tmp, "100", DATE)
        self.assertEqual(again["state"], "running")
        self.assertEqual(again["requested_at"], first["requested_at"])

    def test_submit_after_a_failure_retries(self):
        br.submit(self.tmp, "100", DATE)
        br.set_state(self.tmp, "100", "failed", "boom")
        self.assertEqual(br.submit(self.tmp, "100", DATE)["state"], "waiting")
        self.assertNotIn("error", br.read(self.tmp, "100"))

    def test_next_waiting_is_oldest_first_and_skips_other_states(self):
        for i in ("1", "2", "3"):
            br.submit(self.tmp, i, DATE)
        br.set_state(self.tmp, "1", "failed", "x")
        self.assertEqual(br.next_waiting(self.tmp)["id"], "2")
        br.clear(self.tmp, "2")
        self.assertEqual(br.next_waiting(self.tmp)["id"], "3")
        br.clear(self.tmp, "3")
        self.assertIsNone(br.next_waiting(self.tmp))

    def test_reset_running_after_a_crash(self):
        br.submit(self.tmp, "1", DATE)
        br.set_state(self.tmp, "1", "running")
        self.assertEqual(br.reset_running(self.tmp), 1)
        self.assertEqual(br.read(self.tmp, "1")["state"], "waiting")

    def test_corrupt_request_file_raises(self):
        br.submit(self.tmp, "1", DATE)
        (self.tmp / "queue" / "boost_requests" / "1.json").write_text("{nope")
        with self.assertRaises(br.RequestError):
            br.read(self.tmp, "1")

    def test_bad_job_id_is_rejected(self):
        for bad in ("../x", "1/2", "", "abc"):
            with self.assertRaises(br.RequestError, msg=bad):
                br.submit(self.tmp, bad, DATE)


class RunnerTests(Base):
    def test_nothing_waiting_returns_none(self):
        self.assertIsNone(self.runner().step())
        self.assertEqual(self.calls, [])

    def test_success_clears_the_request_and_passes_date_and_job(self):
        br.submit(self.tmp, "100", DATE)
        self.assertEqual(self.runner().step(), "done")
        self.assertIsNone(br.read(self.tmp, "100"))
        cmd = self.calls[0]
        self.assertIn("boost.py", " ".join(map(str, cmd)))
        self.assertEqual(cmd[cmd.index("--job") + 1], "100")
        self.assertIn(DATE, cmd)

    def test_worker_busy_goes_back_to_waiting(self):
        br.submit(self.tmp, "100", DATE)
        self.result = subprocess.CompletedProcess([], 3, "", "another worker is running")
        self.assertEqual(self.runner().step(), "busy")
        self.assertEqual(br.read(self.tmp, "100")["state"], "waiting")

    def test_failure_records_the_last_error_line(self):
        br.submit(self.tmp, "100", DATE)
        self.result = subprocess.CompletedProcess([], 1, "", "progress...\nboost.py: model not downloaded\n")
        self.assertEqual(self.runner().step(), "failed")
        req = br.read(self.tmp, "100")
        self.assertEqual((req["state"], req["error"]), ("failed", "boost.py: model not downloaded"))

    def test_running_state_is_set_while_the_command_runs(self):
        br.submit(self.tmp, "100", DATE)
        seen = []
        runner = br.BoostRunner(self.tmp, run=lambda cmd, **kw: (seen.append(br.read(self.tmp, "100")["state"]), self.result)[1])
        runner.step()
        self.assertEqual(seen, ["running"])

    def test_missing_venv_python_fails_the_request_with_a_clear_message(self):
        (self.tmp / ".venv" / "bin" / "python").unlink()
        br.submit(self.tmp, "100", DATE)
        self.assertEqual(self.runner().step(), "failed")
        self.assertIn(".venv", br.read(self.tmp, "100")["error"])


if __name__ == "__main__":
    unittest.main()

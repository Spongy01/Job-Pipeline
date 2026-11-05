import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jobslock  # noqa: E402


class JobsLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def test_lock_excludes_second_process(self):
        code = ("import sys, time; sys.path.insert(0, %r); import jobslock\n"
                "with jobslock.jobs_lock(%r):\n    print('held', flush=True); time.sleep(0.5)\n") % (str(SCRIPTS), str(self.tmp))
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        start = time.monotonic()
        with jobslock.jobs_lock(self.tmp):
            waited = time.monotonic() - start
        proc.wait()
        self.assertGreaterEqual(waited, 0.3)

    def test_lock_released_after_exception(self):
        with self.assertRaises(ValueError):
            with jobslock.jobs_lock(self.tmp):
                raise ValueError("boom")
        start = time.monotonic()
        with jobslock.jobs_lock(self.tmp):
            pass
        self.assertLess(time.monotonic() - start, 0.2)

    def test_lock_file_created_in_root(self):
        with jobslock.jobs_lock(self.tmp):
            self.assertTrue((self.tmp / ".jobs.lock").exists())


if __name__ == "__main__":
    unittest.main()

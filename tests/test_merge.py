import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jobslock  # noqa: E402
import merge  # noqa: E402

DATE = "2026-11-04"
M9, MG = "qwen3.5:9b", "gemma4:e4b"


def enriched_job(i, models=(M9, MG), **over):
    job = {"id": i, "posting_name": "Engineer " + i, "company": "Co" + i, "description": "JD " + i,
           "enrichment": {m: {"ok": True, "data": {"score": 80}} for m in models},
           "jd_flags": {"sponsorship": None, "clearance": False}}
    job.update(over)
    return job


def filtered_rec(i, stage="passed", reasons=()):
    return {"id": i, "stage": stage, "reasons": list(reasons), "years_required": None, "years_model": M9}


class MergeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = self.tmp / "runs" / DATE
        (self.run_dir / "boost").mkdir(parents=True)

    def write_run(self, jobs, filtered=None, boosts=None, rollup=None):
        (self.run_dir / "enriched.json").write_text(json.dumps(jobs))
        (self.run_dir / "filtered.json").write_text(json.dumps(
            filtered if filtered is not None else [filtered_rec(j["id"]) for j in jobs]))
        for i, rec in (boosts or {}).items():
            (self.run_dir / "boost" / (i + ".json")).write_text(json.dumps(rec))
        if rollup is not None:
            (self.run_dir / "boost_rollup.json").write_text(json.dumps(rollup))

    def jobs(self):
        return json.loads((self.tmp / "jobs.json").read_text())

    def by_id(self, i):
        return next(j for j in self.jobs() if j["id"] == i)

    def go(self, date=DATE):
        return merge.merge_run(self.tmp, date)


class MergeOneTests(MergeTestCase):
    def one(self, job, rec="auto"):
        rec = filtered_rec(job["id"]) if rec == "auto" else rec
        return merge.merge_one(self.tmp, DATE, job, rec)

    def test_merge_one_adds_then_unchanged(self):
        self.assertEqual(self.one(enriched_job("1")), "added")
        self.assertEqual(self.one(enriched_job("1")), "unchanged")
        record = self.by_id("1")
        self.assertEqual((record["stage"], record["run_date"], record["user_status"]), ("passed", DATE, None))
        self.assertEqual(len(self.jobs()), 1)

    def test_merge_one_fills_missing_model_only(self):
        self.one(enriched_job("1", models=(M9,)))
        self.assertEqual(self.one(enriched_job("1", models=(M9, MG))), "filled")
        self.assertEqual(sorted(self.by_id("1")["enrichment"]), sorted([M9, MG]))

    def test_merge_one_keeps_user_status_and_run_date(self):
        self.one(enriched_job("1"))
        jobs = self.jobs()
        jobs[0]["user_status"] = "applied"
        (self.tmp / "jobs.json").write_text(json.dumps(jobs))
        merge.merge_one(self.tmp, "2030-01-01", enriched_job("1", models=(M9, MG)), filtered_rec("1", stage="rejected"))
        self.assertEqual((self.by_id("1")["user_status"], self.by_id("1")["run_date"]), ("applied", DATE))

    def test_merge_one_corrupt_jobs_json_raises_and_writes_nothing(self):
        (self.tmp / "jobs.json").write_text("[{corrupt")
        with self.assertRaises(merge.MergeError):
            self.one(enriched_job("1"))
        self.assertEqual((self.tmp / "jobs.json").read_text(), "[{corrupt")
        self.assertFalse((self.tmp / "jobs.json.bak").exists())

    def test_merge_one_writes_bak_before_change(self):
        self.one(enriched_job("1"))
        before = (self.tmp / "jobs.json").read_text()
        self.one(enriched_job("2"))
        self.assertEqual((self.tmp / "jobs.json.bak").read_text(), before)

    def test_merge_one_without_filter_record_is_enriched_stage(self):
        self.one(enriched_job("1"), rec=None)
        self.assertEqual(self.by_id("1")["stage"], "enriched")


class MergeTests(MergeTestCase):
    def test_merge_waits_for_lock(self):
        import threading
        import time
        self.write_run([enriched_job("1")])
        done = []
        with jobslock.jobs_lock(self.tmp):
            t = threading.Thread(target=lambda: (self.go(), done.append(time.monotonic())))
            t.start()
            time.sleep(0.3)
            self.assertEqual(done, [])
            released = time.monotonic()
        t.join(5)
        self.assertEqual(len(done), 1)
        self.assertGreaterEqual(done[0], released)

    def test_new_run_adds_all_jobs_with_sections(self):
        self.write_run([enriched_job("1"), enriched_job("2")],
                       boosts={"1": {"ok": True, "plan": {"verdict": "v"}}})
        summary = self.go()
        self.assertEqual((summary["jobs_in_run"], summary["added"], summary["filled"], summary["unchanged"]), (2, 2, 0, 0))
        j1 = self.by_id("1")
        self.assertEqual((j1["stage"], j1["run_date"], j1["user_status"]), ("passed", DATE, None))
        self.assertEqual(j1["boost"]["plan"]["verdict"], "v")
        self.assertEqual(j1["filter"]["years_model"], M9)
        self.assertEqual(sorted(j1["enrichment"]), [MG, M9])
        self.assertIsNone(self.by_id("2")["boost"])

    def test_rejected_jobs_included_with_reasons(self):
        self.write_run([enriched_job("1")], filtered=[filtered_rec("1", "rejected", [{"rule": "exclude_clearance"}])])
        self.go()
        j = self.by_id("1")
        self.assertEqual((j["stage"], j["filter"]["reasons"][0]["rule"]), ("rejected", "exclude_clearance"))

    def test_job_missing_from_filtered_is_enriched_stage(self):
        self.write_run([enriched_job("1")], filtered=[])
        self.go()
        j = self.by_id("1")
        self.assertEqual((j["stage"], j["filter"]), ("enriched", None))

    def test_second_merge_changes_nothing(self):
        self.write_run([enriched_job("1")], rollup={"ok": True, "tally": []})
        self.go()
        before = (self.tmp / "jobs.json").read_bytes(), (self.tmp / "rollups.json").read_bytes()
        summary = self.go()
        self.assertEqual((summary["added"], summary["filled"], summary["unchanged"], summary["rollup"]), (0, 0, 1, "kept"))
        self.assertEqual(((self.tmp / "jobs.json").read_bytes(), (self.tmp / "rollups.json").read_bytes()), before)
        self.assertFalse((self.tmp / "jobs.json.bak").exists())  # nothing was rewritten

    def test_boost_arriving_later_fills_only_boost(self):
        self.write_run([enriched_job("1")])
        self.go()
        data = self.jobs()
        data[0]["user_status"] = "applied"
        data[0]["description"] = "edited by hand"
        (self.tmp / "jobs.json").write_text(json.dumps(data))
        self.write_run([enriched_job("1", description="different")], boosts={"1": {"ok": True, "plan": {"verdict": "late"}}})
        summary = self.go()
        self.assertEqual((summary["added"], summary["filled"]), (0, 1))
        j = self.by_id("1")
        self.assertEqual(j["boost"]["plan"]["verdict"], "late")
        self.assertEqual((j["user_status"], j["description"]), ("applied", "edited by hand"))
        self.assertTrue((self.tmp / "jobs.json.bak").exists())

    def test_existing_enrichment_model_not_replaced_new_model_added(self):
        self.write_run([enriched_job("1", models=(M9,))])
        self.go()
        newer = enriched_job("1", models=(M9, MG))
        newer["enrichment"][M9]["data"]["score"] = 1
        self.write_run([newer])
        self.go()
        j = self.by_id("1")
        self.assertEqual(j["enrichment"][M9]["data"]["score"], 80)
        self.assertIn(MG, j["enrichment"])

    def test_run_date_stays_the_first_one(self):
        self.write_run([enriched_job("1")])
        self.go()
        other = self.tmp / "runs" / "2026-11-05"
        shutil.copytree(self.run_dir, other)
        self.go("2026-11-05")
        self.assertEqual(self.by_id("1")["run_date"], DATE)

    def test_bak_written_only_when_jobs_json_exists(self):
        self.write_run([enriched_job("1")])
        self.go()
        self.assertFalse((self.tmp / "jobs.json.bak").exists())
        self.write_run([enriched_job("1"), enriched_job("2")])
        self.go()
        self.assertEqual(len(json.loads((self.tmp / "jobs.json.bak").read_text())), 1)

    def test_rollup_added_once_and_kept(self):
        self.write_run([enriched_job("1")], rollup={"ok": True, "summary": "first"})
        self.assertEqual(self.go()["rollup"], "added")
        self.write_run([enriched_job("1")], rollup={"ok": True, "summary": "second"})
        self.assertEqual(self.go()["rollup"], "kept")
        self.assertEqual(json.loads((self.tmp / "rollups.json").read_text())[DATE]["summary"], "first")

    def test_failed_boost_and_failed_rollup_are_ignored(self):
        self.write_run([enriched_job("1")], boosts={"1": {"ok": False, "error": "x"}}, rollup={"ok": False, "error": "x"})
        summary = self.go()
        self.assertIsNone(self.by_id("1")["boost"])
        self.assertEqual(summary["rollup"], "none")
        self.assertFalse((self.tmp / "rollups.json").exists())

    def test_spec_text_is_embedded_when_the_file_exists(self):
        specs = self.run_dir / "boost" / "specs"
        specs.mkdir()
        (specs / "1-np.md").write_text("# Spec\nbody\n")
        (specs / "rollup-x.md").write_text("# Rollup spec\n")
        self.write_run([enriched_job("1"), enriched_job("2")],
                       boosts={"1": {"ok": True, "spec_file": "boost/specs/1-np.md"},
                               "2": {"ok": True, "spec_file": "boost/specs/missing.md"}},
                       rollup={"ok": True, "spec_file": "boost/specs/rollup-x.md"})
        self.go()
        self.assertEqual(self.by_id("1")["boost"]["spec_text"], "# Spec\nbody\n")
        self.assertNotIn("spec_text", self.by_id("2")["boost"])
        self.assertEqual(json.loads((self.tmp / "rollups.json").read_text())[DATE]["spec_text"], "# Rollup spec\n")

    def test_no_rollup_file(self):
        self.write_run([enriched_job("1")])
        self.assertEqual(self.go()["rollup"], "none")


class FillMissingTests(unittest.TestCase):
    def test_never_touches_user_status_or_run_date(self):
        existing = {"id": "1", "user_status": "skipped", "run_date": "a", "boost": None}
        changed = merge.fill_missing(existing, {"id": "1", "user_status": None, "run_date": "b", "boost": {"x": 1}})
        self.assertTrue(changed)
        self.assertEqual((existing["user_status"], existing["run_date"], existing["boost"]), ("skipped", "a", {"x": 1}))

    def test_no_change_reports_false(self):
        existing = {"id": "1", "a": 1, "boost": None}
        self.assertFalse(merge.fill_missing(existing, {"id": "1", "a": 2, "boost": None}))


class InputErrorTests(MergeTestCase):
    def snapshot(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.is_file())

    def test_corrupt_or_missing_inputs_write_nothing(self):
        self.write_run([enriched_job("1")])
        (self.tmp / "jobs.json").write_text("[{corrupt")
        before = self.snapshot()
        with self.assertRaises(merge.MergeError):
            self.go()
        (self.tmp / "jobs.json").write_text("[]")
        (self.run_dir / "filtered.json").write_text("{bad")
        with self.assertRaises(merge.MergeError):
            self.go()
        (self.run_dir / "filtered.json").unlink()
        with self.assertRaises(merge.MergeError):
            self.go()
        (self.run_dir / "enriched.json").unlink()
        with self.assertRaises(merge.MergeError):
            self.go()
        self.assertEqual((self.tmp / "jobs.json").read_text(), "[]")
        self.assertFalse((self.tmp / "rollups.json").exists())
        self.assertFalse((self.tmp / "jobs.json.bak").exists())
        self.assertEqual(before.count("jobs.json.bak"), 0)

    def test_corrupt_boost_record_writes_nothing(self):
        self.write_run([enriched_job("1")])
        (self.run_dir / "boost" / "1.json").write_text("{bad")
        with self.assertRaises(merge.MergeError):
            self.go()
        self.assertFalse((self.tmp / "jobs.json").exists())

    def test_cli_prints_one_json_line_and_fails_loudly(self):
        (self.tmp / "scripts").mkdir()
        shutil.copy(SCRIPTS / "merge.py", self.tmp / "scripts" / "merge.py")
        shutil.copy(SCRIPTS / "clean.py", self.tmp / "scripts" / "clean.py")
        shutil.copy(SCRIPTS / "jobslock.py", self.tmp / "scripts" / "jobslock.py")
        self.write_run([enriched_job("1")])
        r = subprocess.run([sys.executable, str(self.tmp / "scripts" / "merge.py"), DATE], capture_output=True, text=True, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertEqual(json.loads(r.stdout)["added"], 1)
        r = subprocess.run([sys.executable, str(self.tmp / "scripts" / "merge.py"), "2020-01-01"], capture_output=True, text=True, cwd="/")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("enriched.json", r.stderr)
        r = subprocess.run([sys.executable, str(self.tmp / "scripts" / "merge.py"), "11-04-2026"], capture_output=True, text=True, cwd="/")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()

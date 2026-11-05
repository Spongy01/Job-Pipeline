import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import filter as jobfilter  # noqa: E402

DATE = "2026-11-04"
RULES = {"experience_years": {"min": 0, "max": 3}, "exclude_us_citizenship_required": True,
         "exclude_clearance": True, "exclude_sponsorship_denied": True}


def entry(items, ok=True):
    if not ok:
        return {"ok": False, "error": "x"}
    return {"ok": True, "data": {"experience": [
        {"area": a, "level": lv, "years_min": lo, "years_max": hi, "evidence": "e"} for a, lv, lo, hi in items]}}


def make_job(i="1", years=(("overall", "required", 1, 1),), model="qwen3.5:9b", flags=None, **over):
    job = {"id": i, "company": "Acme", "posting_name": "Engineer", "closed": False,
           "enrichment": {model: entry(years)},
           "jd_flags": {"sponsorship": None, "sponsorship_evidence": "", "clearance": False, "clearance_evidence": "",
                        "us_citizenship_required": False, "us_citizenship_evidence": ""}}
    if flags:
        job["jd_flags"].update(flags)
    job.update(over)
    return job


def decide(job, rules=RULES):
    return jobfilter.evaluate(job, rules)


class ExperienceTests(unittest.TestCase):
    def test_within_limit_passes(self):
        for years in ((("overall", "required", 0, 0),), (("overall", "required", 3, 3),), (("overall", "required", 2, 4),)):
            self.assertEqual(decide(make_job(years=years))["stage"], "passed", years)

    def test_above_limit_rejected_by_years_min(self):
        r = decide(make_job(years=(("overall", "required", 4, 6),)))
        self.assertEqual(r["stage"], "rejected")
        self.assertEqual(r["reasons"][0]["rule"], "experience_years")
        self.assertEqual(r["years_required"], {"min": 4, "max": 6})

    def test_range_starting_at_limit_passes(self):
        self.assertEqual(decide(make_job(years=(("overall", "required", 3, 5),)))["stage"], "passed")

    def test_no_years_stated_passes(self):
        r = decide(make_job(years=()))
        self.assertEqual(r["stage"], "passed")
        self.assertIsNone(r["years_required"])

    def test_preferred_and_skill_specific_years_ignored(self):
        years = (("overall", "preferred", 8, 8), ("Python", "required", 5, 5))
        self.assertEqual(decide(make_job(years=years))["stage"], "passed")

    def test_highest_required_overall_item_counts(self):
        years = (("overall", "required", 1, 1), ("Overall", "required", 5, 5))
        self.assertEqual(decide(make_job(years=years))["stage"], "rejected")

    def test_min_rule(self):
        rules = dict(RULES, experience_years={"min": 2, "max": 5})
        self.assertEqual(decide(make_job(years=(("overall", "required", 1, 1),)), rules)["stage"], "rejected")
        self.assertEqual(decide(make_job(years=(("overall", "required", 1, 2),)), rules)["stage"], "passed")

    def test_no_experience_rule_means_no_check(self):
        rules = {k: v for k, v in RULES.items() if k != "experience_years"}
        self.assertEqual(decide(make_job(years=(("overall", "required", 9, 9),)), rules)["stage"], "passed")

    def test_preferred_model_used_then_fallback(self):
        job = make_job(years=(("overall", "required", 5, 5),), model="gemma4:e4b")
        job["enrichment"]["qwen3.5:9b"] = entry([("overall", "required", 1, 1)])
        r = decide(job)
        self.assertEqual((r["stage"], r["years_model"]), ("passed", "qwen3.5:9b"))
        job["enrichment"]["qwen3.5:9b"] = entry([], ok=False)  # 9b failed: fall back to gemma
        r = decide(job)
        self.assertEqual((r["stage"], r["years_model"]), ("rejected", "gemma4:e4b"))

    def test_no_usable_model_passes(self):
        job = make_job(years=())
        job["enrichment"] = {"qwen3.5:9b": entry([], ok=False)}
        r = decide(job)
        self.assertEqual((r["stage"], r["years_model"]), ("passed", None))


class FlagRuleTests(unittest.TestCase):
    def test_each_flag_rejects_with_evidence(self):
        cases = [
            ({"us_citizenship_required": True, "us_citizenship_evidence": "US citizenship is required."},
             "exclude_us_citizenship_required"),
            ({"clearance": True, "clearance_evidence": "Active Secret clearance."}, "exclude_clearance"),
            ({"sponsorship": False, "sponsorship_evidence": "We do not sponsor."}, "exclude_sponsorship_denied"),
        ]
        for flags, rule in cases:
            r = decide(make_job(flags=flags))
            self.assertEqual(r["stage"], "rejected", rule)
            self.assertEqual([x["rule"] for x in r["reasons"]], [rule])
            self.assertTrue(r["reasons"][0]["evidence"])

    def test_sponsorship_true_or_unknown_passes(self):
        self.assertEqual(decide(make_job(flags={"sponsorship": True}))["stage"], "passed")
        self.assertEqual(decide(make_job(flags={"sponsorship": None}))["stage"], "passed")

    def test_rules_switched_off(self):
        rules = dict(RULES, exclude_clearance=False, exclude_us_citizenship_required=False, exclude_sponsorship_denied=False)
        job = make_job(flags={"clearance": True, "us_citizenship_required": True, "sponsorship": False})
        self.assertEqual(decide(job, rules)["stage"], "passed")

    def test_several_reasons_all_listed(self):
        job = make_job(years=(("overall", "required", 6, 6),), flags={"clearance": True, "sponsorship": False})
        self.assertEqual([x["rule"] for x in decide(job)["reasons"]],
                         ["experience_years", "exclude_clearance", "exclude_sponsorship_denied"])

    def test_missing_flags_pass(self):
        job = make_job()
        del job["jd_flags"]
        self.assertEqual(decide(job)["stage"], "passed")

    def test_closed_job(self):
        r = decide(make_job(closed=True, flags={"clearance": True}))
        self.assertEqual((r["stage"], r["reasons"]), ("closed", []))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "scripts").mkdir()
        shutil.copy(SCRIPTS / "filter.py", self.tmp / "scripts" / "filter.py")
        shutil.copy(SCRIPTS / "clean.py", self.tmp / "scripts" / "clean.py")
        self.run_dir = self.tmp / "runs" / DATE
        self.run_dir.mkdir(parents=True)
        rules = dict(RULES, work_models=["remote"], locations=[])
        (self.tmp / "config.json").write_text(json.dumps({"rules": rules}))
        jobs = [make_job("1"), make_job("2", years=(("overall", "required", 7, 7),)),
                make_job("3", flags={"clearance": True}), make_job("4", closed=True)]
        (self.run_dir / "enriched.json").write_text(json.dumps(jobs))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.tmp / "scripts" / "filter.py"), *args],
                              capture_output=True, text=True, cwd="/")

    def test_success(self):
        before = {p.name: p.read_bytes() for p in (self.tmp / "config.json", self.run_dir / "enriched.json")}
        r = self.run_cli(DATE)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        out = json.loads(lines[0])
        self.assertEqual((out["input"], out["passed"], out["rejected"], out["closed"]), (4, 1, 2, 1))
        self.assertEqual(out["rejected_by_rule"], {"experience_years": 1, "exclude_clearance": 1})
        filtered = json.loads((self.run_dir / "filtered.json").read_text())
        self.assertEqual([f["stage"] for f in filtered], ["passed", "rejected", "rejected", "closed"])
        report = json.loads((self.run_dir / "filter_report.json").read_text())
        self.assertEqual(report["rules_set_but_not_applied"], {"work_models": ["remote"]})
        self.assertEqual({p.name: p.read_bytes() for p in (self.tmp / "config.json", self.run_dir / "enriched.json")}, before)

    def test_missing_input_writes_nothing(self):
        (self.run_dir / "enriched.json").unlink()
        r = self.run_cli(DATE)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_corrupt_input_and_config(self):
        (self.run_dir / "enriched.json").write_text("[{bad")
        self.assertNotEqual(self.run_cli(DATE).returncode, 0)
        (self.run_dir / "enriched.json").write_text("[]")
        (self.tmp / "config.json").write_text("{nope")
        self.assertNotEqual(self.run_cli(DATE).returncode, 0)
        self.assertFalse((self.run_dir / "filtered.json").exists())

    def test_config_without_rules(self):
        (self.tmp / "config.json").write_text("{}")
        r = self.run_cli(DATE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rules", r.stderr)

    def test_bad_date(self):
        self.assertNotEqual(self.run_cli("11-04-2026").returncode, 0)


if __name__ == "__main__":
    unittest.main()

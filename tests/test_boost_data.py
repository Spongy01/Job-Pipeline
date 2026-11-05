import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import boost_data as bd  # noqa: E402

M9, MG = "qwen3.5:9b", "gemma4:e4b"


def skill(name, in_resume="no", learnable=None):
    return {"skill": name, "in_resume": in_resume, "resume_evidence": "", "learnable_in_a_day": learnable,
            "learn_note": ""}


def result(score=80, required=(), preferred=(), req_points=20, ok=True):
    if not ok:
        return {"ok": False, "error": "x"}
    return {"ok": True, "data": {
        "score": score, "required_skills": list(required), "preferred_skills": list(preferred),
        "score_breakdown": {"required_skills": req_points}}}


def job(i, desc=None, enrichment=None, **over):
    j = {"id": i, "company": "Acme " + i, "posting_name": "Engineer " + i,
         "description": desc if desc is not None else "unique description " + i,
         "enrichment": enrichment if enrichment is not None else {M9: result(), MG: result()}}
    j.update(over)
    return j


def filtered(*passed, rejected=()):
    return ([{"id": i, "stage": "passed"} for i in passed] +
            [{"id": i, "stage": "rejected"} for i in rejected])


class PassedJobsTests(unittest.TestCase):
    def test_only_passed_in_enriched_order(self):
        jobs = [job("1"), job("2"), job("3")]
        out = bd.passed_jobs(jobs, filtered("3", "1", rejected=("2",)))
        self.assertEqual([j["id"] for j in out], ["1", "3"])

    def test_dedupes_identical_descriptions_keeping_first(self):
        jobs = [job("1", desc="Same  text\n here"), job("2", desc="same text here"), job("3")]
        out = bd.passed_jobs(jobs, filtered("1", "2", "3"))
        self.assertEqual([j["id"] for j in out], ["1", "3"])

    def test_jobs_missing_from_filtered_are_not_passed(self):
        self.assertEqual(bd.passed_jobs([job("1")], []), [])

    def test_description_hash_normalizes(self):
        self.assertEqual(bd.description_hash(" A  b\nC "), bd.description_hash("a b c"))


class SelectTopTests(unittest.TestCase):
    def test_orders_by_9b_score_with_tiebreaks(self):
        jobs = [job("1", enrichment={M9: result(80, req_points=10)}),
                job("2", enrichment={M9: result(90)}),
                job("3", enrichment={M9: result(80, req_points=30)}),
                job("4", enrichment={M9: result(80, req_points=30)})]
        out = bd.select_top(jobs, M9, 5)
        self.assertEqual([s["job"]["id"] for s in out], ["2", "3", "4", "1"])
        self.assertEqual([s["rank"] for s in out], [1, 2, 3, 4])
        self.assertEqual(out[0]["score"], 90)
        self.assertEqual(out[0]["score_model"], M9)
        self.assertFalse(out[0]["fallback"])

    def test_top_n_limits(self):
        jobs = [job(str(i), enrichment={M9: result(50 + i)}) for i in range(8)]
        out = bd.select_top(jobs, M9, 5)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["job"]["id"], "7")

    def test_falls_back_to_other_model_and_marks_it(self):
        j = job("1", enrichment={M9: result(ok=False), MG: result(77)})
        out = bd.select_top([j], M9, 5)
        self.assertEqual((out[0]["score"], out[0]["score_model"], out[0]["fallback"]), (77, MG, True))

    def test_skips_jobs_with_no_ok_result(self):
        j = job("1", enrichment={M9: result(ok=False)})
        self.assertEqual(bd.select_top([j, job("2")], M9, 5)[0]["job"]["id"], "2")
        self.assertEqual(len(bd.select_top([j, job("2")], M9, 5)), 1)

    def test_fewer_than_top_n_and_empty(self):
        self.assertEqual(len(bd.select_top([job("1")], M9, 5)), 1)
        self.assertEqual(bd.select_top([], M9, 5), [])


class GapTests(unittest.TestCase):
    def test_skill_key_normalizes(self):
        self.assertEqual(bd.skill_key("AWS (any one)"), bd.skill_key(" aws "))
        self.assertEqual(bd.skill_key("CI/CD"), bd.skill_key("ci / cd"))
        self.assertNotEqual(bd.skill_key("Java"), bd.skill_key("JavaScript"))

    def test_consolidate_gaps_merges_models_and_flags_single_model(self):
        j = job("1", enrichment={
            M9: result(required=[skill("Kubernetes", "no", True), skill("Python", "yes"),
                                 skill("AWS", "similar", True)], preferred=[skill("Terraform", "no", False)]),
            MG: result(required=[skill("kubernetes", "similar", False), skill("Python", "yes"),
                                 skill("AWS", "yes")])})
        gaps = {bd.skill_key(g["skill"]): g for g in bd.consolidate_gaps(j)}
        self.assertEqual(sorted(gaps), ["aws", "kubernetes", "terraform"])
        k = gaps["kubernetes"]
        self.assertEqual((k["kind"], k["status"], k["flagged_by"], k["models_total"], k["learnable_votes"], k["single_model"]),
                         ("required", "no", [MG, M9], 2, 1, False))
        self.assertEqual(k["skill"], "Kubernetes")  # first model's wording
        a = gaps["aws"]
        self.assertTrue(a["single_model"])
        self.assertEqual((a["status"], a["flagged_by"]), ("similar", [M9]))
        self.assertEqual(gaps["terraform"]["kind"], "preferred")
        self.assertTrue(gaps["terraform"]["single_model"])

    def test_consolidate_ignores_failed_models_and_single_model_jobs(self):
        j = job("1", enrichment={M9: result(required=[skill("Go", "no")]), MG: result(ok=False)})
        gaps = bd.consolidate_gaps(j)
        self.assertEqual((len(gaps), gaps[0]["models_total"], gaps[0]["single_model"]), (1, 1, False))


class TallyTests(unittest.TestCase):
    def test_tally_counts_jobs_per_skill_case_insensitively(self):
        jobs = [job("1", enrichment={M9: result(required=[skill("AWS", "no", True)])}),
                job("2", enrichment={M9: result(required=[skill("aws", "similar", False)])}),
                job("3", enrichment={M9: result(preferred=[skill("Aws (any one)", "no", True)])}),
                job("4", enrichment={M9: result(required=[skill("Go", "no", False)])})]
        rows = bd.tally_gaps(jobs)
        aws = rows[0]
        self.assertEqual((aws["jobs"], aws["job_ids"], aws["required_count"], aws["preferred_count"]),
                         (3, ["1", "2", "3"], 2, 1))
        self.assertEqual(aws["skill"], "AWS")  # most common display name, ties to the first seen
        self.assertAlmostEqual(aws["learnable_share"], 2 / 3)
        self.assertEqual(aws["models_flagged_max"], 1)

    def test_tally_sort_order(self):
        jobs = [job("1", enrichment={M9: result(required=[skill("Zed", "no"), skill("Alpha", "no")])}),
                job("2", enrichment={M9: result(required=[skill("Zed", "no")])})]
        self.assertEqual([r["skill"] for r in bd.tally_gaps(jobs)], ["Zed", "Alpha"])
        jobs = [job("1", enrichment={M9: result(required=[skill("Zed", "no"), skill("Alpha", "no")])})]
        self.assertEqual([r["skill"] for r in bd.tally_gaps(jobs)], ["Alpha", "Zed"])

    def test_tally_empty(self):
        self.assertEqual(bd.tally_gaps([]), [])
        self.assertEqual(bd.tally_gaps([job("1", enrichment={M9: result(required=[skill("Go", "yes")])})]), [])


class ProjectsAndChecksTests(unittest.TestCase):
    def test_parse_projects_from_projects_line(self):
        md = "# Projects\n\nProjects: Conduit | BrokeNoMore |  Cardiology-QA\n\n## 1. Conduit (resume project)\n"
        self.assertEqual(bd.parse_projects(md), ["Conduit", "BrokeNoMore", "Cardiology-QA"])

    def test_parse_projects_falls_back_to_headings(self):
        md = "## 1. Conduit (resume project)\n## 2. BrokeNoMore (resume project)\n## Other repos (shorter)\n"
        self.assertEqual(bd.parse_projects(md), ["Conduit", "BrokeNoMore"])

    RESUME = "Built a rate limiter with  Redis Lua scripts.\nInstrumented Prometheus metrics."

    def plan(self):
        return {"verdict": "v",
                "rewordings": [{"current_text": "Built a rate limiter with Redis Lua scripts.", "suggested_text": "s"},
                               {"current_text": "Invented line that is not there", "suggested_text": "s"}],
                "project_extensions": [{"project": "conduit", "what_to_build": "x"},
                                       {"project": "Imaginary", "what_to_build": "y"}]}

    def test_check_plan_flags_unknown_project_and_missing_quote(self):
        original = self.plan()
        checked, warnings = bd.check_plan(original, self.RESUME, ["Conduit", "BrokeNoMore"])
        self.assertEqual([r["current_text_found"] for r in checked["rewordings"]], [True, False])
        self.assertEqual([e["project_known"] for e in checked["project_extensions"]], [True, False])
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("Invented line" in w for w in warnings))
        self.assertTrue(any("Imaginary" in w for w in warnings))
        self.assertNotIn("current_text_found", original["rewordings"][0])  # input untouched

    def test_check_plan_does_not_drop_flagged_items(self):
        checked, _ = bd.check_plan(self.plan(), self.RESUME, ["Conduit"])
        self.assertEqual(len(checked["rewordings"]), 2)
        self.assertEqual(len(checked["project_extensions"]), 2)

    def test_check_plan_accepts_quote_with_different_whitespace_and_case(self):
        plan = {"rewordings": [{"current_text": "BUILT A RATE LIMITER WITH REDIS\nLUA SCRIPTS."}],
                "project_extensions": []}
        checked, warnings = bd.check_plan(plan, self.RESUME, [])
        self.assertTrue(checked["rewordings"][0]["current_text_found"])
        self.assertEqual(warnings, [])

    def test_check_plan_empty_quote_is_not_found(self):
        checked, warnings = bd.check_plan({"rewordings": [{"current_text": ""}], "project_extensions": []}, self.RESUME, [])
        self.assertFalse(checked["rewordings"][0]["current_text_found"])
        self.assertEqual(len(warnings), 1)

    def test_strip_em_dashes_nested(self):
        value = {"a": "x \u2014 y", "b": ["p\u2014q", {"c": "no dash"}], "n": 3, "none": None}
        out = bd.strip_em_dashes(value)
        self.assertEqual(out, {"a": "x  -  y", "b": ["p - q", {"c": "no dash"}], "n": 3, "none": None})
        self.assertNotIn("\u2014", str(out))


class FormatTests(unittest.TestCase):
    def test_format_gaps(self):
        j = job("1", enrichment={
            M9: result(required=[skill("Kubernetes", "no", True)], preferred=[skill("Terraform", "similar", False)]),
            MG: result(required=[skill("Kubernetes", "no", False)])})
        text = bd.format_gaps(bd.consolidate_gaps(j))
        self.assertIn("- Kubernetes | required | missing | flagged by 2 of 2 models | learnable in a day: 1 of 2", text)
        self.assertIn("- Terraform | preferred | only a similar skill is on the resume | flagged by 1 of 2 models "
                      "(flagged by one model, check it against the resume)", text)

    def test_format_gaps_empty(self):
        self.assertIn("none", bd.format_gaps([]).lower())

    def test_format_tally_limits_rows(self):
        rows = [{"skill": "S%d" % i, "jobs": 30 - i, "job_ids": [], "models_flagged_max": 2, "learnable_share": 0.5,
                 "required_count": 1, "preferred_count": 0} for i in range(30)]
        text = bd.format_tally(rows, limit=5)
        self.assertEqual(len(text.splitlines()), 5)
        self.assertIn("- S0: 30 jobs (1 required, 0 preferred)", text)
        self.assertIn("50%", text)


if __name__ == "__main__":
    unittest.main()

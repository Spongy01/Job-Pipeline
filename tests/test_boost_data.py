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


if __name__ == "__main__":
    unittest.main()

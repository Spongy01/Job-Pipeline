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
                                 skill("Docker", "similar", True)], preferred=[skill("Terraform", "no", False)]),
            MG: result(required=[skill("kubernetes", "similar", False), skill("Python", "yes"),
                                 skill("Docker", "yes")])})
        gaps = {bd.skill_key(g["skill"]): g for g in bd.consolidate_gaps(j)}
        self.assertEqual(sorted(gaps), ["docker", "kubernetes", "terraform"])
        k = gaps["kubernetes"]
        self.assertEqual((k["kind"], k["status"], k["flagged_by"], k["models_total"], k["learnable_votes"], k["single_model"]),
                         ("required", "no", [MG, M9], 2, 1, False))
        self.assertEqual(k["skill"], "Kubernetes")  # first model's wording
        a = gaps["docker"]
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
        jobs = [job("1", enrichment={M9: result(required=[skill("Redis", "no", True)])}),
                job("2", enrichment={M9: result(required=[skill("redis", "similar", False)])}),
                job("3", enrichment={M9: result(preferred=[skill("Redis (any one)", "no", True)])}),
                job("4", enrichment={M9: result(required=[skill("Go", "no", False)])})]
        rows = bd.tally_gaps(jobs)
        redis = rows[0]
        self.assertEqual((redis["jobs"], redis["job_ids"], redis["required_count"], redis["preferred_count"]),
                         (3, ["1", "2", "3"], 2, 1))
        self.assertEqual(redis["skill"], "Redis")  # most common display name, ties to the first seen
        self.assertAlmostEqual(redis["learnable_share"], 2 / 3)
        self.assertEqual(redis["models_flagged_max"], 1)

    def test_cloud_variants_are_one_row(self):
        names = ["AWS/GCP/Azure (any one)", "Azure", "cloud platforms (AWS, GCP, or Azure)", "cloud infrastructure"]
        jobs = [job(str(i), enrichment={M9: result(required=[skill(n, "no", True)])}) for i, n in enumerate(names)]
        rows = bd.tally_gaps(jobs)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["skill"], rows[0]["jobs"]), (bd.CLOUD_DISPLAY, 4))

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


PROJECTS_MD = """# Project summaries

Projects: Conduit | BrokeNoMore | Cardiology-QA

## 1. Conduit (resume project)

**Stack.** Python, FastAPI, Redis, Docker, TypeScript dashboards.

**Not in the repo.** No Kubernetes, no Terraform. No GCP deployment.

## 2. BrokeNoMore (resume project)

**Stack.** FastAPI, Next.js 14 with TypeScript, SQLite.

**Not in the repo.** No Docker.

## 3. Cardiology-QA, "Cardiology LLM Fine-Tuning" (resume project)

**What it is.** QLoRA fine-tuning with PyTorch.

## Other repos (shorter)

- **sundial** (TypeScript, pushed 2026-10-07). Browser extension, IndexedDB.
- **miniGPT** (Jupyter). A transformer from scratch in PyTorch.
"""


class RepoMentionTests(unittest.TestCase):
    def test_index_skips_not_in_the_repo_and_splits_other_repos(self):
        index = bd.projects_index(PROJECTS_MD)
        self.assertEqual(sorted(index), ["BrokeNoMore", "Cardiology-QA", "Conduit", "miniGPT", "sundial"])
        self.assertNotIn("kubernetes", " ".join(index["Conduit"]).lower())
        self.assertNotIn("docker", " ".join(index["BrokeNoMore"]).lower())

    def test_repo_mentions(self):
        index = bd.projects_index(PROJECTS_MD)
        self.assertEqual(bd.repo_mentions("TypeScript", index), ["Conduit", "BrokeNoMore", "sundial"])
        self.assertEqual(bd.repo_mentions("Kubernetes", index), [])  # only named under "Not in the repo"
        self.assertEqual(bd.repo_mentions("Java", index), [])  # no substring matches inside other words
        self.assertEqual(bd.repo_mentions("PyTorch", index), ["Cardiology-QA", "miniGPT"])
        self.assertEqual(bd.repo_mentions("Go", index), [])  # too short to match safely

    def test_annotate_repos_marks_in_repos_and_on_resume(self):
        rows = [{"skill": "TypeScript", "jobs": 4}, {"skill": "Kubernetes", "jobs": 2}, {"skill": "Docker", "jobs": 1}]
        out = bd.annotate_repos(rows, PROJECTS_MD, "Skills: Python, Docker")
        by = {r["skill"]: r for r in out}
        self.assertEqual((by["TypeScript"]["in_repos"], by["TypeScript"]["on_resume"]), (["Conduit", "BrokeNoMore", "sundial"], False))
        self.assertEqual(by["Kubernetes"]["in_repos"], [])
        self.assertTrue(by["Docker"]["on_resume"])
        self.assertNotIn("in_repos", rows[0])  # input untouched

    def test_cloud_row_looks_for_the_individual_providers(self):
        md = "## 1. Conduit\n\n**Stack.** deployed on GCP.\n"
        out = bd.annotate_repos([{"skill": bd.CLOUD_DISPLAY, "jobs": 3}], md, "")
        self.assertEqual(out[0]["in_repos"], ["Conduit"])

    def test_format_tally_mentions_repos(self):
        rows = [{"skill": "TypeScript", "jobs": 4, "models_flagged_max": 2, "learnable_share": 1.0, "required_count": 4,
                 "preferred_count": 0, "in_repos": ["sundial"], "on_resume": False}]
        self.assertIn("already in your repos (sundial) but not on the resume", bd.format_tally(rows))


class CleanPlanTests(unittest.TestCase):
    def base(self):
        return {"verdict": "v",
                "rewordings": [{"current_text": "Built X.", "suggested_text": "Built X.", "jd_skills_aligned": []},
                               {"current_text": "Built Y.", "suggested_text": "Built Y well.", "jd_skills_aligned": []}],
                "project_extensions": [
                    {"project": "Conduit", "jd_skills_covered": ["Kubernetes"],
                     "pathway": [{"step": "1 - Write manifests", "detail": ""}, {"step": "2. Deploy", "detail": "d"},
                                 {"step": "Test it", "detail": ""}],
                     "resume_bullet_after_building": "Deployed on Kubernetes."}],
                "new_project": None,
                "skipped_gaps": [{"skill": "Kubernetes", "reason": "covered"}, {"skill": "Go", "reason": "unrelated"}]}

    def gaps(self, *names):
        return [{"skill": n} for n in names]

    def test_drops_no_op_rewordings(self):
        out = bd.clean_plan(self.base())
        self.assertEqual([r["suggested_text"] for r in out["rewordings"]], ["Built Y well."])

    def test_strips_pathway_numbering(self):
        out = bd.clean_plan(self.base())
        self.assertEqual([s["step"] for s in out["project_extensions"][0]["pathway"]], ["Write manifests", "Deploy", "Test it"])

    def test_enforces_bullet_prefix_once(self):
        out = bd.clean_plan(self.base())
        bullet = out["project_extensions"][0]["resume_bullet_after_building"]
        self.assertEqual(bullet, bd.BULLET_PREFIX + "Deployed on Kubernetes.")
        again = bd.clean_plan(out)
        self.assertEqual(again["project_extensions"][0]["resume_bullet_after_building"], bullet)

    def test_empty_bullet_stays_empty(self):
        plan = self.base()
        plan["project_extensions"][0]["resume_bullet_after_building"] = ""
        out = bd.clean_plan(plan)
        self.assertEqual(out["project_extensions"][0]["resume_bullet_after_building"], "")

    def test_skipped_gaps_covered_by_the_plan_are_removed(self):
        out = bd.clean_plan(self.base())
        self.assertEqual([g["skill"] for g in out["skipped_gaps"]], ["Go"])

    def test_input_not_modified(self):
        plan = self.base()
        bd.clean_plan(plan)
        self.assertEqual(len(plan["rewordings"]), 2)


class ExtraCheckTests(unittest.TestCase):
    RESUME = "Built Y with Redis. Reached 100 RPS."
    PROJECTS = "## 1. Conduit\n\n**Stack.** FastAPI and Pydantic models.\n"

    def check(self, plan, gaps=("Kubernetes",)):
        return bd.check_plan(plan, self.RESUME, ["Conduit"], gaps=[{"skill": g} for g in gaps], projects_md=self.PROJECTS)

    def test_extension_covering_no_listed_gap_is_flagged(self):
        plan = {"rewordings": [], "project_extensions": [{"project": "Conduit", "jd_skills_covered": ["AI tools"]}]}
        checked, warnings = self.check(plan)
        self.assertFalse(checked["project_extensions"][0]["covers_gap"])
        self.assertTrue(any("none of the listed gaps" in w for w in warnings))

    def test_extension_covering_a_gap_passes_with_loose_names(self):
        plan = {"rewordings": [], "project_extensions": [{"project": "Conduit", "jd_skills_covered": ["kubernetes (k8s)"]}]}
        checked, warnings = self.check(plan)
        self.assertTrue(checked["project_extensions"][0]["covers_gap"])
        self.assertEqual(warnings, [])

    def test_no_gaps_means_every_extension_is_flagged(self):
        plan = {"rewordings": [], "project_extensions": [{"project": "Conduit", "jd_skills_covered": ["Kubernetes"]}]}
        checked, _ = self.check(plan, gaps=())
        self.assertFalse(checked["project_extensions"][0]["covers_gap"])

    def test_invented_number_in_bullet_is_flagged(self):
        plan = {"rewordings": [], "project_extensions": [{"project": "Conduit", "jd_skills_covered": ["Kubernetes"],
                "resume_bullet_after_building": "Cut latency by 30% at 100 RPS."}]}
        _, warnings = self.check(plan)
        self.assertTrue(any("30%" in w for w in warnings))
        self.assertFalse(any("100" in w for w in warnings))  # 100 is on the resume

    def test_reword_adding_a_term_from_nowhere_is_flagged_repo_term_is_noted(self):
        plan = {"rewordings": [
            {"current_text": "Built Y with Redis.", "suggested_text": "Built Y with Redis and Kafka.", "jd_skills_aligned": ["Kafka"]},
            {"current_text": "Built Y with Redis.", "suggested_text": "Built Y with Redis and Pydantic.", "jd_skills_aligned": ["Pydantic"]},
            {"current_text": "Built Y with Redis.", "suggested_text": "Built Y with Redis, well.", "jd_skills_aligned": ["Redis"]}],
            "project_extensions": []}
        checked, warnings = self.check(plan)
        self.assertEqual(len(warnings), 1)  # only Kafka: Pydantic is in the repo summary, Redis was already there
        r = checked["rewordings"]
        self.assertEqual(r[0]["adds_unsupported"], ["Kafka"])
        self.assertEqual(r[1]["adds_from_repo"], ["Pydantic"])
        self.assertEqual((r[2]["adds_unsupported"], r[2]["adds_from_repo"]), ([], []))


if __name__ == "__main__":
    unittest.main()

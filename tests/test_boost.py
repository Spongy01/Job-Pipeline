import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
import boost  # noqa: E402
import llm  # noqa: E402

DATE = "2026-11-04"
M9, MG = "qwen3.5:9b", "gemma4:e4b"
RESUME = "Built a rate limiter with Redis.\nInstrumented Prometheus metrics."


def skill(name, in_resume="no", learnable=True):
    return {"skill": name, "in_resume": in_resume, "resume_evidence": "", "learnable_in_a_day": learnable, "learn_note": ""}


def ok_result(score=80, required=(), req_points=20):
    return {"ok": True, "data": {"score": score, "required_skills": list(required), "preferred_skills": [],
                                 "score_breakdown": {"required_skills": req_points}}}


def make_job(i, score=80, models=(M9, MG), enrichment=None, **over):
    job = {"id": i, "company": "Co" + i, "posting_name": "Engineer " + i, "description": "Unique JD text " + i,
           "linkedin_url": "https://li/" + i,
           "enrichment": enrichment or {m: ok_result(score, [skill("Kubernetes")]) for m in models}}
    job.update(over)
    return job


def good_plan(**over):
    plan = {"verdict": "Extend Conduit.", "rewordings": [{"resume_location": "Projects", "current_text": "Built a rate limiter with Redis.",
                                                           "suggested_text": "Built a distributed rate limiter with Redis.",
                                                           "jd_skills_aligned": ["Redis"], "why": "w"}],
            "project_extensions": [{"project": "Conduit", "what_to_build": "Add k8s manifests", "effort_hours": 16,
                                    "jd_skills_covered": ["Kubernetes"], "pathway": [{"step": "Write manifests", "detail": "d"}],
                                    "why_it_fits": "w", "resume_bullet_after_building": "Write this only after you have built it: x"}],
            "new_project": None, "skipped_gaps": []}
    plan.update(over)
    return json.dumps(plan)


ROLLUP_REPLY = json.dumps({"summary": "Kubernetes everywhere.", "general_project": {
    "kind": "extension", "project": "Conduit", "name": "Conduit on Kubernetes", "jd_skills_covered": ["Kubernetes"],
    "why": "w", "effort_hours": 20, "outline": "o"}})


class FakeClient:
    """Answers by prompt type; `plan` may be a callable taking the user prompt."""

    def __init__(self, plan=None, spec="# Spec\nbody", rollup=ROLLUP_REPLY, models=(M9,)):
        self.plan, self.spec, self.rollup, self.models = plan or good_plan(), spec, rollup, models
        self.calls, self.unloaded = [], []

    def list(self):
        return {"models": [{"model": m} for m in self.models]}

    def chat(self, **kw):
        if kw.get("keep_alive") == 0:
            self.unloaded.append(kw["model"])
            return {}
        self.calls.append(kw)
        system, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
        if "project specification" in system:
            kind, reply = "spec", self.spec
        elif "ONE general project" in system:
            kind, reply = "rollup", self.rollup
        else:
            kind, reply = "plan", (self.plan(user) if callable(self.plan) else self.plan)
        kw["kind"] = kind
        if isinstance(reply, Exception):
            raise reply
        return {"message": {"content": reply}}

    def kinds(self):
        return [c["kind"] for c in self.calls]


class BoostTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(PROJECT / "prompts", self.tmp / "prompts", ignore=shutil.ignore_patterns("__pycache__"))
        (self.tmp / "resume").mkdir()
        (self.tmp / "resume" / "projects.md").write_text("Projects: Conduit | BrokeNoMore\n\n## 1. Conduit\n")
        (self.tmp / "config.json").write_text(json.dumps({"resume_path": "resume/r.pdf", "rules": {}}))
        self.run_dir = self.tmp / "runs" / DATE
        self.run_dir.mkdir(parents=True)

    def setup_jobs(self, jobs, passed=None, rejected=()):
        passed = [j["id"] for j in jobs] if passed is None else passed
        (self.run_dir / "enriched.json").write_text(json.dumps(jobs))
        (self.run_dir / "filtered.json").write_text(json.dumps(
            [{"id": i, "stage": "passed"} for i in passed] + [{"id": i, "stage": "rejected"} for i in rejected]))

    def go(self, client=None, **kw):
        client = client or FakeClient()
        kw.setdefault("top_n", 5)
        self.client = client
        return boost.run(self.tmp, DATE, client=client, resume_text=RESUME, **kw)

    def plan_file(self, i):
        return json.loads((self.run_dir / "boost" / (i + ".json")).read_text())


class RunTests(BoostTestCase):
    def test_writes_plans_rollup_and_report(self):
        self.setup_jobs([make_job("1", 90), make_job("2", 70)])
        summary = self.go()
        self.assertEqual((summary["selected"], summary["done"], summary["failed"], summary["rollup"]), (2, 2, 0, "done"))
        rec = self.plan_file("1")
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["rank"], rec["score"], rec["score_model"], rec["prompt_version"]), (1, 90, M9, "v1"))
        self.assertEqual(rec["plan"]["rewordings"][0]["current_text_found"], True)
        self.assertEqual(rec["plan"]["project_extensions"][0]["project_known"], True)
        self.assertEqual(rec["gaps"][0]["skill"], "Kubernetes")
        rollup = json.loads((self.run_dir / "boost_rollup.json").read_text())
        self.assertEqual(rollup["tally"][0]["jobs"], 2)
        self.assertEqual(rollup["general_project"]["name"], "Conduit on Kubernetes")
        report = (self.run_dir / "boost.md").read_text()
        self.assertIn("## 1. Co1, Engineer 1", report)
        self.assertIn("Conduit on Kubernetes", report)

    def test_only_top_n_get_plans_rollup_covers_all_passed(self):
        jobs = [make_job(str(i), 50 + i) for i in range(1, 8)]
        self.setup_jobs(jobs)
        summary = self.go(top_n=3)
        files = sorted(p.stem for p in (self.run_dir / "boost").glob("*.json"))
        self.assertEqual(files, ["5", "6", "7"])
        self.assertEqual(summary["selected"], 3)
        self.assertEqual(self.client.kinds().count("plan"), 3)
        rollup = json.loads((self.run_dir / "boost_rollup.json").read_text())
        self.assertEqual(rollup["tally"][0]["jobs"], 7)

    def test_rejected_jobs_are_not_boosted(self):
        self.setup_jobs([make_job("1"), make_job("2", 99)], passed=["1"], rejected=["2"])
        self.go()
        self.assertEqual([p.stem for p in (self.run_dir / "boost").glob("*.json")], ["1"])

    def test_rerun_skips_done_jobs_and_force_redoes(self):
        self.setup_jobs([make_job("1")])
        self.go()
        summary = self.go()
        self.assertEqual((summary["done"], summary["skipped_existing"], summary["rollup"]), (0, 1, "skipped"))
        self.assertEqual(self.client.calls, [])
        summary = self.go(force=True)
        self.assertEqual((summary["done"], summary["rollup"]), (1, "done"))

    def test_rollup_redone_when_the_tally_changes(self):
        self.setup_jobs([make_job("1")])
        self.go()
        self.setup_jobs([make_job("1"), make_job("2", 60)])
        summary = self.go()
        self.assertEqual(summary["rollup"], "done")

    def test_failed_job_is_recorded_others_continue_and_retry_next_run(self):
        self.setup_jobs([make_job("1", 90), make_job("2", 80)])
        bad_then_good = lambda user: "garbage" if "Company: Co1" in user else good_plan()  # noqa: E731
        summary = self.go(FakeClient(plan=bad_then_good))
        self.assertEqual((summary["done"], summary["failed"]), (1, 1))
        self.assertFalse(self.plan_file("1")["ok"])
        self.assertTrue(self.plan_file("2")["ok"])
        self.assertIn("This job failed", (self.run_dir / "boost.md").read_text())
        summary = self.go(FakeClient())
        self.assertEqual((summary["done"], summary["skipped_existing"], summary["failed"]), (1, 1, 0))
        self.assertTrue(self.plan_file("1")["ok"])

    def test_fewer_than_top_n_and_none_passed(self):
        self.setup_jobs([make_job("1")])
        self.go(top_n=5)
        self.assertIn("Only 1 job(s)", (self.run_dir / "boost.md").read_text())
        shutil.rmtree(self.run_dir / "boost")
        (self.run_dir / "boost_rollup.json").unlink()
        self.setup_jobs([make_job("1")], passed=[], rejected=["1"])
        client = FakeClient()
        summary = self.go(client)
        self.assertEqual((summary["selected"], client.calls, summary["rollup"]), (0, [], "done"))
        self.assertIn("nothing to boost", (self.run_dir / "boost.md").read_text())

    def test_fallback_ranked_job_is_marked(self):
        j = make_job("1", enrichment={M9: {"ok": False, "error": "x"}, MG: ok_result(77, [skill("Go")])})
        self.setup_jobs([j])
        self.go()
        rec = self.plan_file("1")
        self.assertEqual((rec["score_model"], rec["fallback"]), (MG, True))
        self.assertIn("ranked by gemma4:e4b", (self.run_dir / "boost.md").read_text())

    def test_job_with_no_usable_enrichment_is_left_out(self):
        j = make_job("1", enrichment={M9: {"ok": False, "error": "x"}})
        self.setup_jobs([j, make_job("2")])
        summary = self.go()
        self.assertEqual(summary["selected"], 1)

    def test_em_dashes_removed_from_written_files(self):
        plan = json.loads(good_plan(verdict="Do this — now.", new_project={"name": "NP", "outline": "o — p"}))
        self.setup_jobs([make_job("1")])
        self.go(FakeClient(plan=json.dumps(plan), spec="# Spec\nA — B", rollup=ROLLUP_REPLY.replace("Kubernetes everywhere.", "A — B")))
        for path in self.run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn("—", path.read_text(), path)

    def test_unknown_project_and_bad_quote_become_warnings(self):
        plan = json.loads(good_plan())
        plan["rewordings"][0]["current_text"] = "A line that is not on the resume."
        plan["project_extensions"][0]["project"] = "Imaginary"
        self.setup_jobs([make_job("1")])
        self.go(FakeClient(plan=json.dumps(plan)))
        rec = self.plan_file("1")
        self.assertTrue(rec["ok"])
        self.assertEqual(len(rec["plan"]["rewordings"]), 1)  # kept, only flagged
        self.assertFalse(rec["plan"]["rewordings"][0]["current_text_found"])
        self.assertTrue(any("not found in the resume" in w for w in rec["warnings"]))
        self.assertTrue(any("Imaginary" in w for w in rec["warnings"]))
        report = (self.run_dir / "boost.md").read_text()
        self.assertIn("### Check these", report)

    def test_gaps_already_in_repos_become_resume_additions(self):
        (self.tmp / "resume" / "projects.md").write_text(
            "Projects: Conduit\n\n## 1. Conduit\n\n**Stack.** Python, Kubernetes manifests.\n")
        self.setup_jobs([make_job("1")])
        self.go()
        rec = self.plan_file("1")
        self.assertEqual(rec["resume_additions"], [{"skill": "Kubernetes", "projects": ["Conduit"]}])
        gap = rec["gaps"][0]
        self.assertEqual((gap["in_repos"], gap["on_resume"]), (["Conduit"], False))
        user_prompt = [c for c in self.client.calls if c["kind"] == "plan"][0]["messages"][1]["content"]
        self.assertIn("already in your repos (Conduit) but not on the resume: no need to build it", user_prompt)
        self.assertIn("Add to your resume", (self.run_dir / "boost.md").read_text())

    def test_general_project_covering_a_repo_skill_is_warned(self):
        (self.tmp / "resume" / "projects.md").write_text(
            "Projects: Conduit\n\n## 1. Conduit\n\n**Stack.** Python, Kubernetes manifests.\n")
        self.setup_jobs([make_job("1")])
        self.go()
        rollup = json.loads((self.run_dir / "boost_rollup.json").read_text())
        self.assertTrue(any("already in your repos" in w for w in rollup["warnings"]))

    def test_new_project_triggers_spec_call_and_file(self):
        plan = good_plan(new_project={"name": "Event Pipeline", "one_liner": "o", "jd_skills_covered": ["Kafka"],
                                      "why_no_existing_project_fits": "none", "effort_hours": 80, "outline": "o"})
        self.setup_jobs([make_job("1")])
        client = FakeClient(plan=plan)
        self.go(client)
        self.assertEqual(client.kinds(), ["plan", "spec", "rollup", "spec"])
        rec = self.plan_file("1")
        self.assertEqual(rec["spec_file"], "boost/specs/1-event-pipeline.md")
        self.assertEqual((self.run_dir / rec["spec_file"]).read_text(), "# Spec\nbody\n")

    def test_no_new_project_means_no_spec_call(self):
        self.setup_jobs([make_job("1")])
        client = FakeClient(rollup=json.dumps({"general_project": {"kind": "extension", "project": "Conduit", "name": "N"}}))
        self.go(client)
        self.assertEqual(client.kinds().count("spec"), 1)  # only the rollup's general project

    def test_spec_wrapped_in_code_fence_is_unwrapped(self):
        plan = good_plan(new_project={"name": "NP", "outline": "o"})
        self.setup_jobs([make_job("1")])
        self.go(FakeClient(plan=plan, spec="```markdown\n# Spec\nbody\n```"))
        self.assertEqual((self.run_dir / "boost/specs/1-np.md").read_text(), "# Spec\nbody\n")

    def test_failed_rollup_exit_state(self):
        self.setup_jobs([make_job("1")])
        summary = self.go(FakeClient(rollup="not json"))
        self.assertEqual(summary["rollup"], "failed")
        self.assertIn("The rollup failed", (self.run_dir / "boost.md").read_text())

    def test_config_boost_block_sets_top_n_and_cli_overrides(self):
        self.setup_jobs([make_job(str(i), 50 + i) for i in range(1, 6)])
        (self.tmp / "config.json").write_text(json.dumps({"resume_path": "x", "boost": {"top_n": 2}}))
        self.assertEqual(boost.run(self.tmp, DATE, client=FakeClient(), resume_text=RESUME)["selected"], 2)
        self.assertEqual(boost.run(self.tmp, DATE, top_n=4, client=FakeClient(), resume_text=RESUME)["selected"], 4)

    def boost_selected(self, boost_cfg, n_jobs=7, **kw):
        self.setup_jobs([make_job(str(i), 50 + i) for i in range(1, n_jobs + 1)])
        (self.tmp / "config.json").write_text(json.dumps({"resume_path": "x", "boost": boost_cfg}))
        return boost.run(self.tmp, DATE, client=FakeClient(), resume_text=RESUME, **kw)["selected"]

    def test_boost_type_all_plans_every_passed_job(self):
        self.assertEqual(self.boost_selected({"type": "all"}), 7)

    def test_boost_type_all_ignores_booster_count(self):
        self.assertEqual(self.boost_selected({"type": "all", "booster_count": 2}), 7)

    def test_boost_type_highest_uses_booster_count(self):
        self.assertEqual(self.boost_selected({"type": "highest", "booster_count": 2}), 2)

    def test_boost_type_highest_without_count_defaults_to_five(self):
        self.assertEqual(self.boost_selected({"type": "highest"}), 5)

    def test_no_boost_block_is_highest_five(self):
        self.setup_jobs([make_job(str(i), 50 + i) for i in range(1, 8)])
        (self.tmp / "config.json").write_text(json.dumps({"resume_path": "x"}))
        self.assertEqual(boost.run(self.tmp, DATE, client=FakeClient(), resume_text=RESUME)["selected"], 5)

    def test_cli_top_n_overrides_type_all(self):
        self.assertEqual(self.boost_selected({"type": "all"}, top_n=3), 3)

    def test_type_all_with_no_passed_jobs_selects_nothing(self):
        self.setup_jobs([make_job("1")], passed=[], rejected=["1"])
        (self.tmp / "config.json").write_text(json.dumps({"resume_path": "x", "boost": {"type": "all"}}))
        summary = boost.run(self.tmp, DATE, client=FakeClient(), resume_text=RESUME)
        self.assertEqual(summary["selected"], 0)

    def test_bad_boost_config_is_an_error(self):
        self.setup_jobs([make_job("1")])
        for bad in ({"type": "some"}, {"type": "highest", "booster_count": 0}, {"type": "highest", "booster_count": "3"},
                    {"type": "highest", "booster_count": True}, {"type": "highest", "booster_count": 2.5}, "all", ["all"]):
            (self.tmp / "config.json").write_text(json.dumps({"resume_path": "x", "boost": bad}))
            with self.assertRaises(boost.LLMError, msg=repr(bad)):
                boost.run(self.tmp, DATE, client=FakeClient(), resume_text=RESUME)

    def test_only_job_plans_just_that_job_even_if_rejected_and_skips_rollup_and_report(self):
        self.setup_jobs([make_job("1", 90), make_job("2", 70), make_job("3", 60)], passed=["1", "2"], rejected=["3"])
        summary = boost.run(self.tmp, DATE, only=["3"], client=FakeClient(), resume_text=RESUME)
        self.assertEqual((summary["selected"], summary["done"], summary["rollup"]), (1, 1, "skipped"))
        self.assertEqual(sorted(p.stem for p in (self.run_dir / "boost").glob("*.json")), ["3"])
        self.assertFalse((self.run_dir / "boost_rollup.json").exists())
        self.assertFalse((self.run_dir / "boost.md").exists())

    def test_only_job_with_an_existing_plan_is_reused(self):
        self.setup_jobs([make_job("1", 90)])
        boost.run(self.tmp, DATE, only=["1"], client=FakeClient(), resume_text=RESUME)
        again = FakeClient()
        summary = boost.run(self.tmp, DATE, only=["1"], client=again, resume_text=RESUME)
        self.assertEqual((summary["skipped_existing"], summary["done"]), (1, 0))
        self.assertEqual(again.kinds().count("plan"), 0)

    def test_only_unknown_job_selects_nothing(self):
        self.setup_jobs([make_job("1")])
        self.assertEqual(boost.run(self.tmp, DATE, only=["999"], client=FakeClient(), resume_text=RESUME)["selected"], 0)

    def test_run_for_job_merges_the_plan_into_jobs_json(self):
        self.setup_jobs([make_job("1", 90), make_job("2", 70)])
        (self.tmp / "jobs.json").write_text(json.dumps([{**make_job("1", 90), "boost": None, "stage": "passed", "run_date": DATE,
                                                         "user_status": None}]))
        summary = boost.run_for_job(self.tmp, DATE, "1", client=FakeClient(), resume_text=RESUME)
        self.assertEqual(summary["selected"], 1)
        jobs = json.loads((self.tmp / "jobs.json").read_text())
        self.assertTrue(jobs[0]["boost"]["ok"])
        self.assertEqual(len(jobs), 2)  # the merge also brought in the rest of that day's run

    def test_run_for_job_without_a_usable_score_is_an_error(self):
        self.setup_jobs([make_job("1", enrichment={M9: {"ok": False, "error": "x"}})])
        with self.assertRaises(boost.LLMError):
            boost.run_for_job(self.tmp, DATE, "1", client=FakeClient(), resume_text=RESUME)

    def test_job_cli_exits_3_while_a_worker_holds_the_lock(self):
        import subprocess
        sys.path.insert(0, str(PROJECT / "scripts"))
        import jobslock
        (self.tmp / "scripts").mkdir()
        for f in (PROJECT / "scripts").glob("*.py"):
            shutil.copy(f, self.tmp / "scripts" / f.name)
        with jobslock.worker_lock(self.tmp):
            r = subprocess.run([sys.executable, str(self.tmp / "scripts" / "boost.py"), DATE, "--job", "1"],
                               capture_output=True, text=True, cwd="/", timeout=60)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("worker", r.stderr)

    def write_config(self, data):
        (self.tmp / "config.json").write_text(json.dumps({"resume_path": "x", **data}))

    def test_booster_model_comes_from_config(self):
        self.setup_jobs([make_job("1", models=("other:1",))])
        self.write_config({"models": {"booster": "other:1"}})
        client = FakeClient(models=("other:1",))
        summary = boost.run(self.tmp, DATE, client=client, resume_text=RESUME)
        self.assertEqual(summary["model"], "other:1")
        self.assertEqual({c["model"] for c in client.calls}, {"other:1"})
        self.assertEqual(client.unloaded, ["other:1"])
        self.assertEqual(self.plan_file("1")["score_model"], "other:1")

    def test_command_line_model_beats_config_and_default_applies_without_either(self):
        self.setup_jobs([make_job("1")])
        self.write_config({"models": {"booster": "other:1"}})
        client = FakeClient(models=(M9, "other:1"))
        self.assertEqual(boost.run(self.tmp, DATE, model=M9, client=client, resume_text=RESUME)["model"], M9)
        self.write_config({})
        shutil.rmtree(self.run_dir / "boost")
        (self.run_dir / "boost_rollup.json").unlink()
        self.assertEqual(boost.run(self.tmp, DATE, client=FakeClient(), resume_text=RESUME)["model"], M9)

    def test_bad_models_section_is_an_error_and_writes_nothing(self):
        self.setup_jobs([make_job("1")])
        self.write_config({"models": {"booster": 7}})
        with self.assertRaises(llm.LLMError):
            self.go()
        self.assertFalse((self.run_dir / "boost.md").exists())

    def test_unloads_model_and_keep_loaded(self):
        self.setup_jobs([make_job("1")])
        self.go()
        self.assertEqual(self.client.unloaded, [M9])
        self.go(force=True, unload=False)
        self.assertEqual(self.client.unloaded, [])


class InputErrorTests(BoostTestCase):
    def snapshot(self):
        return sorted(str(p.relative_to(self.run_dir)) for p in self.run_dir.rglob("*") if p.is_file())

    def test_missing_projects_md_writes_nothing(self):
        self.setup_jobs([make_job("1")])
        (self.tmp / "resume" / "projects.md").unlink()
        before = self.snapshot()
        with self.assertRaises(llm.LLMError) as cm:
            self.go()
        self.assertIn("projects.md", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_or_missing_inputs_write_nothing(self):
        self.setup_jobs([make_job("1")])
        before = self.snapshot()
        (self.run_dir / "enriched.json").write_text("[{bad")
        with self.assertRaises(llm.LLMError):
            self.go()
        (self.run_dir / "filtered.json").unlink()
        with self.assertRaises(llm.LLMError):
            self.go()
        self.assertEqual([f for f in self.snapshot() if f.startswith(("boost", "boost_rollup"))], [])
        self.assertNotIn("boost.md", self.snapshot())
        self.assertEqual(before, ["enriched.json", "filtered.json"])

    def test_model_not_downloaded(self):
        self.setup_jobs([make_job("1")])
        with self.assertRaises(llm.LLMError) as cm:
            self.go(FakeClient(models=("other:1",)))
        self.assertIn(M9, str(cm.exception))
        self.assertNotIn("boost.md", self.snapshot())

    def test_unknown_prompt_version(self):
        self.setup_jobs([make_job("1")])
        with self.assertRaises(llm.LLMError):
            self.go(prompt_version="v9")

    def test_inputs_are_not_modified(self):
        self.setup_jobs([make_job("1")])
        files = [self.run_dir / "enriched.json", self.run_dir / "filtered.json", self.tmp / "config.json",
                 self.tmp / "resume" / "projects.md"]
        before = [f.read_bytes() for f in files]
        self.go()
        self.assertEqual([f.read_bytes() for f in files], before)


if __name__ == "__main__":
    unittest.main()

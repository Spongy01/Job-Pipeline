import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
import enrich  # noqa: E402

DATE = "2026-11-04"


def skill(name, in_resume="yes", learnable=None, evidence="", note=""):
    return {"skill": name, "in_resume": in_resume, "resume_evidence": evidence,
            "learnable_in_a_day": learnable, "learn_note": note}


def reply(**over):
    data = {
        "summary": "A role.",
        "experience": [],
        "required_skills": [skill("Python", evidence="Python")],
        "preferred_skills": [],
        "scores": {"experience_fit": 15, "role_alignment": 10, "reasoning": "ok"},
    }
    data.update(over)
    return json.dumps(data)


class FakeClient:
    def __init__(self, replies, models=("m1:latest", "m2:latest")):
        self.replies = list(replies)
        self.models = models
        self.calls = []
        self.unloaded = []

    def list(self):
        return {"models": [{"model": m} for m in self.models]}

    def chat(self, **kwargs):
        if kwargs.get("keep_alive") == 0:
            self.unloaded.append(kwargs["model"])
            return {}
        self.calls.append(kwargs)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return {"message": {"content": r}}


def make_job(i="1", **over):
    job = {"id": i, "posting_name": "Engineer", "company": "Acme", "location": "Remote",
           "work_model": "Remote", "description": "Acme needs Python. " * 20, "closed": False}
    job.update(over)
    return job


class PureHelperTests(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(enrich.extract_json('{"a": 1}'), '{"a": 1}')
        self.assertEqual(enrich.extract_json('```json\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(enrich.extract_json('Sure! {"a": {"b": 2}} Hope it helps'), '{"a": {"b": 2}}')
        with self.assertRaises(ValueError):
            enrich.extract_json("no json here")

    def test_resume_kerning_fix(self):
        raw = "LLM T raining: PyTorch  \nVector Stores, F AISS\nF amiliar: C++\n\n\n\nA role"
        self.assertEqual(enrich.clean_resume_text(raw),
                         "LLM Training: PyTorch\nVector Stores, FAISS\nFamiliar: C++\n\nA role")

    def test_resume_normal_text_untouched(self):
        self.assertEqual(enrich.clean_resume_text("Built a T-shirt shop in F#"), "Built a T-shirt shop in F#")

    def test_load_prompt_rejects_unknown_version(self):
        with self.assertRaises(enrich.EnrichError):
            enrich.load_prompt("v999", PROJECT)
        with self.assertRaises(enrich.EnrichError):
            enrich.load_prompt("../x", PROJECT)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.prompt = enrich.load_prompt("v1", PROJECT)

    def out(self, required, preferred=(), fit=10, align=10):
        return self.prompt.Output.model_validate({
            "summary": "s", "experience": [], "required_skills": list(required),
            "preferred_skills": list(preferred),
            "scores": {"experience_fit": fit, "role_alignment": align, "reasoning": "r"}})

    def test_all_yes(self):
        total, b = self.prompt.score(self.out([skill("a"), skill("b")], [skill("c")], 20, 20))
        self.assertEqual((b["required_skills"], b["preferred_skills"], b["learnability"]), (40, 10, 10))
        self.assertEqual(total, 100)

    def test_weights(self):
        req = [skill("a"), skill("b", "similar", True), skill("c", "no", True), skill("d", "no", False)]
        _, b = self.prompt.score(self.out(req))
        self.assertEqual(b["required_skills"], 40 * (1 + 0.75 + 0.5 + 0) / 4)
        # gaps: b, c, d -> 2 of 3 learnable
        self.assertEqual(b["learnability"], round(10 * 2 / 3, 1))

    def test_empty_lists_are_neutral(self):
        _, b = self.prompt.score(self.out([], []))
        self.assertEqual((b["required_skills"], b["preferred_skills"]), (20, 5))

    def test_total_is_rounded_sum(self):
        total, b = self.prompt.score(self.out([skill("a", "no", False)], [], 7, 3))
        self.assertEqual(total, 0 + 5 + 0 + 7 + 3)

    def test_model_scores_out_of_range_rejected(self):
        with self.assertRaises(Exception):
            self.out([skill("a")], fit=21)


class PostprocessTests(unittest.TestCase):
    def test_drops_zero_year_items_and_cross_list_duplicates(self):
        prompt = enrich.load_prompt("v1", PROJECT)
        out = prompt.Output.model_validate(json.loads(reply(
            experience=[{"area": "overall", "level": "required", "years_min": 0, "years_max": 0, "evidence": "x"},
                        {"area": "overall", "level": "required", "years_min": 2, "years_max": 4, "evidence": "y"}],
            required_skills=[skill("SQL")],
            preferred_skills=[skill(" sql "), skill("Rust", "no", False)])))
        out = prompt.postprocess(out)
        self.assertEqual([(e.years_min, e.years_max) for e in out.experience], [(2, 4)])
        self.assertEqual([s.skill for s in out.preferred_skills], ["Rust"])


class V2SchemaTests(unittest.TestCase):
    def setUp(self):
        self.prompt = enrich.load_prompt("v2", PROJECT)

    def test_v2_reply_validates_and_has_no_authorization_or_clearance_fields(self):
        out = self.prompt.Output.model_validate_json(reply())
        self.assertNotIn("work_authorization", self.prompt.Output.model_fields)
        self.assertNotIn("clearance", self.prompt.Output.model_fields)
        self.assertEqual(out.required_skills[0].skill, "Python")

    def test_optional_fields_default_instead_of_failing(self):
        data = json.loads(reply(required_skills=[{"skill": "Python", "in_resume": "yes"}]))
        out = self.prompt.Output.model_validate(data)
        self.assertEqual(out.required_skills[0].resume_evidence, "")
        self.assertIsNone(out.required_skills[0].learnable_in_a_day)

    def test_score_detail_adds_up(self):
        out = self.prompt.Output.model_validate(json.loads(reply(
            required_skills=[skill("a"), skill("b", "similar", True), skill("c", "no", True), skill("d", "no", False)],
            preferred_skills=[skill("e", "no", False)])))
        total, b = self.prompt.score(out)
        req = b["detail"]["required_skills"]
        self.assertEqual(req["max"], 40)
        self.assertEqual([i["weight"] for i in req["items"]], [1.0, 0.75, 0.5, 0.0])
        self.assertAlmostEqual(sum(i["points"] for i in req["items"]), b["required_skills"], places=1)
        self.assertEqual(b["detail"]["learnability"], {"max": 10, "gaps": 4, "learnable_gaps": 2})
        parts = ("required_skills", "preferred_skills", "learnability", "experience_fit", "role_alignment")
        self.assertEqual(total, round(sum(b[k] for k in parts)))

    def test_score_detail_empty_lists(self):
        out = self.prompt.Output.model_validate(json.loads(reply(required_skills=[], preferred_skills=[])))
        _, b = self.prompt.score(out)
        self.assertTrue(b["detail"]["required_skills"]["empty_list_default"])
        self.assertEqual(b["detail"]["required_skills"]["items"], [])
        self.assertEqual(b["required_skills"], 20)

    def test_enrich_one_stores_v2(self):
        res = enrich.enrich_one(FakeClient([reply()]), "m1", self.prompt, "R", make_job(), 4096, False, 0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["prompt_version"], "v2")

    def test_v1_postprocess_still_works_without_job(self):
        v1 = enrich.load_prompt("v1", PROJECT)
        res = enrich.enrich_one(FakeClient([reply()]), "m1", v1, "R", make_job(), 4096, False, 0)
        self.assertTrue(res["ok"])

    def test_v1_still_loads_and_is_unchanged_in_version(self):
        self.assertEqual(enrich.load_prompt("v1", PROJECT).VERSION, "v1")


class EnrichOneTests(unittest.TestCase):
    def setUp(self):
        self.prompt = enrich.load_prompt("v1", PROJECT)

    def go(self, client, retries=1):
        return enrich.enrich_one(client, "m1", self.prompt, "RESUME TEXT", make_job(), 4096, False, retries)

    def test_success_and_prompt_contents(self):
        client = FakeClient([reply()])
        res = self.go(client)
        self.assertTrue(res["ok"])
        self.assertEqual(res["prompt_version"], "v1")
        self.assertIn("score", res["data"])
        self.assertIn("required_skills", res["data"]["score_breakdown"])
        self.assertNotIn("scores", res["data"])
        call = client.calls[0]
        self.assertNotIn("format", call)  # MLX runner rejects structured output
        self.assertEqual(call["options"], {"temperature": 0, "num_ctx": 4096})
        user = call["messages"][1]["content"]
        self.assertIn("RESUME TEXT", user)
        self.assertIn("Acme needs Python.", user)

    def test_fenced_reply_accepted(self):
        self.assertTrue(self.go(FakeClient(["```json\n" + reply() + "\n```"]))["ok"])

    def test_repair_after_invalid_reply(self):
        client = FakeClient(["not json at all", reply()])
        res = self.go(client)
        self.assertTrue(res["ok"])
        self.assertEqual(res["attempts"], 2)
        repair = client.calls[1]["messages"][-1]["content"]
        self.assertIn("not valid", repair)

    def test_gives_up_and_records_error(self):
        res = self.go(FakeClient(['{"summary": 1}']), retries=1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["attempts"], 2)
        self.assertIn("error", res)
        self.assertNotIn("data", res)

    def test_transport_error_recorded(self):
        res = self.go(FakeClient([RuntimeError("boom")]), retries=0)
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["error"])

    def test_think_flag_rejected_is_retried_without_it(self):
        client = FakeClient([RuntimeError("model does not support think"), reply()])
        res = self.go(client)
        self.assertTrue(res["ok"])
        self.assertIn("think", client.calls[0])
        self.assertNotIn("think", client.calls[1])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(PROJECT / "prompts", self.tmp / "prompts", ignore=shutil.ignore_patterns("__pycache__"))
        self.run_dir = self.tmp / "runs" / DATE
        self.run_dir.mkdir(parents=True)
        self.jobs = [make_job("1"), make_job("2", closed=True), make_job("3")]
        self.clean = self.run_dir / "clean.json"
        self.clean.write_text(json.dumps(self.jobs))

    def run_it(self, client, models=("m1",), **kw):
        return enrich.run(self.tmp, DATE, list(models), client=client, resume_text="RESUME", **kw)

    def load(self):
        return json.loads((self.run_dir / "enriched.json").read_text())

    def test_writes_enriched_and_leaves_clean_untouched(self):
        before = self.clean.read_bytes()
        summary = self.run_it(FakeClient([reply()]))
        self.assertEqual((summary["done"], summary["failed"], summary["skipped_closed"]), (2, 0, 1))
        out = self.load()
        self.assertEqual([j["id"] for j in out], ["1", "2", "3"])
        self.assertTrue(out[0]["enrichment"]["m1"]["ok"])
        self.assertEqual(out[1]["enrichment"], {})  # closed job skipped
        self.assertEqual(self.clean.read_bytes(), before)

    def test_models_run_one_at_a_time_and_are_unloaded(self):
        client = FakeClient([reply()])
        self.run_it(client, models=["m1", "m2"])
        order = [c["model"] for c in client.calls]
        self.assertEqual(order, ["m1", "m1", "m2", "m2"])  # all of m1 before any of m2
        self.assertEqual(client.unloaded, ["m1", "m2"])

    def test_keep_loaded(self):
        client = FakeClient([reply()])
        self.run_it(client, unload=False)
        self.assertEqual(client.unloaded, [])

    def test_unload_failure_is_ignored(self):
        class Broken(FakeClient):
            def chat(self, **kw):
                if kw.get("keep_alive") == 0:
                    raise RuntimeError("nope")
                return super().chat(**kw)
        self.assertEqual(self.run_it(Broken([reply()]))["failed"], 0)

    def test_jd_flags_added_for_open_jobs_only(self):
        jobs = [make_job("1", description="We do not sponsor visas. Active Secret clearance required. " * 5),
                make_job("2", closed=True)]
        self.clean.write_text(json.dumps(jobs))
        self.run_it(FakeClient([reply()]))
        out = self.load()
        self.assertIs(out[0]["jd_flags"]["sponsorship"], False)
        self.assertTrue(out[0]["jd_flags"]["clearance"])
        self.assertNotIn("jd_flags", out[1])

    def test_jd_flags_do_not_depend_on_models_or_skips(self):
        self.run_it(FakeClient([reply()]))
        self.run_it(FakeClient([reply()]))  # everything skipped the second time
        self.assertIn("jd_flags", self.load()[0])

    def test_limit(self):
        summary = self.run_it(FakeClient([reply()]), limit=1)
        self.assertEqual(summary["jobs"], 1)
        self.assertEqual([j["id"] for j in self.load()], ["1"])

    def test_second_model_merges_and_existing_skipped(self):
        self.run_it(FakeClient([reply()]), models=["m1"])
        client = FakeClient([reply()])
        summary = self.run_it(client, models=["m1", "m2"])
        self.assertEqual((summary["done"], summary["skipped_existing"]), (2, 2))
        self.assertEqual(len(client.calls), 2)  # only m2 was called
        self.assertEqual(sorted(self.load()[0]["enrichment"]), ["m1", "m2"])

    def test_force_redoes(self):
        self.run_it(FakeClient([reply()]))
        client = FakeClient([reply()])
        self.run_it(client, force=True)
        self.assertEqual(len(client.calls), 2)

    def test_failed_entries_are_retried_next_run(self):
        self.run_it(FakeClient(["garbage"]), retries=0)
        self.assertFalse(self.load()[0]["enrichment"]["m1"]["ok"])
        summary = self.run_it(FakeClient([reply()]))
        self.assertEqual(summary["done"], 2)
        self.assertTrue(self.load()[0]["enrichment"]["m1"]["ok"])

    def test_limited_run_keeps_other_enriched_jobs(self):
        self.run_it(FakeClient([reply()]))
        self.run_it(FakeClient([reply()]), models=["m2"], limit=1)
        out = {j["id"]: j for j in self.load()}
        self.assertEqual(sorted(out), ["1", "2", "3"])
        self.assertIn("m1", out["3"]["enrichment"])

    def test_out_dir(self):
        other = self.tmp / "elsewhere"
        self.run_it(FakeClient([reply()]), out_dir=other)
        self.assertTrue((other / "enriched.json").exists())
        self.assertFalse((self.run_dir / "enriched.json").exists())

    def test_missing_model_fails_before_any_call(self):
        client = FakeClient([reply()])
        with self.assertRaises(enrich.EnrichError) as cm:
            self.run_it(client, models=["nope"])
        self.assertIn("nope", str(cm.exception))
        self.assertEqual(client.calls, [])
        self.assertFalse((self.run_dir / "enriched.json").exists())

    def test_server_unreachable(self):
        class Down:
            def list(self):
                raise ConnectionError("refused")
        with self.assertRaises(enrich.EnrichError) as cm:
            self.run_it(Down())
        self.assertIn("ollama serve", str(cm.exception))

    def test_missing_and_corrupt_input(self):
        self.clean.unlink()
        with self.assertRaises(enrich.EnrichError):
            self.run_it(FakeClient([reply()]))
        self.clean.write_text("[{broken")
        with self.assertRaises(enrich.EnrichError):
            self.run_it(FakeClient([reply()]))
        self.assertFalse((self.run_dir / "enriched.json").exists())

    def test_corrupt_existing_enriched_raises_not_overwritten(self):
        (self.run_dir / "enriched.json").write_text("{oops")
        with self.assertRaises(enrich.EnrichError):
            self.run_it(FakeClient([reply()]))
        self.assertEqual((self.run_dir / "enriched.json").read_text(), "{oops")

    def test_unknown_prompt_version(self):
        with self.assertRaises(enrich.EnrichError):
            self.run_it(FakeClient([reply()]), prompt_version="v9")


if __name__ == "__main__":
    unittest.main()

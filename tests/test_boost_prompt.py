import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
import llm  # noqa: E402

p = llm.load_prompt("v1", PROJECT, "boost")


def plan(**over):
    d = {"verdict": "Do X."}
    d.update(over)
    return json.dumps(d)


class PlanModelTests(unittest.TestCase):
    def test_minimal_reply(self):
        out = p.Plan.model_validate_json(plan())
        self.assertEqual((out.rewordings, out.project_extensions, out.new_project, out.skipped_gaps), ([], [], None, []))

    def test_full_reply(self):
        out = p.Plan.model_validate_json(plan(
            rewordings=[{"resume_location": "Projects", "current_text": "a", "suggested_text": "b",
                         "jd_skills_aligned": ["Redis"], "why": "w"}],
            project_extensions=[{"project": "Conduit", "what_to_build": "CI", "effort_hours": 12,
                                 "pathway": [{"step": "one", "detail": "d"}], "jd_skills_covered": ["GitHub Actions"],
                                 "resume_bullet_after_building": "Write this only after you have built it: x"}],
            new_project={"name": "N", "outline": "o", "effort_hours": 80},
            skipped_gaps=[{"skill": "Go", "reason": "r"}]))
        self.assertEqual(out.project_extensions[0].pathway[0].step, "one")
        self.assertEqual(out.new_project.effort_hours, 80)

    def test_missing_verdict_rejected(self):
        with self.assertRaises(Exception):
            p.Plan.model_validate_json(json.dumps({"rewordings": []}))

    def test_lenient_strings_for_lists_and_pathway_steps(self):
        out = p.Plan.model_validate_json(plan(project_extensions=[{
            "project": "Conduit", "what_to_build": "x", "jd_skills_covered": "Kubernetes",
            "pathway": ["first step", {"step": "second"}]}]))
        ext = out.project_extensions[0]
        self.assertEqual(ext.jd_skills_covered, ["Kubernetes"])
        self.assertEqual([s.step for s in ext.pathway], ["first step", "second"])

    def test_null_lists_become_empty(self):
        out = p.Plan.model_validate_json(plan(rewordings=None, skipped_gaps=None))
        self.assertEqual((out.rewordings, out.skipped_gaps), ([], []))

    def test_rewording_needs_current_and_suggested_text(self):
        with self.assertRaises(Exception):
            p.Plan.model_validate_json(plan(rewordings=[{"current_text": "a"}]))


class RollupModelTests(unittest.TestCase):
    def test_valid(self):
        out = p.Rollup.model_validate_json(json.dumps({
            "summary": "s", "general_project": {"kind": "new", "name": "N", "jd_skills_covered": "AWS"}}))
        self.assertEqual(out.general_project.jd_skills_covered, ["AWS"])

    def test_kind_checked(self):
        with self.assertRaises(Exception):
            p.Rollup.model_validate_json(json.dumps({"general_project": {"kind": "maybe", "name": "N"}}))


class TemplateTests(unittest.TestCase):
    def test_templates_format_without_keyerror(self):
        p.USER_TEMPLATE_PLAN.format(resume="r", projects="p", title="t", company="c", description="d",
                                    gaps="g", output_format=p.PLAN_FORMAT)
        p.USER_TEMPLATE_SPEC.format(resume="r", projects="p", context="c", outline="o")
        p.USER_TEMPLATE_ROLLUP.format(resume="r", projects="p", tally="t", output_format=p.ROLLUP_FORMAT)

    def test_system_prompts_contain_the_honesty_rules(self):
        plan_prompt = p.SYSTEM_PLAN.lower()
        self.assertIn("write this only after you have built it", plan_prompt)
        self.assertIn("word for word", plan_prompt)
        self.assertIn("never invent", plan_prompt)
        self.assertIn("em dash", plan_prompt)
        self.assertIn("never invent", p.SYSTEM_ROLLUP.lower())
        self.assertIn("never invent", p.SYSTEM_SPEC.lower())

    def test_prompts_contain_no_em_dash(self):
        for text in (p.SYSTEM_PLAN, p.SYSTEM_SPEC, p.SYSTEM_ROLLUP, p.PLAN_FORMAT, p.ROLLUP_FORMAT,
                     p.USER_TEMPLATE_PLAN, p.USER_TEMPLATE_SPEC, p.USER_TEMPLATE_ROLLUP):
            self.assertNotIn("—", text)

    def test_formats_are_valid_json_skeletons_of_the_models(self):
        sample = p.PLAN_FORMAT[p.PLAN_FORMAT.index("{"):p.PLAN_FORMAT.rindex("}") + 1]
        sample = sample.replace('"Write this only after you have built it: string"', '"x"')
        p.Plan.model_validate_json(sample)
        sample = p.ROLLUP_FORMAT[p.ROLLUP_FORMAT.index("{"):p.ROLLUP_FORMAT.rindex("}") + 1]
        p.Rollup.model_validate_json(sample)

    def test_version(self):
        self.assertEqual(p.VERSION, "v1")


if __name__ == "__main__":
    unittest.main()

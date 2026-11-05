import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import boost_report as br  # noqa: E402


def record(rank=1, ok=True, **over):
    r = {"ok": ok, "rank": rank, "score": 90 - rank, "score_model": "qwen3.5:9b", "fallback": False,
         "job": {"id": str(rank), "company": "Acme%d" % rank, "posting_name": "Engineer", "linkedin_url": "https://x/%d" % rank},
         "warnings": [], "spec_file": None}
    if ok:
        r["plan"] = {"verdict": "Add CI to Conduit.",
                     "rewordings": [{"resume_location": "Projects / Conduit", "current_text": "Built X.",
                                     "suggested_text": "Built X with Y.", "jd_skills_aligned": ["Y"], "why": "JD says Y"}],
                     "project_extensions": [{"project": "Conduit", "what_to_build": "GitHub Actions CI", "effort_hours": 8,
                                             "jd_skills_covered": ["CI"], "why_it_fits": "tests exist",
                                             "pathway": [{"step": "Write workflow", "detail": "run pytest"}],
                                             "resume_bullet_after_building": "Write this only after you have built it: Added CI."}],
                     "new_project": None, "skipped_gaps": [{"skill": "Go", "reason": "unrelated"}]}
    else:
        r["error"] = "ValidationError: bad"
    r.update(over)
    return r


ROLLUP = {"ok": True, "summary": "Cloud keeps coming up.",
          "tally": [{"skill": "AWS", "jobs": 5, "models_flagged_max": 2, "learnable_share": 0.5, "job_ids": [],
                     "in_repos": [], "on_resume": False},
                    {"skill": "TypeScript", "jobs": 4, "models_flagged_max": 3, "learnable_share": 1.0, "job_ids": [],
                     "in_repos": ["sundial", "BrokeNoMore"], "on_resume": False}],
          "general_project": {"kind": "new", "name": "Cloud Gateway", "one_liner": "o", "jd_skills_covered": ["AWS"],
                              "why": "w", "effort_hours": 40, "project": ""},
          "spec_file": "boost/specs/rollup-cloud-gateway.md", "warnings": ["Project not found in projects.md: 'X'"]}


class ReportTests(unittest.TestCase):
    def test_sections_in_order(self):
        text = br.render_report("2026-11-04", ROLLUP, [record(1), record(2)], ["a note"])
        order = [text.index(s) for s in ("# Boost report, 2026-11-04", "a note", "## Across today's jobs",
                                         "| AWS | 5 |", "### Already in your repos, not on your resume", "### General project: Cloud Gateway", "## 1. Acme1, Engineer",
                                         "### Rewordings", "### Extend an existing project", "### Skipped gaps",
                                         "## 2. Acme2, Engineer")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Spec: `boost/specs/rollup-cloud-gateway.md`", text)
        self.assertIn("1. Write workflow - run pytest", text)
        self.assertIn("https://x/1", text)

    def test_repo_skills_are_listed_for_the_resume(self):
        text = br.render_report("d", ROLLUP, [], [])
        self.assertIn("- TypeScript: asked for in 4 job(s); shown in sundial, BrokeNoMore", text)
        self.assertIn("| TypeScript | 4 | 3 | 100% | sundial, BrokeNoMore |", text)
        self.assertIn("| AWS | 5 | 2 | 50% | no |", text)

    def test_additions_and_new_words_shown(self):
        r = record(1, resume_additions=[{"skill": "TypeScript", "projects": ["sundial", "Solwards"]}])
        r["plan"]["rewordings"][0]["added_text"] = "and Prometheus."
        text = br.render_report("d", None, [r], [])
        self.assertIn("### Add to your resume (your repos already show these)", text)
        self.assertIn("- TypeScript: shown in sundial, Solwards", text)
        self.assertIn("  - New in this wording: and Prometheus.", text)

    def test_risky_rewordings_are_in_their_own_section_with_reasons(self):
        r = record(1)
        safe = {"current_text": "Built A.", "suggested_text": "Built A well.", "resume_location": "Projects", "risky": False}
        bad = {"current_text": "Built B.", "suggested_text": "Built B and C.", "resume_location": "Experience", "risky": True,
               "problems": ["The rewording adds 'C', which is not on your resume or in your project summaries."]}
        r["plan"]["rewordings"] = [bad, safe]
        r["warnings"] = list(bad["problems"]) + ["Project not found in projects.md: 'X'"]
        text = br.render_report("d", None, [r], [])
        self.assertLess(text.index("### Rewordings"), text.index("### Risky rewordings"))
        self.assertLess(text.index("Built A well."), text.index("Built B and C."))
        self.assertIn("  - Problem: The rewording adds 'C'", text)
        check = text[text.index("### Check these"):]
        self.assertIn("Project not found in projects.md", check)
        self.assertNotIn("The rewording adds 'C'", check)  # shown once, under the risky rewording

    def test_warnings_listed(self):
        text = br.render_report("d", ROLLUP, [record(1, warnings=["Reworded text not found in the resume: 'z'"])], [])
        self.assertIn("### Check these", text)
        self.assertIn("- Reworded text not found in the resume: 'z'", text)
        self.assertIn("- Check: Project not found in projects.md: 'X'", text)

    def test_no_plans_says_so(self):
        text = br.render_report("d", None, [], ["No jobs passed the filter, so there is nothing to boost."])
        self.assertIn("No jobs were selected", text)
        self.assertIn("No rollup was produced.", text)

    def test_failed_job_and_failed_rollup(self):
        text = br.render_report("d", {"ok": False, "error": "boom"}, [record(1, ok=False)], [])
        self.assertIn("The rollup failed: boom", text)
        self.assertIn("This job failed: ValidationError: bad", text)
        self.assertIn("retried on the next run", text)

    def test_new_project_and_fallback_note(self):
        r = record(1, fallback=True, spec_file="boost/specs/1-x.md")
        r["plan"]["new_project"] = {"name": "NP", "one_liner": "o", "jd_skills_covered": ["K8s"],
                                    "why_no_existing_project_fits": "none", "effort_hours": 60}
        text = br.render_report("d", ROLLUP, [r], [])
        self.assertIn("### New project: NP (about 60 h)", text)
        self.assertIn("Spec: `boost/specs/1-x.md`", text)
        self.assertIn("fallback model", text)

    def test_no_em_dash_in_report(self):
        self.assertNotIn("—", br.render_report("d", ROLLUP, [record(1)], ["n"]))


if __name__ == "__main__":
    unittest.main()

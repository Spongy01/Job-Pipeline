import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import jobs_query as jq  # noqa: E402

M9, MG = "qwen3.5:9b", "gemma4:e4b"
TODAY = "2026-11-04"


def res(score, req=20, skills=()):
    return {"ok": True, "data": {"score": score, "score_breakdown": {"required_skills": req},
                                 "required_skills": [{"skill": s} for s in skills], "preferred_skills": []}}


def job(i, s9=80, sg=70, run_date=TODAY, stage="passed", scraped="2026-11-04T10:00:00Z", boost=None,
        user_status=None, flags=None, title=None, company=None, skills=(), **over):
    j = {"id": i, "posting_name": title or "Engineer " + i, "company": company or "Co" + i, "location": "Remote",
         "work_model": "Remote", "scraped_at": scraped, "run_date": run_date, "stage": stage,
         "apply_type": "external", "apply_url": "https://x/" + i, "review": False,
         "enrichment": {}, "jd_flags": flags or {"sponsorship": None, "clearance": False, "us_citizenship_required": False},
         "boost": boost, "user_status": user_status}
    if s9 is not None:
        j["enrichment"][M9] = res(s9, skills=skills)
    if sg is not None:
        j["enrichment"][MG] = res(sg, skills=skills)
    j.update(over)
    return j


def ids(view):
    return [c["id"] for c in view["cards"]]


class BasicsTests(unittest.TestCase):
    def test_models_in(self):
        self.assertEqual(jq.models_in([job("1"), job("2", s9=None)]), [MG, M9])

    def test_job_score_and_average(self):
        j = job("1", 90, 70)
        self.assertEqual(jq.job_score(j, M9), 90)
        self.assertEqual(jq.job_score(j, "average"), 80)
        self.assertIsNone(jq.job_score(job("1", None, None), M9))
        self.assertIsNone(jq.job_score(job("1", None, None), "average"))

    def test_is_flagged(self):
        base = {"sponsorship": None, "clearance": False, "us_citizenship_required": False}
        self.assertFalse(jq.is_flagged(job("1")))
        for k, v in (("sponsorship", False), ("clearance", True), ("us_citizenship_required", True)):
            self.assertTrue(jq.is_flagged(job("1", flags={**base, k: v})), k)
        self.assertFalse(jq.is_flagged(job("1", flags={**base, "sponsorship": True})))
        self.assertTrue(jq.is_flagged(job("1", review=True)))
        self.assertFalse(jq.is_flagged(job("1", flags=None, jd_flags=None)))

    def test_card_fields_and_badges(self):
        c = jq.card(job("1", 95, 60, boost={"ok": True}, apply_url=None), [M9, MG])
        self.assertEqual((c["id"], c["title"], c["company"], c["stage"], c["boost"]), ("1", "Engineer 1", "Co1", "passed", True))
        self.assertEqual(c["scores"], {M9: 95, MG: 60})
        self.assertTrue(c["badges"]["models_differ"])
        self.assertTrue(c["badges"]["no_apply_url"])
        self.assertFalse(jq.card(job("2", 80, 70), [M9, MG])["badges"]["models_differ"])
        self.assertEqual(jq.card(job("3", s9=None), [M9, MG])["scores"][M9], None)


class QueryTests(unittest.TestCase):
    def q(self, jobs, **kw):
        kw.setdefault("today", TODAY)
        return jq.query(jobs, **kw)

    def test_default_is_today_sorted_by_9b_score(self):
        jobs = [job("1", 70), job("2", 90), job("3", 80), job("old", 99, run_date="2026-11-01")]
        v = self.q(jobs)
        self.assertEqual(ids(v), ["2", "3", "1"])
        self.assertEqual(v["models"], [MG, M9])

    def test_yesterday_and_from(self):
        jobs = [job("t", run_date=TODAY), job("y", run_date="2026-11-03"), job("o", run_date="2026-10-30")]
        self.assertEqual(ids(self.q(jobs, range="yesterday")), ["y"])
        self.assertEqual(sorted(ids(self.q(jobs, range="from", from_date="2026-11-03"))), ["t", "y"])
        self.assertEqual(sorted(ids(self.q(jobs, range="from", from_date="2026-10-01"))), ["o", "t", "y"])
        self.assertEqual(self.q(jobs, range="from", from_date="2026-11-03")["range"], {"from": "2026-11-03", "to": None})

    def test_today_with_no_jobs_is_empty_and_reports_latest_run_date(self):
        v = self.q([job("o", run_date="2026-11-01"), job("p", run_date="2026-11-02")])
        self.assertTrue(v["empty"])
        self.assertEqual((v["cards"], v["latest_run_date"]), ([], "2026-11-02"))
        self.assertEqual(self.q([])["latest_run_date"], None)

    def test_filters_leaving_nothing_are_not_empty(self):
        v = self.q([job("1")], show="rejected")
        self.assertEqual((v["cards"], v["empty"]), ([], False))

    def test_sort_ties_and_missing_scores(self):
        a = job("a", 80); b = job("b", 80); c = job("c", s9=None, sg=99)
        a["enrichment"][M9]["data"]["score_breakdown"]["required_skills"] = 10
        b["enrichment"][M9]["data"]["score_breakdown"]["required_skills"] = 30
        self.assertEqual(ids(self.q([a, b, c])), ["b", "a", "c"])  # tie broken by required skills, no score last

    def test_sort_by_chosen_model_and_average(self):
        jobs = [job("1", 90, 10), job("2", 50, 99)]
        self.assertEqual(ids(self.q(jobs, model=MG)), ["2", "1"])
        self.assertEqual(ids(self.q(jobs, model="average")), ["2", "1"])  # averages 74 and 50

    def test_sort_recent(self):
        jobs = [job("1", scraped="2026-11-04T08:00:00Z"), job("2", scraped="2026-11-04T12:00:00Z"),
                job("3", scraped="2026-11-04T12:00:00Z")]
        self.assertEqual(ids(self.q(jobs, sort="recent")), ["2", "3", "1"])

    def test_show_filters(self):
        jobs = [job("p"), job("r", stage="rejected"), job("f", flags={"clearance": True}), job("b", boost={"ok": True})]
        self.assertEqual(sorted(ids(self.q(jobs, show="passed"))), ["b", "f", "p"])
        self.assertEqual(ids(self.q(jobs, show="rejected")), ["r"])
        self.assertEqual(ids(self.q(jobs, show="flagged")), ["f"])
        self.assertEqual(ids(self.q(jobs, show="boost")), ["b"])
        self.assertEqual(len(self.q(jobs, show="all")["cards"]), 4)

    def test_status_filter(self):
        jobs = [job("n"), job("a", user_status="applied"), job("s", user_status="skipped")]
        self.assertEqual(ids(self.q(jobs, status="new")), ["n"])
        self.assertEqual(ids(self.q(jobs, status="applied")), ["a"])
        self.assertEqual(ids(self.q(jobs, status="skipped")), ["s"])
        self.assertEqual(len(self.q(jobs)["cards"]), 3)

    def test_counts_ignore_show_but_respect_range_status_and_search(self):
        jobs = [job("p"), job("r", stage="rejected"), job("f", flags={"clearance": True}), job("b", boost={"ok": True}),
                job("old", run_date="2026-11-01"), job("done", user_status="applied")]
        v = self.q(jobs, show="rejected", status="new")
        self.assertEqual(v["counts"], {"all": 4, "passed": 3, "rejected": 1, "flagged": 1, "boost": 1})
        self.assertEqual(ids(v), ["r"])

    def test_search_matches_title_company_and_skill_names(self):
        jobs = [job("1", title="Data Wrangler"), job("2", company="Acme"), job("3", skills=["Kubernetes"]), job("4")]
        self.assertEqual(ids(self.q(jobs, q="wrangler")), ["1"])
        self.assertEqual(ids(self.q(jobs, q="ACME")), ["2"])
        self.assertEqual(ids(self.q(jobs, q="kubern")), ["3"])
        self.assertEqual(ids(self.q(jobs, q="  ")), ids(self.q(jobs)))  # blank search filters nothing

    def test_series_follow_card_order_and_skip_missing_scores(self):
        jobs = [job("1", 70, 10), job("2", 90, None), job("3", None, 50)]
        v = self.q(jobs)
        self.assertEqual(ids(v), ["2", "1", "3"])
        self.assertEqual(v["series"][M9], [90, 70])
        self.assertEqual(v["series"][MG], [10, 50])
        self.assertEqual(v["averages"][M9], 80.0)
        self.assertEqual(v["averages"][MG], 30.0)

    def test_series_with_no_cards(self):
        v = self.q([job("1")], show="rejected")
        self.assertEqual(v["series"], {MG: [], M9: []})
        self.assertEqual(v["averages"], {MG: None, M9: None})


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_missing_file_is_empty_list(self):
        self.assertEqual(jq.load_jobs(self.tmp / "jobs.json"), [])

    def test_corrupt_file_raises(self):
        (self.tmp / "jobs.json").write_text("[{bad")
        with self.assertRaises(jq.JobsError):
            jq.load_jobs(self.tmp / "jobs.json")

    def test_non_list_raises(self):
        (self.tmp / "jobs.json").write_text("{}")
        with self.assertRaises(jq.JobsError):
            jq.load_jobs(self.tmp / "jobs.json")

    def test_loads_list(self):
        (self.tmp / "jobs.json").write_text(json.dumps([job("1")]))
        self.assertEqual(jq.load_jobs(self.tmp / "jobs.json")[0]["id"], "1")


if __name__ == "__main__":
    unittest.main()

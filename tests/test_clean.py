import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import clean  # noqa: E402

DATE = "2026-11-04"
LONG = ("Acme builds things. " * 30).strip()  # 599 chars, mentions Acme


def make_job(**over):
    job = {
        "id": "123",
        "posting_name": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "work_model": "Remote",
        "posted_text": "2 hours ago",
        "posted_on": "2026-11-04",
        "reposted": False,
        "description": LONG,
        "apply_type": "external",
        "apply_url": "https://example.com/apply",
        "job_board_url": None,
        "linkedin_url": "https://www.linkedin.com/jobs/view/123/",
        "scraped_at": "2026-11-04T18:55:00Z",
    }
    job.update(over)
    return job


def clean_one(**over):
    cleaned, report = clean.clean_all([make_job(**over)])
    return cleaned[0], report


class PostedOnTests(unittest.TestCase):
    def test_units_and_prefixes(self):
        s = "2026-11-10T08:00:00Z"
        cases = {
            "5 minutes ago": "2026-11-10",
            "1 minute ago": "2026-11-10",
            "5 hours ago": "2026-11-10",
            "Reposted 2 hours ago": "2026-11-10",
            "3 days ago": "2026-11-07",
            "Posted 3 days ago": "2026-11-07",
            "2 weeks ago": "2026-10-27",
            "Reposted 2 weeks ago": "2026-10-27",
            "1 month ago": "2026-10-11",
            "1 year ago": "2025-11-10",
            "POSTED 3 DAYS AGO": "2026-11-07",
        }
        for text, expected in cases.items():
            self.assertEqual(clean.parse_posted_on(text, s), expected, text)

    def test_unparseable(self):
        self.assertIsNone(clean.parse_posted_on("just now", "2026-11-10T08:00:00Z"))
        self.assertIsNone(clean.parse_posted_on("3 days ago", "garbage"))
        self.assertIsNone(clean.parse_posted_on(None, "2026-11-10T08:00:00Z"))

    def test_unparsed_keeps_existing_or_null_and_is_reported(self):
        job, report = clean_one(posted_text="sometime", posted_on="2026-11-01")
        self.assertEqual(job["posted_on"], "2026-11-01")
        self.assertEqual(report["unparsed_posted_text"], ["123"])
        job, report = clean_one(posted_text="sometime", posted_on=None)
        self.assertIsNone(job["posted_on"])
        self.assertEqual(report["unparsed_posted_text"], ["123"])

    def test_reposted(self):
        self.assertTrue(clean_one(posted_text="Reposted 2 hours ago", reposted=False)[0]["reposted"])
        self.assertTrue(clean_one(posted_text="REPOSTED 2 hours ago")[0]["reposted"])
        self.assertFalse(clean_one(posted_text="2 hours ago", reposted=True)[0]["reposted"])


class ApplyUrlTests(unittest.TestCase):
    def test_unwrap_linkedin_redirect(self):
        url = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjobs.example.com%2Fa%3Fx%3D1%26utm_source%3Dli&trk=abc"
        self.assertEqual(clean.clean_apply_url(url), "https://jobs.example.com/a?x=1")

    def test_tracking_params_removed_others_and_fragment_kept(self):
        url = ("https://x.com/job?id=7&utm_medium=a&UTM_Campaign=b&gclid=1&fbclid=2&trk=3"
               "&trackingId=4&refId=5&referrer=6&gh_src=keep#frag")
        self.assertEqual(clean.clean_apply_url(url), "https://x.com/job?id=7&gh_src=keep#frag")

    def test_all_params_tracking_drops_question_mark(self):
        self.assertEqual(clean.clean_apply_url("https://x.com/j?utm_source=a"), "https://x.com/j")

    def test_idempotent(self):
        urls = [
            "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fa.com%2Fb%3Fc%3D%2520d%26utm_x%3D1",
            "https://x.com/job?id=7&utm_medium=a#frag",
            "https://x.com/plain",
        ]
        for url in urls:
            once = clean.clean_apply_url(url)
            self.assertEqual(clean.clean_apply_url(once), once)

    def test_none_stays_none(self):
        self.assertIsNone(clean.clean_apply_url(None))

    def test_cleaner_is_idempotent_on_its_own_output(self):
        raw = [make_job(apply_url="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjobs.ashbyhq.com%2Facme%2Fabc%2Fapplication%3Futm_source%3Dx",
                        description=LONG + "  \n\n\n\n" + "more   \n")]
        first, _ = clean.clean_all(raw)
        second, _ = clean.clean_all(first)
        self.assertEqual(first, second)


class JobBoardTests(unittest.TestCase):
    def check(self, url, expected):
        self.assertEqual(clean.derive_job_board_url(url), expected, url)

    def test_boards(self):
        self.check("https://jobs.ashbyhq.com/Mintive/a93e/application", "https://jobs.ashbyhq.com/Mintive")
        self.check("https://job-boards.greenhouse.io/pmg/jobs/858", "https://job-boards.greenhouse.io/pmg")
        self.check("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme")
        self.check("https://jobs.lever.co/acme/abc-123/apply", "https://jobs.lever.co/acme")

    def test_workday_without_locale(self):
        self.check("https://morningstar.wd5.myworkdayjobs.com/morningstar/job/Chicago/Assoc_REQ-1",
                   "https://morningstar.wd5.myworkdayjobs.com/morningstar")

    def test_workday_with_locale(self):
        self.check("https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/NYC/Dev_R1",
                   "https://acme.wd1.myworkdayjobs.com/en-US/Careers")
        self.check("https://acme.wd1.myworkdayjobs.com/fr/Careers/job/NYC/Dev_R1",
                   "https://acme.wd1.myworkdayjobs.com/fr/Careers")

    def test_unknown_and_null(self):
        self.check("https://careers.servicenow.com/jobs/1/x/", None)
        self.check("https://grnh.se/abc", None)
        self.check(None, None)


class FlagTests(unittest.TestCase):
    def flags(self, **over):
        return clean_one(**over)[0]["flags"]

    def test_short_boundary(self):
        self.assertIn("description_short", self.flags(description="Acme " + "x" * 294))      # 299
        self.assertNotIn("description_short", self.flags(description="Acme " + "x" * 295))   # 300

    def test_truncated_boundaries(self):
        self.assertIn("description_truncated_suspect", self.flags(description="Acme " + "x" * 4995))   # 5000
        self.assertNotIn("description_truncated_suspect", self.flags(description="Acme " + "x" * 4994))  # 4999
        self.assertNotIn("description_truncated_suspect", self.flags(description="Acme " + "x" * 4996))  # 5001
        self.assertIn("description_truncated_suspect", self.flags(description="Acme " + "x" * 995))     # 1000

    def test_truncation_uses_original_length(self):
        # 1000 chars raw, but trailing whitespace is stripped by normalizing
        desc = "Acme " + "x" * 985 + " " * 10
        job, _ = clean_one(description=desc)
        self.assertEqual(len(desc), 1000)
        self.assertIn("description_truncated_suspect", job["flags"])
        self.assertLess(len(job["description"]), 1000)

    def test_missing_apply_url(self):
        self.assertIn("missing_apply_url", self.flags(apply_type="external", apply_url=None))
        self.assertNotIn("missing_apply_url", self.flags(apply_type="easy_apply", apply_url=None))
        self.assertNotIn("missing_apply_url", self.flags())

    def test_company_not_in_description_is_soft(self):
        job, _ = clean_one(company="Zed Corp", description="x" * 400)
        self.assertEqual(job["flags"], ["company_not_in_description"])
        self.assertFalse(job["review"])

    def test_company_suffix_and_case(self):
        desc = "Welcome to ACME " + "x" * 400
        self.assertNotIn("company_not_in_description", self.flags(company="Acme, Inc.", description=desc))
        self.assertNotIn("company_not_in_description", self.flags(company="Acme LLC", description=desc))
        self.assertIn("company_not_in_description", self.flags(company="Other Ltd", description=desc))

    def test_review_true_for_hard_flag(self):
        job, _ = clean_one(description="Acme short")
        self.assertTrue(job["review"])

    def test_clean_job_has_no_flags(self):
        job, _ = clean_one()
        self.assertEqual(job["flags"], [])
        self.assertFalse(job["review"])


class DescriptionTests(unittest.TestCase):
    def test_normalize(self):
        text = "  Acme line   \nsecond\t\n\n\n\n\nthird  \n\n"
        self.assertEqual(clean.normalize_description(text), "Acme line\nsecond\n\nthird")

    def test_double_newline_kept(self):
        self.assertEqual(clean.normalize_description("a\n\nb"), "a\n\nb")


class ClosedTests(unittest.TestCase):
    def test_closed_passthrough(self):
        job, _ = clean_one(closed=True, description="tiny", apply_type="external", apply_url=None)
        self.assertTrue(job["closed"])
        self.assertIsNone(job["description"])
        self.assertEqual(job["flags"], [])
        self.assertFalse(job["review"])

    def test_missing_closed_is_false(self):
        self.assertFalse(clean_one()[0]["closed"])


class ValidationTests(unittest.TestCase):
    def test_missing_required_dropped_and_reported(self):
        bad = [
            make_job(id="1", posting_name=""),
            make_job(id="2", company=None),
            make_job(id="3", linkedin_url="   "),
            make_job(id="abc"),
            "not a dict",
        ]
        no_id = make_job()
        del no_id["id"]
        bad.append(no_id)
        cleaned, report = clean.clean_all(bad + [make_job(id="9")])
        self.assertEqual([j["id"] for j in cleaned], ["9"])
        self.assertEqual(len(report["dropped_invalid"]), 6)
        self.assertTrue(all(d["reason"] for d in report["dropped_invalid"]))
        self.assertEqual(report["counts"]["dropped_invalid"], 6)

    def test_id_normalized_to_string(self):
        cleaned, _ = clean.clean_all([make_job(id=456)])
        self.assertEqual(cleaned[0]["id"], "456")

    def test_duplicates_keep_first(self):
        cleaned, report = clean.clean_all([
            make_job(id="5", posting_name="first"),
            make_job(id="6"),
            make_job(id="5", posting_name="second"),
        ])
        self.assertEqual([j["posting_name"] for j in cleaned if j["id"] == "5"], ["first"])
        self.assertEqual(report["duplicates"], [{"id": "5", "index": 2, "kept_index": 0}])

    def test_order_kept(self):
        cleaned, _ = clean.clean_all([make_job(id="3"), make_job(id="1"), make_job(id="2")])
        self.assertEqual([j["id"] for j in cleaned], ["3", "1", "2"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "scripts").mkdir()
        shutil.copy(SCRIPTS / "clean.py", self.tmp / "scripts" / "clean.py")
        self.run_dir = self.tmp / "runs" / DATE
        self.run_dir.mkdir(parents=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.tmp / "scripts" / "clean.py"), *args],
                              capture_output=True, text=True, cwd="/")

    def test_success_one_json_line_and_outputs(self):
        raw = [make_job(id="1"), make_job(id="2", closed=True), make_job(id="1")]
        raw_path = self.run_dir / "raw.json"
        raw_path.write_text(json.dumps(raw))
        before = raw_path.read_bytes()
        r = self.run_cli(DATE)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        counts = json.loads(lines[0])
        self.assertEqual((counts["input"], counts["written"], counts["closed"], counts["duplicates"]), (3, 2, 1, 1))
        self.assertEqual(len(json.loads((self.run_dir / "clean.json").read_text())), 2)
        report = json.loads((self.run_dir / "clean_report.json").read_text())
        self.assertEqual(report["counts"]["duplicates"], 1)
        self.assertEqual(raw_path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()),
                         ["clean.json", "clean_report.json", "raw.json"])

    def test_does_not_touch_other_files(self):
        (self.run_dir / "raw.json").write_text("[]")
        (self.tmp / "jobs.json").write_text("[]")
        (self.tmp / "config.json").write_text("{}")
        self.assertEqual(self.run_cli(DATE).returncode, 0)
        self.assertEqual((self.tmp / "jobs.json").read_text(), "[]")
        self.assertEqual((self.tmp / "config.json").read_text(), "{}")

    def test_missing_raw_fails_and_writes_nothing(self):
        r = self.run_cli(DATE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("raw.json", r.stderr)
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_corrupt_raw_fails_and_writes_nothing(self):
        (self.run_dir / "raw.json").write_text("[{not json")
        r = self.run_cli(DATE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid JSON", r.stderr)
        self.assertEqual([p.name for p in self.run_dir.iterdir()], ["raw.json"])

    def test_non_list_raw_fails(self):
        (self.run_dir / "raw.json").write_text("{}")
        self.assertNotEqual(self.run_cli(DATE).returncode, 0)

    def test_bad_date_arg_fails(self):
        self.assertNotEqual(self.run_cli("11-04-2026").returncode, 0)


if __name__ == "__main__":
    unittest.main()

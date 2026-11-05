import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jobslock  # noqa: E402
import serve  # noqa: E402

M9, MG = "qwen3.5:9b", "gemma4:e4b"
TODAY = "2026-11-04"


def job(i, s9=80, sg=70, stage="passed", **over):
    j = {"id": i, "posting_name": "Engineer " + i, "company": "Co" + i, "location": "Remote", "work_model": "Remote",
         "scraped_at": "2026-11-04T10:00:00Z", "run_date": TODAY, "stage": stage, "apply_type": "external",
         "apply_url": "https://x/" + i, "jd_flags": {}, "boost": None, "user_status": None, "description": "JD " + i,
         "enrichment": {M9: {"ok": True, "data": {"score": s9, "score_breakdown": {"required_skills": 20},
                                                   "required_skills": [], "preferred_skills": []}},
                        MG: {"ok": True, "data": {"score": sg, "score_breakdown": {"required_skills": 20},
                                                   "required_skills": [], "preferred_skills": []}}}}
    j.update(over)
    return j


class ServeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "web").mkdir()
        (self.tmp / "web" / "index.html").write_text("<html>page</html>")
        (self.tmp / "web" / "app.js").write_text("console.log(1)")
        (self.tmp / "web" / "style.css").write_text("body{}")
        (self.tmp / "secret.txt").write_text("do not serve")
        self.jobs = [job("1", 90), job("2", 70), job("3", 50, stage="rejected")]
        self.write_jobs(self.jobs)
        (self.tmp / "rollups.json").write_text(json.dumps({TODAY: {"ok": True, "summary": "s", "tally": []}}))
        self.server = serve.create_server(self.tmp, 0, today=TODAY)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def write_jobs(self, jobs):
        (self.tmp / "jobs.json").write_text(json.dumps(jobs))

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type") or "", data

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        return status, json.loads(data)

    def post_status(self, job_id, payload, headers=None, raw=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        body = raw if raw is not None else json.dumps(payload)
        return self.request("POST", "/api/job/%s/status" % job_id, body=body, headers=h)


class ReadTests(ServeTestCase):
    def test_index_and_static_served(self):
        status, ctype, data = self.request("GET", "/")
        self.assertEqual((status, data), (200, b"<html>page</html>"))
        self.assertIn("text/html", ctype)
        status, ctype, data = self.request("GET", "/static/app.js")
        self.assertEqual((status, data), (200, b"console.log(1)"))
        self.assertIn("javascript", ctype)
        self.assertIn("text/css", self.request("GET", "/static/style.css")[1])

    def test_static_path_traversal_blocked(self):
        for path in ("/static/../jobs.json", "/static/%2e%2e/jobs.json", "/static/..%2fjobs.json",
                     "/static//etc/passwd", "/static/%2Fetc/passwd", "/static/../secret.txt", "/static/missing.js",
                     "/static/", "/static/sub/../../secret.txt"):
            status, _, data = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"do not serve", data)
        self.assertEqual(self.request("GET", "/jobs.json")[0], 404)
        self.assertEqual(self.request("GET", "/secret.txt")[0], 404)

    def test_view_returns_cards_series_counts(self):
        status, view = self.get_json("/api/view?range=today&sort=score&show=all&status=any&model=%s&q=" % M9)
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in view["cards"]], ["1", "2", "3"])
        self.assertEqual(view["series"][M9], [90, 70, 50])
        self.assertEqual(view["counts"]["rejected"], 1)
        self.assertEqual(view["models"], [MG, M9])
        status, view = self.get_json("/api/view?show=rejected")
        self.assertEqual([c["id"] for c in view["cards"]], ["3"])

    def test_view_defaults_and_validation(self):
        self.assertEqual(self.get_json("/api/view")[0], 200)
        for bad in ("range=nope", "sort=nope", "show=nope", "status=nope", "range=from&from=not-a-date"):
            status, body = self.get_json("/api/view?" + bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", body)

    def test_view_today_empty_reports_latest_run_date(self):
        self.write_jobs([job("1", run_date="2026-10-01")])
        status, view = self.get_json("/api/view?range=today")
        self.assertEqual((status, view["empty"], view["latest_run_date"]), (200, True, "2026-10-01"))

    def test_job_and_rollup_routes(self):
        status, rec = self.get_json("/api/job/1")
        self.assertEqual((status, rec["description"]), (200, "JD 1"))
        self.assertEqual(self.get_json("/api/job/nope")[0], 404)
        status, rollup = self.get_json("/api/rollup/" + TODAY)
        self.assertEqual((status, rollup["summary"]), (200, "s"))
        self.assertEqual(self.get_json("/api/rollup/2020-01-01")[0], 404)
        self.assertEqual(self.get_json("/api/rollup/not-a-date")[0], 400)

    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.request("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.request("DELETE", "/api/job/1/status")[0], 405)
        self.assertEqual(self.request("POST", "/api/view", body="{}", headers={"Content-Type": "application/json"})[0], 404)

    def test_wrong_host_header_refused_everywhere(self):
        self.assertEqual(self.request("GET", "/api/view", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.request("GET", "/api/view", headers={"Host": "localhost:%d" % self.port})[0], 200)

    def test_server_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_external_changes_to_jobs_json_are_picked_up(self):
        self.assertEqual(len(self.get_json("/api/view")[1]["cards"]), 3)
        self.write_jobs(self.jobs + [job("4", 60)])
        self.assertEqual(len(self.get_json("/api/view")[1]["cards"]), 4)


class StatusTests(ServeTestCase):
    def test_set_status_waits_for_lock(self):
        import time
        results = []
        with jobslock.jobs_lock(self.tmp):
            t = threading.Thread(target=lambda: results.append(self.post_status("2", {"status": "applied"})))
            t.start()
            time.sleep(0.3)
            self.assertEqual(results, [])
            self.assertIsNone(json.loads((self.tmp / "jobs.json").read_text())[1]["user_status"])
        t.join(5)
        self.assertEqual(results[0][0], 200)

    def file_jobs(self):
        return json.loads((self.tmp / "jobs.json").read_text())

    def test_status_post_changes_only_that_job(self):
        before = self.file_jobs()
        status, _, data = self.post_status("2", {"status": "applied"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"id": "2", "user_status": "applied"})
        after = self.file_jobs()
        self.assertEqual(after[1]["user_status"], "applied")
        after[1]["user_status"] = None
        self.assertEqual(after, before)  # everything else identical
        self.assertEqual(json.loads((self.tmp / "jobs.json.bak").read_text()), before)
        self.assertEqual(self.get_json("/api/view?status=applied")[1]["cards"][0]["id"], "2")

    def test_status_can_be_set_to_skipped_and_cleared_with_null(self):
        self.post_status("1", {"status": "skipped"})
        self.assertEqual(self.file_jobs()[0]["user_status"], "skipped")
        self.post_status("1", {"status": None})
        self.assertIsNone(self.file_jobs()[0]["user_status"])

    def test_status_rejects_bad_requests_and_leaves_file_unchanged(self):
        before = (self.tmp / "jobs.json").read_bytes()
        cases = [
            (self.post_status("1", {"status": "done"}), 400),
            (self.post_status("1", {"status": 5}), 400),
            (self.post_status("1", {}), 400),
            (self.post_status("1", None, raw="not json"), 400),
            (self.post_status("1", None, raw="[1]"), 400),
            (self.post_status("nope", {"status": "applied"}), 404),
            (self.post_status("1", {"status": "applied"}, headers={"Host": "evil.example"}), 403),
            (self.post_status("1", {"status": "applied"}, headers={"Content-Type": "text/plain"}), 415),
            (self.post_status("1", {"status": "applied"}, headers={"Content-Type": "application/x-www-form-urlencoded"}), 415),
        ]
        for (status, _, data), expected in cases:
            self.assertEqual(status, expected, data)
            self.assertIn("error", json.loads(data))
        self.assertEqual((self.tmp / "jobs.json").read_bytes(), before)
        self.assertFalse((self.tmp / "jobs.json.bak").exists())

    def test_status_survives_restart(self):
        self.post_status("1", {"status": "applied"})
        store = serve.JobsStore(self.tmp)
        self.assertEqual(store.get("1")["user_status"], "applied")

    def test_concurrent_status_writes_do_not_lose_updates(self):
        threads = [threading.Thread(target=self.post_status, args=(i, {"status": "applied"})) for i in ("1", "2", "3")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual([j["user_status"] for j in self.file_jobs()], ["applied"] * 3)

    def test_huge_body_refused(self):
        status, _, _ = self.post_status("1", None, raw=json.dumps({"status": "applied", "pad": "x" * 100000}))
        self.assertEqual(status, 413)


class ErrorTests(ServeTestCase):
    def test_corrupt_jobs_json_gives_500_with_message_not_empty_list(self):
        (self.tmp / "jobs.json").write_text("[{corrupt")
        for path in ("/api/view", "/api/job/1"):
            status, body = self.get_json(path)
            self.assertEqual(status, 500, path)
            self.assertIn("jobs.json", body["error"])
        self.assertEqual(self.post_status("1", {"status": "applied"})[0], 500)
        self.assertEqual((self.tmp / "jobs.json").read_text(), "[{corrupt")

    def test_corrupt_rollups_json_gives_500(self):
        (self.tmp / "rollups.json").write_text("{bad")
        self.assertEqual(self.get_json("/api/rollup/" + TODAY)[0], 500)

    def test_missing_jobs_json_is_an_empty_page_not_an_error(self):
        (self.tmp / "jobs.json").unlink()
        status, view = self.get_json("/api/view")
        self.assertEqual((status, view["cards"], view["empty"]), (200, [], True))


class StartupTests(unittest.TestCase):
    def test_port_already_in_use_gives_a_clear_message(self):
        import socket
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "scripts").mkdir()
        for name in ("serve.py", "jobs_query.py", "clean.py", "jobslock.py"):
            shutil.copy(SCRIPTS / name, tmp / "scripts" / name)
        (tmp / "web").mkdir()
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]
        r = subprocess.run([sys.executable, str(tmp / "scripts" / "serve.py"), "--port", str(port)],
                           capture_output=True, text=True, cwd="/", timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot listen on port %d" % port, r.stderr)
        self.assertIn("--port", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_corrupt_jobs_json_refuses_to_start(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "scripts").mkdir()
        for name in ("serve.py", "jobs_query.py", "clean.py", "jobslock.py"):
            shutil.copy(SCRIPTS / name, tmp / "scripts" / name)
        (tmp / "web").mkdir()
        (tmp / "jobs.json").write_text("[{corrupt")
        r = subprocess.run([sys.executable, str(tmp / "scripts" / "serve.py"), "--port", "0"],
                           capture_output=True, text=True, cwd="/", timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("jobs.json", r.stderr)


if __name__ == "__main__":
    unittest.main()

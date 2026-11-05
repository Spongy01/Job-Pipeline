import io
import json
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jobqueue  # noqa: E402
import start  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeProc:
    def __init__(self, cmd, exit_code=None):
        self.cmd, self.exit_code = cmd, exit_code
        self.terminated = False

    def poll(self):
        return self.exit_code

    @property
    def returncode(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.exit_code


class HelperTests(unittest.TestCase):
    def test_ollama_up_true_against_local_stub_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/api/tags" else 404)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(start.ollama_up(port=server.server_address[1]))

    def test_ollama_up_false_when_nothing_listens(self):
        self.assertFalse(start.ollama_up(port=free_port()))

    def test_port_in_use_true_and_false(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        self.assertTrue(start.port_in_use(s.getsockname()[1]))
        self.assertFalse(start.port_in_use(free_port()))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.procs = []
        self.worker_exit = None

    def spawn(self, cmd, **kwargs):
        proc = FakeProc(cmd, self.worker_exit if "worker.py" in " ".join(map(str, cmd)) else None)
        self.procs.append(proc)
        return proc

    def go(self, ollama=True, page=False, ticks=1, ollama_checks=None):
        calls = []

        def wait(_):
            calls.append(1)
            if len(calls) >= ticks:
                raise KeyboardInterrupt

        checks = iter(ollama_checks) if ollama_checks is not None else None
        out = io.StringIO()
        with redirect_stdout(out):
            code = start.main([], spawn=self.spawn, wait=wait,
                              ollama_check=(lambda: next(checks, False)) if checks else (lambda: ollama),
                              port_check=lambda port: page, root=self.tmp, python="py")
        self.lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return code

    def started(self):
        return [" ".join(map(str, p.cmd)) for p in self.procs]

    def test_main_skips_ollama_and_page_already_running(self):
        self.go(ollama=True, page=True)
        self.assertEqual(len(self.procs), 1)
        self.assertIn("worker.py", self.started()[0])
        statuses = {l["component"]: l["status"] for l in self.lines}
        self.assertEqual((statuses["ollama"], statuses["page"]), ("already running", "already running"))

    def test_main_starts_ollama_when_down(self):
        self.go(ollama_checks=[False, False, True], page=True, ticks=2)
        self.assertEqual(self.procs[0].cmd, ["ollama", "serve"])
        self.assertEqual({l["component"]: l["status"] for l in self.lines}["ollama"], "started")

    def test_main_starts_page_when_port_free(self):
        self.go(ollama=True, page=False)
        self.assertTrue(any("serve.py" in c for c in self.started()))

    def test_main_creates_the_run(self):
        self.go()
        self.assertEqual(jobqueue.read_run(self.tmp)["stopping"], False)

    def test_main_terminates_only_what_it_started_on_interrupt(self):
        self.go(ollama=True, page=True)
        self.assertEqual([p.terminated for p in self.procs], [True])  # the worker only; ollama and page were not ours

    def test_main_stops_everything_it_started_on_interrupt(self):
        self.go(ollama_checks=[False, True], page=False)
        self.assertEqual(len(self.procs), 3)
        self.assertTrue(all(p.terminated for p in self.procs))

    def test_ollama_that_never_comes_up_is_an_error_and_is_stopped(self):
        code = self.go(ollama=False, page=False, ticks=1000)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.procs[0].cmd, ["ollama", "serve"])
        self.assertTrue(self.procs[0].terminated)
        self.assertEqual(len(self.procs), 1)

    def test_worker_exit_code_is_reported_and_page_keeps_running(self):
        self.worker_exit = 1
        self.go(ollama=True, page=False, ticks=2)
        exited = [l for l in self.lines if l.get("component") == "worker" and l.get("status") == "exited"]
        self.assertEqual(exited[0]["code"], 1)
        page = next(p for p in self.procs if "serve.py" in " ".join(map(str, p.cmd)))
        self.assertTrue(page.terminated)  # only at interrupt, after it kept running through the ticks

    def test_missing_venv_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as ctx:
            start.main([], spawn=self.spawn, wait=lambda s: None, ollama_check=lambda: True,
                       port_check=lambda p: True, root=self.tmp)
        self.assertIn(".venv", str(ctx.exception))
        self.assertEqual(self.procs, [])


if __name__ == "__main__":
    unittest.main()

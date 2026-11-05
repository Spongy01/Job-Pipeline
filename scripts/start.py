#!/usr/bin/env python3
"""Usage: python3 scripts/start.py [--port 8765]

One command for a streaming run. In the foreground it:
  1. starts `ollama serve` if Ollama does not answer yet (and stops it again on Ctrl-C only if it started it),
  2. starts the run (queue/run.json) and the worker (.venv/bin/python scripts/worker.py),
  3. starts the jobs page (scripts/serve.py), unless the port already answers.
Prints one JSON line per component. Ctrl-C stops what this command started. When the worker finishes
after stoppipeline.py, the page and Ollama keep running until Ctrl-C.
"""
import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobqueue  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765
OLLAMA_WAIT_SECONDS = 30


def ollama_up(host="127.0.0.1", port=11434, timeout=1.0):
    try:
        with urllib.request.urlopen("http://%s:%d/api/tags" % (host, port), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _say(**fields):
    print(json.dumps(fields), flush=True)


def main(argv, spawn=subprocess.Popen, wait=time.sleep, ollama_check=None, port_check=None, root=ROOT, python=None):
    parser = argparse.ArgumentParser(description="Start Ollama, the worker and the jobs page.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    root = Path(root)
    ollama_check = ollama_check or ollama_up
    port_check = port_check or port_in_use
    python = python or str(root / ".venv" / "bin" / "python")
    if python == str(root / ".venv" / "bin" / "python") and not Path(python).exists():
        sys.exit("start.py: %s not found. Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" % python)

    started = []
    try:
        if ollama_check():
            _say(component="ollama", status="already running")
        else:
            started.append(spawn(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(OLLAMA_WAIT_SECONDS):
                if ollama_check():
                    break
                wait(1)
            else:
                _say(component="ollama", status="error", error="did not answer within %d seconds" % OLLAMA_WAIT_SECONDS)
                return 1
            _say(component="ollama", status="started")

        run = jobqueue.begin_run(root)
        worker = spawn([python, str(root / "scripts" / "worker.py")])
        started.append(worker)
        _say(component="worker", status="started", run_date=run["date"])

        if port_check(args.port):
            _say(component="page", status="already running", url="http://localhost:%d/" % args.port)
        else:
            started.append(spawn([sys.executable, str(root / "scripts" / "serve.py"), "--port", str(args.port)]))
            _say(component="page", status="started", url="http://localhost:%d/" % args.port)

        reported = False
        while True:
            wait(1)
            code = worker.poll()
            if code is not None and not reported:
                reported = True
                _say(component="worker", status="exited", code=code)
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in started:
            proc.terminate()
        for proc in started:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

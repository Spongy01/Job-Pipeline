# Streaming Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jobs handed to `enqueue.py` by the scraper are cleaned, enriched, filtered and merged into `jobs.json` one at a time while scraping continues; `stoppipeline.py` then triggers boost, the rollup and the final merge; one command starts Ollama, the worker and the web page.

**Architecture:** A folder spool (`queue/pending|processing|failed`) plus `queue/run.json` carry jobs and run state. A single worker process reuses the existing clean, enrich, filter, merge and boost code per job. A cross-process file lock protects `jobs.json` against the worker, `merge.py` and the web server writing at once. The page polls.

**Tech Stack:** Python 3 standard library (`fcntl`, `json`, `subprocess`, `urllib`), `unittest`, existing venv packages (ollama, pypdf) for model calls only, plain JavaScript for the page.

**Spec:** `docs/superpowers/specs/2026-11-05-streaming-pipeline-design.md` (read its section "Calls settled after the builder check" first; it overrides the sections above it). Also read `SPEC.md`.

## Global Constraints

- Python 3 standard library only, unless the owner approves a dependency. The venv's ollama and pypdf are already approved; use them only in the model steps.
- Paths relative to the project root; scripts find the root from `Path(__file__).resolve().parent.parent` and work from any current directory.
- Writes to `jobs.json` are atomic (temp file then `os.replace`), keep a `jobs.json.bak` first, and never overwrite an existing job's data or touch `user_status`.
- A corrupt JSON file raises an error, never treated as empty.
- Scripts print one line of JSON to stdout and exit non-zero on failure; progress goes to stderr.
- Never edit or delete files in `runs/` by hand. The worker may write its own run files; tests use temporary directories only, never real data. Tests use a fake Ollama client.
- Do not change the scraper prompt or run the scraper. Do not commit `resume/`, `jobs.json`, `runs/`, `queue/`.
- No em dashes in any text the owner may copy into a job portal.
- Existing tests must keep passing: `python3 -m unittest discover -s tests` (use `.venv/bin/python` where a test imports ollama or pypdf).

## Review Focus

Failure modes the spec implies that no single task's happy path covers (each has a test in the task that owns the code):

1. A job whose description holds quotes, backslashes, newlines, unicode and is very long, passed through stdin (Task 3).
2. Two `enqueue.py` calls for the same id at the same moment: exactly one is queued (Task 3).
3. The worker killed mid-job: the job comes back from `processing/`, and the run files and `jobs.json` end up with one entry, not two (Tasks 2, 4, 5).
4. A closed job or a job with no description: no model call, still lands in `jobs.json` with its stage (Task 4).
5. Ollama not reachable or a model missing when the worker starts: exit non-zero before claiming any job, queue untouched (Task 5).

---

### Task 1: Cross-process lock for `jobs.json`

**Files:**
- Create: `scripts/jobslock.py`, `tests/test_jobslock.py`
- Modify: `scripts/merge.py` (`merge_run`), `scripts/serve.py` (`JobsStore.set_status`), `tests/test_merge.py`, `tests/test_serve.py`

**Interfaces:**
- Produces: `jobslock.jobs_lock(root) -> contextmanager` (blocking `fcntl.flock` exclusive lock on `<root>/.jobs.lock`, file created if missing, released on exit even on exception). Not reentrant: never nest.

- [ ] **Step 1: Write failing tests** in `tests/test_jobslock.py`: `test_lock_excludes_second_process` (a subprocess holds the lock for 0.5 s; the parent's `jobs_lock` call returns only after it ends; measure with `time.monotonic`), `test_lock_released_after_exception`, `test_lock_file_created_in_root`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_jobslock -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `scripts/jobslock.py` as specified above.
- [ ] **Step 4: Wire it in.** `merge.merge_run`: take the lock around "load `jobs.json` through write" (the read must happen inside the lock). `JobsStore.set_status`: take the lock around its fresh read, `.bak` copy and write, in addition to its in-process lock. Add `test_merge_waits_for_lock` in `tests/test_merge.py` (hold the lock in a thread, assert merge only completes after release) and `test_set_status_waits_for_lock` in `tests/test_serve.py`.
- [ ] **Step 5: Run** `python3 -m unittest discover -s tests`. Expected: all pass.
- [ ] **Step 6: Commit** `git add scripts/jobslock.py scripts/merge.py scripts/serve.py tests && git commit -m "Cross-process lock for jobs.json writers"` (end with the attribution line from the session's reminder).

---

### Task 2: Queue and run state module

**Files:**
- Create: `scripts/jobqueue.py` (not `queue.py`, which would shadow the standard library), `tests/test_jobqueue.py`

**Interfaces:**
- Produces (all take `root` first; all raise `QueueError` on a corrupt `run.json` or job file, never treat it as empty):
  - `QueueError(Exception)`
  - `read_run(root) -> dict | None` (`{"date": "YYYY-MM-DD", "stopping": bool}` from `queue/run.json`)
  - `begin_run(root, today: str | None = None) -> dict` (returns the existing run, or creates one with `today` or `date.today().isoformat()`)
  - `set_stopping(root) -> dict` (begins a run for today if none, sets `stopping` true, returns it)
  - `end_run(root) -> None` (removes `run.json`)
  - `put(root, job_id: str, job: dict) -> None` (writes a temp file in `queue/` then renames into `queue/pending/<id>.json`)
  - `state_of(root, job_id) -> str | None` (`"pending"`, `"processing"`, `"failed"` or None)
  - `claim_next(root) -> tuple[str, dict] | None` (oldest by mtime then name; moves to `processing/`)
  - `complete(root, job_id) -> None` (deletes from `processing/`)
  - `fail(root, job_id, error: str) -> None` (moves to `failed/`, file holds `{"job": ..., "error": ...}`)
  - `requeue_failed(root, job_id) -> bool` (moves the stored job back to `pending/` as a plain job file)
  - `recover(root) -> int` (moves everything in `processing/` back to `pending/`, returns the count)
  - `counts(root) -> {"pending": n, "processing": n, "failed": n}`
- Directories under `<root>/queue/` are created on first use.

- [ ] **Step 1: Write failing tests** in `tests/test_jobqueue.py`, one per behavior: `test_put_and_claim_is_fifo`, `test_claim_moves_to_processing_and_complete_deletes`, `test_empty_claim_returns_none`, `test_recover_returns_processing_to_pending`, `test_fail_then_requeue_failed_roundtrip` (job content identical after the round trip), `test_state_of_each_state`, `test_begin_run_is_idempotent_and_keeps_date` (second call with another `today` keeps the first date), `test_set_stopping_without_run_creates_one`, `test_end_run_removes_file`, `test_corrupt_run_json_raises`, `test_put_leaves_no_partial_file` (no `.tmp` left in `pending/`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_jobqueue -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `scripts/jobqueue.py` per the interfaces. `run.json` is written with `clean.write_json_atomic`.
- [ ] **Step 4: Run** the new tests, then the full suite. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "Job queue and run state"`.

---

### Task 3: `enqueue.py` and `stoppipeline.py`

**Files:**
- Create: `scripts/enqueue.py`, `scripts/stoppipeline.py`, `tests/test_enqueue.py`

**Interfaces:**
- Consumes: `jobqueue.*` (Task 2), `jobslock.jobs_lock` (Task 1), `clean.validate(job) -> (id, reason)`, `clean.write_json_atomic`.
- Produces:
  - `enqueue.EnqueueError(Exception)`
  - `enqueue.enqueue(root, job: dict, today: str | None = None) -> dict` returning `{"status": "queued" | "duplicate" | "requeued", "id": str, "date": str, "pending": int}`
  - `stoppipeline.stop(root) -> dict` returning `{"status": "stopping", "date": str, "pending": int, "processing": int}`
- CLI: `enqueue.py '<json>'` or `enqueue.py -` (JSON on stdin); `stoppipeline.py`. Each prints one JSON line; failure prints `{"status": "error", "error": "..."}` and exits 1.

Behavior of `enqueue` (everything under `jobs_lock(root)`): invalid JSON or `clean.validate` reason raises `EnqueueError` (nothing written); `jobqueue.begin_run` (so the first enqueue creates the run); if the run is `stopping`, raise `EnqueueError("run is stopping")`; read `jobs.json` (missing is an empty list, corrupt raises `EnqueueError`); if the id is in `jobs.json` or `state_of` is pending or processing, return `duplicate`; if `state_of` is failed, `requeue_failed` and return `requeued`; otherwise append the job (id normalized to the string from `validate`) to `runs/<run date>/raw.json` unless an entry with that id is already there (creating the run dir and file; a corrupt `raw.json` raises), then `jobqueue.put`, return `queued`. The raw append comes before `put` so a crash between them is repaired by calling enqueue again.

- [ ] **Step 1: Write failing tests** in `tests/test_enqueue.py` (temporary root): `test_queued_job_in_raw_and_pending`, `test_duplicate_in_jobs_json`, `test_duplicate_in_queue`, `test_failed_job_is_requeued`, `test_invalid_json_exits_nonzero_and_writes_nothing` (via subprocess), `test_missing_required_field`, `test_refused_when_stopping`, `test_corrupt_jobs_json_is_an_error`, `test_corrupt_raw_json_is_an_error`, `test_raw_not_duplicated_on_retry_after_crash` (pre-seed raw.json with the id but nothing in the queue: result `queued`, raw still one entry), `test_stdin_job_with_quotes_newlines_unicode_and_100kb_description` (subprocess, heredoc-style stdin, round-trips byte-identical), `test_concurrent_same_id_queues_once` (4 subprocesses at once; exactly one `queued`, rest `duplicate`), `test_stop_marks_run_and_reports_pending`, `test_stop_without_run_creates_one`, `test_run_date_comes_from_run_json_not_clock` (run.json date `2026-01-01`: raw lands in `runs/2026-01-01/`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_enqueue -v`. Expected: FAIL.
- [ ] **Step 3: Implement** both scripts per the behavior above. Both resolve ROOT from their own location; `main(argv)` functions accept a `root` override only through the importable functions, tests of the CLI copy the scripts into a temp root (as `tests/test_merge.py` does for `merge.py`).
- [ ] **Step 4: Run** the new tests and the full suite. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "enqueue.py and stoppipeline.py"`.

---

### Task 4: Per-job processing

**Files:**
- Create: `scripts/pipeline_job.py`, `tests/test_pipeline_job.py`
- Modify: `scripts/merge.py` (add `merge_one`), `scripts/filter.py` (factor out report building, add `rebuild_report`), `tests/test_merge.py`, `tests/test_filter.py`

**Interfaces:**
- Consumes: `clean.validate`, `clean.clean_job`, `enrich.enrich_one`, `enrich.load_prompt`, `jd_flags.detect`, `filter.evaluate`, `merge.build_record`, `merge.fill_missing`, `jobslock.jobs_lock`.
- Produces:
  - `merge.merge_one(root, run_date, job: dict, filter_record: dict | None) -> str` returning `"added"`, `"filled"` or `"unchanged"`. Under `jobs_lock`: load `jobs.json` (missing is an empty list, corrupt raises `MergeError`), `build_record`, add or `fill_missing`, copy `.bak` then `write_json_atomic` only when something changed.
  - `filter.build_report(records: list, rules: dict, model: str) -> tuple[dict, dict]` returning `(counts, report)` exactly as `filter.run` writes them today; `filter.run` calls it.
  - `filter.rebuild_report(root, run_date, model=DEFAULT_MODEL) -> dict` reads `filtered.json` and `config.json` rules and writes `filter_report.json` (same content as the batch run would), returns the counts.
  - `pipeline_job.PipelineError(Exception)`
  - `pipeline_job.JobPipeline(root, run_date, client, models: list[str], prompt, resume_text: str, rules: dict, filter_model: str = filter.DEFAULT_MODEL, num_ctx: int = 8192, retries: int = 1, think: bool = False)`
  - `JobPipeline.process(raw_job: dict) -> dict` returning `{"id": str, "stage": str, "merge": str}`

`process` does: `clean.validate` (reason raises `PipelineError`) and `clean_job`; upsert the cleaned job into `runs/<date>/clean.json`; for an open job with a description, add `jd_flags` and, for each model whose entry in the existing `enriched.json` record is not ok for this prompt version, call `enrich_one` (existing ok entries are kept, so a retry after a crash does not redo finished models); upsert the result into `enriched.json`; if the job is open with a description and no model returned ok, raise `PipelineError` (the worker retries, then fails the job); `filter.evaluate` and upsert into `filtered.json`; `merge.merge_one`. Upsert means: read the list (missing file is an empty list, corrupt raises), replace the entry with the same id or append, `write_json_atomic`. Closed jobs and jobs without a description make no model call and get the stage `filter.evaluate` gives them.

- [ ] **Step 1: Write failing tests.** `tests/test_merge.py`: `test_merge_one_adds_then_unchanged`, `test_merge_one_fills_missing_model_only`, `test_merge_one_keeps_user_status_and_run_date`, `test_merge_one_corrupt_jobs_json_raises_and_writes_nothing`, `test_merge_one_writes_bak_before_change`. `tests/test_filter.py`: `test_rebuild_report_matches_batch_report`. `tests/test_pipeline_job.py` with a fake client and a fake `enrich_one` (monkeypatch `pipeline_job.enrich_one`): `test_open_job_runs_every_model_and_lands_in_jobs_json`, `test_files_updated_without_duplicates_when_processed_twice` (process the same job twice; one entry per file and in `jobs.json`), `test_retry_skips_models_already_ok` (first model ok, second raises; second `process` calls only the second model), `test_closed_job_makes_no_model_call_and_lands_closed`, `test_job_without_description_makes_no_model_call`, `test_all_models_failed_raises_pipeline_error`, `test_rejected_job_is_in_jobs_json_with_stage_rejected`, `test_invalid_job_raises`.
- [ ] **Step 2: Run** the three test files. Expected: FAIL.
- [ ] **Step 3: Implement** `merge.merge_one`, refactor `filter.py` (batch output must stay byte-identical; existing filter tests prove it), then `pipeline_job.py`.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "Per-job processing: clean, enrich, filter, merge"`.

---

### Task 5: The worker

**Files:**
- Create: `scripts/worker.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `jobqueue.*`, `JobPipeline` (Task 4), `filter.rebuild_report`, `clean.clean_all`, `clean.write_json_atomic`, `boost.run(root, run_date, ..., client=, resume_text=, unload=, log=)`, `merge.merge_run(root, run_date)`, `llm.check_models`, `llm.unload_model`, `enrich.load_resume_text`, `enrich.resolve_models`, `enrich.load_prompt`.
- Produces: `worker.run_worker(root, client=None, resume_text=None, boost_run=boost.run, poll_seconds: float = 2.0, sleep=time.sleep, log=lambda m: None) -> dict` returning `{"date", "processed", "failed", "end_steps": "ok" | "failed" | "skipped", "error"?}`; `worker.main(argv)` prints that dict as one JSON line and exits 1 if any job failed or the end steps failed. Run with `.venv/bin/python scripts/worker.py`.

Behavior: preflight first and fail before touching the queue (config, models via `check_models`, resume text, prompt), so a missing model or an unreachable Ollama exits non-zero with the queue untouched. Then `jobqueue.begin_run`, `jobqueue.recover`, and loop: read `stopping` first, then `claim_next`; if a job was claimed process it with up to 2 attempts (`PipelineError` or any exception from a model call), `complete` on success, `fail` with the error text after the second failure; if none was claimed and `stopping` was true at the read, leave the loop; otherwise `sleep(poll_seconds)`. End steps (only when `runs/<date>/filtered.json` exists): `filter.rebuild_report`, rewrite `clean_report.json` from `clean_all(raw.json)`'s report, `boost_run(...)`, `merge.merge_run`, then `jobqueue.end_run`. If an end step raises, keep `run.json`, return `end_steps: "failed"` with the error, and do not call `end_run`. With no jobs processed at all, the end steps are `"skipped"` and `end_run` is still called. Models are unloaded in a `finally`.

- [ ] **Step 1: Write failing tests** in `tests/test_worker.py` (temporary root, fake client, fake `enrich_one`, fake `boost_run` recording calls; `sleep` is a stub that flips `stopping` after N calls): `test_processes_queued_jobs_then_runs_end_steps_after_stop`, `test_end_steps_wait_for_stop` (no boost call before `stopping`), `test_job_enqueued_before_stop_is_never_dropped` (stopping set while the last job is in `pending`), `test_crash_recovery_job_in_processing_is_done_once` (pre-seed `processing/`; one entry in each run file and in `jobs.json`), `test_job_failing_twice_goes_to_failed_and_others_continue`, `test_preflight_failure_leaves_queue_untouched` (client whose `check_models` raises; `pending/` unchanged, exception or non-zero result), `test_end_step_failure_keeps_run_json_and_second_start_retries_end_steps_only`, `test_success_removes_run_json`, `test_stop_with_empty_queue_and_no_jobs_skips_end_steps`, `test_models_unloaded_on_exit`.
- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_worker -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `scripts/worker.py` per the behavior above.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "Worker: drain the queue, then boost and final merge"`.

---

### Task 6: One start command

**Files:**
- Create: `scripts/start.py`, `tests/test_start.py`

**Interfaces:**
- Produces: `start.ollama_up(host: str = "127.0.0.1", port: int = 11434, timeout: float = 1.0) -> bool` (GET `/api/tags` succeeds), `start.port_in_use(port: int) -> bool`, `start.main(argv, spawn=subprocess.Popen, wait=time.sleep)`. CLI: `python3 scripts/start.py [--port 8765]`.

Behavior (the design's "Start command" section): if Ollama is not up, spawn `ollama serve` and poll `ollama_up` until ready (give up after about 30 s with a clear error and stop what it started); `jobqueue.begin_run`; spawn the worker with `.venv/bin/python scripts/worker.py` (error if `.venv` is missing); spawn `scripts/serve.py --port N` unless `port_in_use`; print one status line of JSON per component (`{"component": "ollama", "status": "started" | "already running"}` and so on); stay in the foreground until Ctrl-C, then terminate only the processes it started. When the worker exits (after a stop) print its summary line, and keep the web page and Ollama running until Ctrl-C. A worker exit code other than 0 is printed as such, not hidden.

- [ ] **Step 1: Write failing tests:** `test_ollama_up_true_against_local_stub_server`, `test_ollama_up_false_when_nothing_listens`, `test_port_in_use_true_and_false`, `test_main_skips_ollama_and_page_already_running` (injected spawner records only the worker), `test_main_starts_ollama_when_down` (spawner called with `["ollama", "serve"]`), `test_main_terminates_only_what_it_started_on_interrupt`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_start -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `scripts/start.py`.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "start.py: Ollama, worker and page in one command"`.

---

### Task 7: Live page

**Files:**
- Modify: `web/app.js`, `tests/test_web_files.py`

**Interfaces:**
- Consumes: existing `refresh()` and `loadMain()` in `web/app.js`.

Behavior: poll every 5 seconds, only while `document.visibilityState === "visible"` and no refresh is in flight; a poll re-fetches the nav view and, when the selected job is still present, leaves the selection, the right-hand pane and the scroll position alone (re-render the pane only if that job's record changed); new cards get a short "new" marker. A failing poll shows nothing new and retries on the next tick; it never blanks the page. No new files or network calls beyond the page's own `/api/*`.

- [ ] **Step 1: Write failing test** in `tests/test_web_files.py`: `test_app_js_polls_only_when_visible` (asserts `setInterval` and `visibilityState` appear in `web/app.js`; this is a smoke check, the behavior is verified in Step 4).
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement** the polling in `web/app.js`, keeping the existing plain-JS style (`textContent` only, no dependencies).
- [ ] **Step 4: Verify in the real page** with the `run` skill, using a temporary copy of the project root (not real data): start `serve.py` against it, then run `enqueue.py` plus a worker with a fake model or write a record into the temporary `jobs.json`, and confirm the new card appears without reload and the selected job and scroll position survive.
- [ ] **Step 5: Run** the full suite, then commit `git commit -m "Jobs page polls for new jobs"`.

---

### Task 8: Docs, ignore rules and end-to-end check

**Files:**
- Modify: `.gitignore` (add `queue/` and `.jobs.lock`), `SPEC.md`, `CLAUDE.md`
- Create: `tests/test_end_to_end.py`

**Interfaces:**
- Consumes: everything above. `docs/scraper-prompt.md` already holds the text for the Cowork prompt; link to it, do not edit the scraper.

- [ ] **Step 1: Write the end-to-end test** `test_enqueue_worker_stop_roundtrip` in a temporary root with a fake client, fake `enrich_one` and fake `boost_run`: enqueue 3 jobs (one closed, one that the filter rejects), start `run_worker` in a thread, call `stoppipeline.stop`, join; assert `jobs.json` has 3 records with the right stages, `raw.json`, `clean.json`, `enriched.json`, `filtered.json` have 3 each, `filter_report.json` exists, boost and `merge_run` ran once after the last job, `run.json` is gone, and a `user_status` set on one job through `JobsStore.set_status` during the run survived.
- [ ] **Step 2: Run** it. Expected: FAIL until docs-independent wiring is correct; fix any wiring bug it reveals in the owning module with its own test.
- [ ] **Step 3: Docs.** `SPEC.md`: add sections for the queue layout and `run.json`, `enqueue.py`, `stoppipeline.py`, the worker and its end-of-run steps, `start.py`, the file lock, and page polling. `CLAUDE.md`: bring "How it works", "Layout" and "Structure" in line with SPEC.md (the real stage order, `jobs.json` is a list, no `apply_filter.py`) and list the new scripts and the queue; keep every existing rule.
- [ ] **Step 4: Run** the full suite: `.venv/bin/python -m unittest discover -s tests`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Docs, ignore rules and end-to-end test for the streaming pipeline"`.

# Jobs Merge and Jobs Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `scripts/merge.py` puts a run's jobs and rollup into `jobs.json` and `rollups.json`; `scripts/serve.py` serves a page (`web/`) that shows them as in the approved mockup.

**Architecture:** `merge.py` is a plain file merge. `scripts/jobs_query.py` holds every filter, sort and graph series as pure functions. `scripts/serve.py` is a thin standard library HTTP layer over those functions and one write (`user_status`). `web/` is vanilla HTML, CSS and JavaScript that only renders what `/api/view` returns.

**Tech Stack:** Python 3.12 standard library (the venv is not needed), `unittest`, plain JavaScript, inline SVG.

**Spec:** `docs/superpowers/specs/2026-11-05-jobs-page-design.md` (mockup: `docs/superpowers/specs/assets/jobs-page-mockup-v2.html`)

## Global Constraints

- Python standard library only; no JavaScript dependencies, no build step, no external network requests.
- Paths relative to the project root, found from the script's own location.
- Atomic writes (temp file then `os.replace`) and a `jobs.json.bak` before every rewrite of `jobs.json`.
- Never overwrite existing data in `jobs.json`; never touch `user_status` in the merge.
- A corrupt JSON input raises an error, never treated as empty. Scripts print one line of JSON to stdout and exit non-zero on failure.
- The server binds 127.0.0.1 only; it serves only `web/` files and the `/api/` routes; the only write is `user_status` of one job.
- Tests use temporary directories and fixtures, never real data.
- Commit messages carry no Co-Authored-By line (the author wants full credit).
- `jobs.json`, `jobs.json.bak`, `rollups.json` and `runs/` stay out of git (add `rollups.json` to `.gitignore`).

## Review Focus

1. A date range with no jobs: the view says so and carries `latest_run_date` so the page can offer to jump there.
2. Corrupt `jobs.json`: merge exits non-zero without writing, the server refuses to start and answers requests with a 500 and a message, never an empty list.
3. Status writes: an unknown id, a bad status value, a wrong `Host`, a non-JSON body each get a 4xx and leave the file byte for byte unchanged; a good write changes only that job's `user_status`.
4. A static path with `..` or an absolute path never reads outside `web/`.
5. Merging the same run twice changes nothing; a boost plan that arrives a day later is added to the existing job without changing any other field or `user_status`.

---

### Task 1: Merge (`scripts/merge.py`)

**Files:**
- Create: `scripts/merge.py`, `tests/test_merge.py`
- Modify: `.gitignore` (add `rollups.json`)

**Interfaces:**
- Produces:
  - `build_record(job: dict, filter_record: dict | None, boost_record: dict | None, run_date: str) -> dict`: the enriched job plus `filter` (`{stage, reasons, years_required, years_model}` or null), `boost` (or null), `stage` (filter stage, else `"enriched"`), `run_date`, `user_status: None`
  - `fill_missing(existing: dict, new: dict) -> bool`: copies into `existing` every key of `new` that is absent or None in `existing`, merges `enrichment` per model key (never replacing an existing model), never touches `user_status` or `run_date`; returns whether anything changed
  - `merge_run(root, run_date: str) -> dict` returning `{"date", "jobs_in_run", "added", "filled", "unchanged", "rollup": "added"|"kept"|"none"}`; `main(argv)` with an optional date argument
- Reads `runs/<date>/enriched.json`, `filtered.json`, `boost/<id>.json` (use only records with `ok` true), `boost_rollup.json` (only if `ok`). Missing `enriched.json` or `filtered.json` or any corrupt file raises `MergeError`; nothing is written.
- Writes `jobs.json` (list; `jobs.json.bak` first, only when `jobs.json` exists) and `rollups.json` (object keyed by run date, an existing date is kept).

- [ ] **Step 1:** Write tests: `test_new_run_adds_all_jobs_with_sections`, `test_rejected_jobs_included_with_reasons`, `test_second_merge_changes_nothing` (bytes equal, `unchanged == jobs_in_run`), `test_boost_arriving_later_fills_only_boost` (existing fields and `user_status: "applied"` preserved), `test_existing_enrichment_model_not_replaced_new_model_added`, `test_bak_written_only_when_jobs_json_exists`, `test_rollup_added_once_and_kept` (Review Focus 5), `test_failed_boost_record_is_ignored`, `test_corrupt_or_missing_inputs_write_nothing` (Review Focus 2), `test_cli_prints_one_json_line`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_merge -v`; expect ImportError.
- [ ] **Step 3:** Implement `scripts/merge.py` (reuse `clean.write_json_atomic`).
- [ ] **Step 4:** Run the tests; expect PASS, then the whole suite.
- [ ] **Step 5:** Commit `feat: merge a run into jobs.json`.

### Task 2: View model (`scripts/jobs_query.py`)

**Files:**
- Create: `scripts/jobs_query.py`, `tests/test_jobs_query.py`

**Interfaces:**
- Produces:
  - `class JobsError(Exception)`; `load_jobs(path) -> list[dict]` (missing file gives `[]`; corrupt raises `JobsError`)
  - `models_in(jobs) -> list[str]` (sorted keys of every `enrichment`)
  - `job_score(job, model: str) -> int | None` (`model == "average"` gives the rounded mean of the ok models)
  - `is_flagged(job) -> bool` (any of `jd_flags.sponsorship is False`, `clearance`, `us_citizenship_required` true, or `review` true)
  - `card(job, models) -> dict` with `id, title, company, location, work_model, scraped_at, run_date, stage, scores ({model: int | None}), boost (bool), user_status, badges ({models_differ: bool (max minus min >= 25), no_apply_url: bool (apply_type external and apply_url null), flagged: bool})`
  - `query(jobs, range="today", from_date=None, today=None, sort="score", show="all", status="any", model="qwen3.5:9b", q="") -> dict` with `cards`, `series` ({model: [scores in card order, None skipped]}), `averages` ({model: float | None}), `models`, `counts` ({all, passed, rejected, flagged, boost}, taken after the time frame and the status and search filters, before `show`), `range` ({from, to}), `latest_run_date`, `empty` (bool)
- Rules: `range` today/yesterday use `run_date == today` / `today - 1 day` (`today` is an ISO string, default the current date); `from` uses `run_date >= from_date`. `show`: `passed`/`rejected` by `stage`, `flagged` by `is_flagged`, `boost` by a boost record. `status`: `new` is `user_status is None`. `q`: case-insensitive substring over title, company and the skill names in all models. `sort=score` by `job_score(model)` desc (None last), ties by `score_breakdown.required_skills` of that model desc then id; `sort=recent` by `scraped_at` desc then id.

- [ ] **Step 1:** Write tests: one per rule above, including `test_today_with_no_jobs_is_empty_and_reports_latest_run_date` (Review Focus 1), `test_series_follow_card_order_and_skip_missing_scores`, `test_counts_ignore_show_but_respect_range`, `test_average_model_option`, `test_search_matches_skill_names`, `test_corrupt_file_raises` , `test_missing_file_is_empty_list`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_jobs_query -v`; expect ImportError.
- [ ] **Step 3:** Implement the functions in `scripts/jobs_query.py`.
- [ ] **Step 4:** Run the tests; expect PASS.
- [ ] **Step 5:** Commit `feat: jobs view model`.

### Task 3: Server (`scripts/serve.py`)

**Files:**
- Create: `scripts/serve.py`, `tests/test_serve.py`

**Interfaces:**
- Consumes: `jobs_query.load_jobs/query/JobsError`, `clean.write_json_atomic`.
- Produces:
  - `class JobsStore(root)` with `jobs() -> list[dict]` (re-reads the file when its mtime changed; raises `JobsError`), `get(job_id) -> dict | None`, `set_status(job_id, status: str | None) -> dict` (valid values `"applied"`, `"skipped"`, `None`; raises `KeyError` for an unknown id, `ValueError` for a bad value; rewrites `jobs.json` atomically with a `.bak`, under a lock, changing only that job's `user_status`), `rollup(date) -> dict | None` (from `rollups.json`)
  - `create_server(root, port: int = 8765) -> http.server.ThreadingHTTPServer` bound to `127.0.0.1`; `main(argv)` with `--port`; on a corrupt `jobs.json` it exits non-zero before listening
- Routes per the spec: `GET /` (`web/index.html`), `GET /static/<file>` (only inside `web/`, content types for html/css/js/svg), `GET /api/view?...`, `GET /api/job/<id>`, `GET /api/rollup/<date>`, `POST /api/job/<id>/status`. Errors are JSON `{"error": "..."}` with 400, 403 (bad Host), 404, 415 (not JSON), 500 (`JobsError`, message included).

- [ ] **Step 1:** Write tests that start the server on port 0 in a thread against a temporary root: `test_index_and_static_served`, `test_static_path_traversal_blocked` (Review Focus 4: `/static/../jobs.json`, `/static/%2e%2e/jobs.json`, absolute paths all 404), `test_view_returns_cards_series_counts`, `test_job_and_rollup_routes`, `test_status_post_changes_only_that_job` (compare every other byte of the parsed file), `test_status_rejects_bad_value_unknown_id_wrong_host_non_json_and_leaves_file_unchanged` (Review Focus 3), `test_status_can_be_cleared_with_null`, `test_corrupt_jobs_json_gives_500_with_message_not_empty_list` (Review Focus 2), `test_server_binds_loopback_only`, `test_status_survives_restart`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_serve -v`; expect ImportError.
- [ ] **Step 3:** Implement with `http.server`; keep route handling in small functions.
- [ ] **Step 4:** Run the tests; expect PASS, then the whole suite.
- [ ] **Step 5:** Commit `feat: local jobs server`.

### Task 4: Page (`web/index.html`, `web/style.css`, `web/app.js`)

**Files:**
- Create: `web/index.html`, `web/style.css`, `web/app.js`; `tests/test_web_files.py`

**Interfaces:**
- Consumes the Task 3 API. Layout, copy and the Sage palette from the mockup; the font sizes of mockup v2 (base 15px). Element ids the test checks: `topbar`, `charts`, `ctl-range`, `ctl-from`, `ctl-sort`, `ctl-show`, `ctl-model`, `ctl-status`, `ctl-search`, `nav`, `main`.
- `app.js` behaviors: load `/api/view` with the current controls; draw one inline SVG polyline per model with the dashed average line; render cards; select a card to load `/api/job/<id>` and render every section in the spec's order (rejected jobs show their reasons with evidence; the boost section shows clean rewordings, then risky ones under their heading with their problems; copy buttons for resume bullets and spec text; applied/skipped buttons POST the status); the pinned "Today's rollup" card loads `/api/rollup/<date>`; empty state offers "show latest run"; up/down and j/k move between jobs; controls are saved to `localStorage` inside try/catch. All text from the data is inserted with `textContent`, never `innerHTML`.

- [ ] **Step 1:** Write `tests/test_web_files.py`: `test_page_has_the_required_element_ids`, `test_page_loads_only_local_assets` (no `http://` or `https://` in html, css, js except in `href` values built from data), `test_no_innerhtml_with_data` (the string `innerHTML` does not appear in `app.js`).
- [ ] **Step 2:** Run it; expect FAIL (files missing).
- [ ] **Step 3:** Write the three files.
- [ ] **Step 4:** Run the tests; expect PASS.
- [ ] **Step 5:** Run `python3 scripts/merge.py 2026-11-04` and `python3 scripts/serve.py` against the real data, open `http://localhost:8765` with the browser tools, check each section against the mockup (screenshots), switch every control, mark one job applied and then clear it again. Fix what differs.
- [ ] **Step 6:** Commit `feat: jobs page`.

### Task 5: Document

**Files:**
- Modify: `SPEC.md`

- [ ] **Step 1:** Add a merge section (usage, record shape, never-overwrite rule, rollups.json) and a page section (usage, routes, filters, what is written) to `SPEC.md`.
- [ ] **Step 2:** Run the whole suite; commit `docs: merge and page`.

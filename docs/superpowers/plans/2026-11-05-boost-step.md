# Boost Step Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `scripts/boost.py`: for the best 5 filtered jobs of the day, produce per-job improvement plans (rewordings, project extensions, new project with spec) and a daily rollup of missing skills with one general project.

**Architecture:** Pure functions in `scripts/boost_data.py` (select, gaps, tally, checks, sanitizing) are tested without a model. `scripts/llm.py` holds the Ollama helpers shared with `scripts/enrich.py` (moved out, behavior unchanged). `prompts/boost/v1.py` holds prompts and pydantic models. `scripts/boost.py` orchestrates and `scripts/boost_report.py` renders `boost.md`.

**Tech Stack:** Python 3.12 in `.venv`, `ollama`, `pypdf`, `pydantic` (already installed), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-05-boost-step-design.md`

## Global Constraints

- Python standard library plus the already approved `ollama`, `pypdf` (and `pydantic`, which ollama installs).
- Paths relative to the project root, found from the script's own location.
- Writes are atomic (`clean.write_json_atomic`). Never write to `config.json`, `jobs.json`, `resume/`, `enriched.json`, `filtered.json`.
- A corrupt JSON input raises an error, never treated as empty. Scripts print one line of JSON to stdout and exit non-zero on failure.
- Tests use temporary directories and a fake Ollama client, never real data.
- No `format=` argument to Ollama (the MLX runner returns 501): ask for JSON in the prompt, parse, validate, one repair turn.
- No em dashes (U+2014) in anything the model wrote; replace them before writing files.
- One model in memory at a time: unload the model when finished (`keep_alive=0`).
- Commit messages carry no Co-Authored-By line (the author wants full credit).

## Review Focus

1. Fewer passed jobs than `top_n`, or none: no crash, the report says so.
2. A passed job whose qwen3.5:9b enrichment failed: ranked by the next model's score and marked; if every model failed it is excluded and reported.
3. The model returns invalid JSON twice for one job: that job is recorded `ok: false`, the others continue, exit is non-zero, and the next run retries only the failed job.
4. Em dashes in model output are replaced before any file is written.
5. A rewording whose quoted text is not in the resume, or a project not in `projects.md`, is kept but flagged and listed as a warning in the report.

---

### Task 1: Move shared Ollama helpers into `scripts/llm.py`

**Files:**
- Create: `scripts/llm.py`, `tests/test_llm.py`
- Modify: `scripts/enrich.py` (import from `llm`, delete the moved code)

**Interfaces:**
- Produces in `llm.py`:
  - `class LLMError(Exception)`
  - `extract_json(text: str) -> str`
  - `call_model(client, model: str, messages: list[dict], num_ctx: int, think: bool) -> str` (no `format`, retries once without `think` if the error mentions it)
  - `generate_json(client, model, messages, parse: Callable[[str], T], num_ctx: int, think: bool, retries: int) -> tuple[T | None, str | None, int]` returning `(value, error, attempts)`; `parse` receives the raw reply and raises on invalid; on failure it appends the assistant reply and a "That reply was not valid. Problem: ... Return the corrected JSON object only." turn
  - `generate_text(client, model, messages, num_ctx, think) -> str`
  - `unload_model(client, model) -> None` (errors ignored), `check_models(client, models: list[str]) -> None` (raises `LLMError`)
  - `load_prompt(version: str, root, area: str)` loads `<root>/prompts/<area>/<version>.py`, version must match `v\d+`, raises `LLMError`
- `enrich.py` keeps the names `extract_json`, `call_model`, `unload_model`, `check_models`, `load_prompt(version, root=ROOT)`, `EnrichError` (alias of `LLMError`) so `tests/test_enrich.py` passes unchanged. `enrich_one` uses `generate_json`.

- [ ] **Step 1:** Write `tests/test_llm.py` with `test_generate_json_repairs_once` (fake client replies garbage then valid JSON, expect `(value, None, 2)` and the second call's last message containing "not valid"), `test_generate_json_gives_up` (expect `(None, error, retries + 1)`), `test_unload_ignores_errors`, `test_load_prompt_area_and_version_checks`.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_llm -v`; expect ImportError.
- [ ] **Step 3:** Create `scripts/llm.py` by moving the code from `enrich.py` and adding `generate_json`, `generate_text`, `load_prompt(area)`. Edit `enrich.py` to import and use it.
- [ ] **Step 4:** Run `.venv/bin/python -m unittest discover -s tests`; expect all existing tests plus the new ones to pass.
- [ ] **Step 5:** Commit `refactor: share ollama helpers in llm.py`.

### Task 2: Select jobs and consolidate gaps (`scripts/boost_data.py`)

**Files:**
- Create: `scripts/boost_data.py`, `tests/test_boost_data.py`

**Interfaces:**
- Produces:
  - `description_hash(text: str) -> str` (sha1 of lowercased, whitespace-collapsed text)
  - `passed_jobs(enriched: list[dict], filtered: list[dict]) -> list[dict]`: jobs whose filtered stage is `"passed"`, in enriched order, duplicates (same description hash) removed keeping the first
  - `select_top(jobs: list[dict], model: str, top_n: int) -> list[dict]` returning `{"job", "rank", "score", "score_model", "fallback"}` sorted by score desc, tie-break `score_breakdown.required_skills` desc then id asc; a job with no ok result for `model` uses the first other model's score with `fallback=True`; a job with no ok result at all is left out
  - `skill_key(name: str) -> str`: lowercase, parenthesised parts removed, punctuation and whitespace collapsed
  - `consolidate_gaps(job: dict) -> list[dict]` with keys `skill` (display name of the first model's wording), `kind` (`"required"` or `"preferred"`), `status` (`"no"` if any model said no, else `"similar"`), `flagged_by` (sorted model names that flagged it), `models_total` (count of ok models), `learnable_votes` (models that said `learnable_in_a_day` true), `single_model` (bool: `len(flagged_by) == 1 and models_total > 1`)

- [ ] **Step 1:** Tests: `test_passed_jobs_dedupes_identical_descriptions`, `test_select_top_orders_by_9b_score_with_tiebreaks`, `test_select_top_falls_back_to_other_model_and_marks_it` (assert `fallback is True`), `test_select_top_skips_jobs_with_no_ok_result`, `test_select_top_fewer_than_top_n_returns_all`, `test_select_top_empty_returns_empty`, `test_skill_key_normalizes` (`"AWS (any one)"` and `" aws "` give the same key), `test_consolidate_gaps_merges_models_and_flags_single_model`.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_boost_data -v`; expect failures.
- [ ] **Step 3:** Implement the functions above in `scripts/boost_data.py`.
- [ ] **Step 4:** Run the same command; expect PASS.
- [ ] **Step 5:** Commit `feat: boost job selection and gap consolidation`.

### Task 3: Tally, checks and sanitizing (`scripts/boost_data.py`)

**Files:**
- Modify: `scripts/boost_data.py`, `tests/test_boost_data.py`

**Interfaces:**
- Consumes: `consolidate_gaps`, `skill_key` from Task 2.
- Produces:
  - `tally_gaps(jobs: list[dict]) -> list[dict]` with keys `skill` (most common display name), `jobs` (count), `job_ids` (list), `models_flagged_max` (int), `learnable_share` (float 0..1, learnable votes over flagged votes), `required_count`, `preferred_count`; sorted by `jobs` desc, then `skill_key` asc
  - `parse_projects(projects_md: str) -> list[str]`: the names on a line starting `Projects:` separated by `|`; if absent, names from `## N. Name` headings with the parenthesis removed
  - `check_plan(plan: dict, resume_text: str, project_names: list[str]) -> tuple[dict, list[str]]`: returns a copy where each rewording has `current_text_found` (whitespace and case insensitive substring of the resume) and each extension has `project_known` (its `project` equals a project name case-insensitively), plus a list of warning strings
  - `strip_em_dashes(value)`: recursively replaces U+2014 with `" - "` in all strings of a dict/list/str

- [ ] **Step 1:** Tests: `test_tally_counts_jobs_per_skill_case_insensitively` (expect `AWS`, `aws` and `Aws (any one)` in 3 jobs give one row with `jobs == 3`), `test_tally_sort_order`, `test_parse_projects_from_projects_line`, `test_parse_projects_falls_back_to_headings`, `test_check_plan_flags_unknown_project_and_missing_quote` (warnings list names both), `test_check_plan_accepts_quote_with_different_whitespace`, `test_strip_em_dashes_nested` (Review Focus 4), `test_check_plan_does_not_drop_flagged_items` (Review Focus 5).
- [ ] **Step 2:** Run the module tests; expect failures.
- [ ] **Step 3:** Implement the four functions.
- [ ] **Step 4:** Run the module tests; expect PASS.
- [ ] **Step 5:** Commit `feat: boost tally, plan checks, sanitizing`.

### Task 4: Prompt v1 (`prompts/boost/v1.py`)

**Files:**
- Create: `prompts/boost/v1.py`, `prompts/boost/CHANGELOG.md`, `tests/test_boost_prompt.py`

**Interfaces:**
- Produces in `v1.py`: `VERSION = "v1"`; pydantic models `Rewording`, `Extension`, `NewProject`, `SkippedGap`, `Plan` (fields exactly as the spec's "Shape of a per-job plan"; `new_project: Optional[NewProject] = None`, all lists default to `[]`), `GeneralProject`, `Rollup` (`general_project: GeneralProject`; `GeneralProject.kind` is `"extension"` or `"new"`); strings `SYSTEM_PLAN`, `USER_TEMPLATE_PLAN` (placeholders `{resume} {projects} {title} {company} {description} {gaps} {output_format}`), `PLAN_FORMAT`, `SYSTEM_SPEC`, `USER_TEMPLATE_SPEC` (`{resume} {projects} {context} {outline}`), `SYSTEM_ROLLUP`, `USER_TEMPLATE_ROLLUP` (`{resume} {projects} {tally} {output_format}`), `ROLLUP_FORMAT`.
- The system prompts must state: use only facts in the resume and `projects.md`; rewordings quote `current_text` word for word from the resume; a suggested bullet for unbuilt work is labelled "write this only after you have built it"; extensions only for projects that exist and only where the skill fits the project's architecture; a new project only when extensions leave important gaps open; no invented numbers; no em dashes; gaps flagged by one model must be checked against the resume; effort in hours is an estimate, no size limit.

- [ ] **Step 1:** Tests: `test_plan_validates_minimal_reply` (only `verdict`), `test_plan_validates_full_reply`, `test_plan_rejects_missing_verdict`, `test_templates_format_without_keyerror` (format each template with dummy values), `test_system_prompts_contain_the_honesty_rules` (assert the phrases "write this only after you have built it" and "word for word" are present).
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_boost_prompt -v`; expect failures.
- [ ] **Step 3:** Write `prompts/boost/v1.py` and start `CHANGELOG.md`.
- [ ] **Step 4:** Run the tests; expect PASS.
- [ ] **Step 5:** Commit `feat: boost prompt v1`.

### Task 5: Orchestrator (`scripts/boost.py`)

**Files:**
- Create: `scripts/boost.py`, `tests/test_boost.py`

**Interfaces:**
- Consumes: Tasks 1 to 4. `enrich.load_resume_text(pdf_path) -> str`, `clean.write_json_atomic`.
- Produces: `run(root, run_date: str, top_n: int | None = None, model: str | None = None, prompt_version: str = "v1", force: bool = False, num_ctx: int = 16384, retries: int = 1, think: bool = False, client=None, resume_text: str | None = None, unload: bool = True, log=lambda m: None) -> dict` returning `{"date", "prompt_version", "selected", "done", "skipped_existing", "failed", "rollup": "done"|"skipped"|"failed"}`; `main(argv)` with `--top-n --model --prompt-version --force --num-ctx --retries --think --keep-loaded`.
- Defaults from `config.json` `boost` block (`top_n` 5, `model` `"qwen3.5:9b"`) when present; CLI overrides.
- Files written under `runs/<date>/`: `boost/<id>.json` (`{ok, prompt_version, generated_at, seconds, attempts, job:{id,company,posting_name}, rank, score, score_model, fallback, gaps, plan, warnings, spec_file}`; on failure `{ok: false, error, ...}`), `boost/specs/<id>-<slug>.md`, `boost_rollup.json` (`{prompt_version, tally, general_project, spec_file, tally_hash, warnings}`), and `boost.md` (Task 6).
- Flow per selected job: call A with `generate_json(parse=Plan.model_validate_json(extract_json(...)))`; `strip_em_dashes`; `check_plan`; if `plan.new_project` call B with `generate_text` and write the spec; skip a job whose file exists with `ok` and the same `prompt_version` unless `force`. Rollup: `tally_gaps` over all `passed_jobs`, skipped when `boost_rollup.json` has the same `tally_hash` and version and not `force`; one call C. Unload the model at the end.
- Raises `LLMError` when `filtered.json`, `enriched.json`, `config.json` or `resume/projects.md` is missing or corrupt, or the model is not downloaded; writes nothing in that case.

- [ ] **Step 1:** Tests with a fake client (replies chosen by which system prompt the call carries) and a temporary root with copies of `prompts/`: `test_writes_plans_rollup_and_report`, `test_only_top_n_get_plans_rollup_covers_all_passed`, `test_rerun_skips_done_jobs_and_force_redoes`, `test_failed_job_is_recorded_others_continue_and_retry_next_run` (Review Focus 3), `test_fewer_than_top_n_and_none_passed` (Review Focus 1), `test_fallback_ranked_job_is_marked` (Review Focus 2), `test_em_dashes_removed_from_written_files` (Review Focus 4), `test_unknown_project_and_bad_quote_become_warnings` (Review Focus 5), `test_new_project_triggers_spec_call_and_file`, `test_missing_projects_md_or_corrupt_input_writes_nothing`, `test_other_files_untouched` (byte compare of enriched/filtered/config), `test_unloads_model`.
- [ ] **Step 2:** Run `.venv/bin/python -m unittest tests.test_boost -v`; expect failures.
- [ ] **Step 3:** Implement `scripts/boost.py` (CLI mirrors `scripts/enrich.py`; progress to stderr, one JSON line to stdout, exit 1 if `failed` or rollup failed).
- [ ] **Step 4:** Run the tests; expect PASS, then the whole suite.
- [ ] **Step 5:** Commit `feat: boost step orchestrator`.

### Task 6: Report (`scripts/boost_report.py`)

**Files:**
- Create: `scripts/boost_report.py`, `tests/test_boost_report.py`
- Modify: `scripts/boost.py` (write `boost.md` through `write_text_atomic`)

**Interfaces:**
- Produces: `render_report(run_date: str, rollup: dict | None, plans: list[dict], notes: list[str]) -> str` and `write_text_atomic(path, text)` in `boost.py` (temp file then `os.replace`).
- Report layout: title with date; notes (fewer than top_n, fallback ranking, failures); rollup (tally table of the top 15 rows with columns skill, jobs, models, learnable share; then the general project with its spec path); then per job in rank order: heading with rank, company, title, score, link to the LinkedIn URL if present in `job`; verdict; rewordings as current/suggested; extensions with numbered pathway and effort; new project with spec path; skipped gaps; warnings in a "Check these" list.

- [ ] **Step 1:** Tests: `test_report_has_sections_in_order`, `test_report_lists_warnings`, `test_report_with_no_plans_says_so`, `test_report_marks_failed_job`.
- [ ] **Step 2:** Run the tests; expect failures.
- [ ] **Step 3:** Implement `render_report`; wire it into `run()`.
- [ ] **Step 4:** Run the whole suite; expect PASS.
- [ ] **Step 5:** Commit `feat: boost markdown report`.

### Task 7: Refine the prompt on real jobs, then document

**Files:**
- Modify: `prompts/boost/v1.py`, `prompts/boost/CHANGELOG.md`, `SPEC.md`
- Create: `resume/projects.md` header line `Projects: Conduit | BrokeNoMore | Cardiology-QA | sundial | Solwards | Spongy01.github.io | pdf-2-latex | miniGPT` (ignored by git)

- [ ] **Step 1:** Run `.venv/bin/python scripts/boost.py 2026-11-04 --force` (about 10 to 20 minutes with the 9B model).
- [ ] **Step 2:** Read `runs/2026-11-04/boost.md` against the resume and `resume/projects.md`: truthful, specific, fits the project, warnings explained. Note faults.
- [ ] **Step 3:** Edit the prompt, re-run, repeat at least three times; keep each round's faults and fixes in `prompts/boost/CHANGELOG.md`. Code-level fixes (sanitizing, checks) are preferred over longer prompts.
- [ ] **Step 4:** Add a boost section to `SPEC.md` (usage, files, plan shape, rollup shape, checks).
- [ ] **Step 5:** Run the whole suite, commit `docs: boost spec and prompt notes`.

# SPEC

Schemas and stage notes. Grows as steps are built.

## raw.json (written by the scraper, never edited)

`runs/<date>/raw.json` is a JSON list. One object per job, in scrape order.

| field | type | notes |
|---|---|---|
| id | string | LinkedIn job id, digits |
| posting_name | string | job title |
| company | string | |
| location | string | |
| work_model | string or null | Remote, Hybrid, On-site |
| posted_text | string | as shown on LinkedIn, e.g. "Reposted 2 hours ago" |
| posted_on | string or null | YYYY-MM-DD, scraper's guess |
| reposted | boolean | scraper's guess |
| description | string | full job text |
| apply_type | string | "external" or "easy_apply" |
| apply_url | string or null | |
| job_board_url | string or null | scraper's guess, usually null |
| linkedin_url | string | |
| source | object | `{search, page}` |
| scraped_at | string | ISO timestamp, UTC |

`closed` (boolean) is not written by the scraper today. A missing value means false.

## clean step (scripts/clean.py)

`python3 scripts/clean.py [YYYY-MM-DD]` reads `runs/<date>/raw.json` and writes
`runs/<date>/clean.json` and `runs/<date>/clean_report.json`. It never changes
raw.json, jobs.json or config.json and does not merge anything. Prints one JSON
line of counts. Missing or corrupt raw.json: non-zero exit, nothing written.

### clean.json

A JSON list in raw order. Same fields as raw, with these changes:

| field | change |
|---|---|
| id | normalized to a digits-only string |
| description | trailing whitespace stripped per line, 3+ newlines collapsed to 2, trimmed. null for closed jobs |
| posted_on | recomputed from posted_text and the date of scraped_at (minutes and hours = scrape date, day = 1, week = 7, month = 30, year = 365 days). If unparseable: the raw value or null, and the id goes in the report |
| reposted | true if posted_text contains "repost" (case-insensitive) |
| apply_url | LinkedIn redirects unwrapped, tracking params removed (utm_*, gclid, fbclid, trk, trackingId, refId, referrer). Other params and the fragment are kept |
| job_board_url | derived from apply_url for Ashby, Greenhouse, Lever, Workday, else null (table in `BOARD_RULES`) |
| closed | new, boolean, false if missing |
| flags | new, list of strings |
| review | new, true if any flag other than company_not_in_description |

Flags:

| flag | meaning |
|---|---|
| description_short | original description under 300 characters |
| description_truncated_suspect | original length is an exact multiple of 1000 (`TRUNCATION_MULTIPLE`) and at least 1000 |
| missing_apply_url | apply_type is "external" and apply_url is null after cleaning |
| company_not_in_description | company name (minus Inc, LLC, Ltd, Corp and similar) not found in the description. Soft hint only |

Length checks use the original description length, before normalizing.

Closed jobs: description null, flags empty, review false. apply_url and
job_board_url are left as they are. posted_on and reposted are still derived.

Required for every job: id (digits), posting_name, company, linkedin_url.
Jobs missing any are dropped and reported. For duplicate ids the first is kept.

### clean_report.json

`counts` (input, written, closed, dropped_invalid, duplicates, review_true),
`dropped_invalid` (index, id, reason), `duplicates` (id, index, kept_index),
`unparsed_posted_text` (list of ids), `flagged` (id, company, posting_name, flags).

## enrich step (scripts/enrich.py)

Needs the venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
(ollama, pypdf). Ollama must be running (`ollama serve`) with the models downloaded.

    .venv/bin/python scripts/enrich.py [YYYY-MM-DD] [--models M1 [M2 ...]]
        [--limit N] [--prompt-version v1] [--input clean.json] [--out-dir DIR]
        [--force] [--num-ctx 8192] [--retries 1] [--think]

Models: `--models`, or else `models.enricher` in `config.json` (a model name or a list; every model is run and stored
side by side). The boost step uses `models.booster` (one model, default qwen3.5:9b; `--model` overrides). A bad value in
`config.json` is an error; the scripts never write to `config.json`.

Reads `runs/<date>/clean.json`, the resume text from `config.json` `resume_path` (PDF, via
pypdf), and the prompt `prompts/enrich/<version>.py`. Writes `runs/<date>/enriched.json`
(or `--out-dir`). Closed jobs are skipped. Existing ok entries for the same model and prompt
version are kept unless `--force`; failed entries are retried. Prints one JSON line of
counts, progress goes to stderr. Exit is non-zero if the server is down, a model is not
downloaded, or any job/model pair failed.

The MLX runner rejects structured output, so the prompt asks for JSON, the reply is parsed
and validated, and one repair attempt is made with the validation error.

### enriched.json

A list: every clean.json job field, plus `enrichment`, an object keyed by model name:

| field | notes |
|---|---|
| ok | boolean |
| prompt_version | e.g. "v1" |
| generated_at | UTC ISO timestamp |
| seconds | wall time for this job and model |
| attempts | 1, or 2 if a repair was needed |
| error | only when ok is false |
| data | only when ok is true, see below |

`data`:

| field | notes |
|---|---|
| summary | 2 to 3 sentences about the job |
| experience | list of {area, level (required or preferred), years_min, years_max, evidence}. Empty when the JD states no years. A single number has min = max |
| required_skills, preferred_skills | lists of {skill, in_resume (yes, similar, no), resume_evidence, learnable_in_a_day (true, false, null when yes), learn_note} |
| score | integer 0 to 100, the sum of score_breakdown without reasoning |
| score_breakdown | required_skills (max 40), preferred_skills (10), learnability (10), experience_fit (20), role_alignment (20), reasoning |

### Score rubric (v1)

The first three parts are computed in code from the model's skill lists. Skill weight:
yes 1, similar 0.75, no but learnable in a day 0.5, no and not learnable 0. An empty list
gives half of its points. Learnability is the share of gaps (skills not "yes") that are
learnable in a day, or full points when there are no gaps. The model gives experience_fit
(table in the prompt, by the JD's required years) and role_alignment.

### Prompt versions

`prompts/enrich/vN.py` defines VERSION, SYSTEM, USER_TEMPLATE, OUTPUT_FORMAT, the pydantic
Output model, `score(output)` and optionally `postprocess(output)`. Released versions are
not edited, see `prompts/enrich/CHANGELOG.md`.

### jd_flags (scripts/jd_flags.py)

Keyword flags computed from the description text in code, with no model. The enrich step adds
`jd_flags` to every open job, whatever the models or the prompt version.

| field | values |
|---|---|
| sponsorship | true (JD says it sponsors work visas, H-1B etc), false (says it does not, or cannot consider people who need it), null (not mentioned) |
| sponsorship_evidence | the sentence that decided it, "" when null |
| clearance | true if a security clearance (or access to classified information, a federal background investigation) is mentioned, else false |
| clearance_evidence | first matching sentence |
| us_citizenship_required | true for "US citizenship is required", "must be a US citizen", ITAR, export-controlled data, US persons |
| us_citizenship_evidence | first matching sentence |

A negative sponsorship sentence wins over a positive one. `false` for clearance and citizenship means
"not found", not "confirmed absent". New phrasings are added to the pattern lists with a test.

### score_breakdown.detail (prompt v2)

`score_breakdown` keeps the five part scores and the reasoning. `detail` adds, per part, the maximum;
for required_skills and preferred_skills a list of {skill, in_resume, learnable_in_a_day, weight, points}
and whether the empty-list default was used; for learnability the gap count and how many are learnable.

## filter step (scripts/filter.py)

    python3 scripts/filter.py [YYYY-MM-DD] [--model qwen3.5:9b] [--input enriched.json]

Reads `runs/<date>/enriched.json` and `rules` from `config.json`. Writes `runs/<date>/filtered.json` and
`runs/<date>/filter_report.json`. Never changes config.json, jobs.json or enriched.json. It sits after the
enrich step because the years of experience come from the enrichment models. Prints one JSON line of
counts. Missing or corrupt input or config: non-zero exit, nothing written.

Rules applied:

| config rule | rejects when |
|---|---|
| experience_years {min, max} | the JD's required overall years start above max, or end below min. A JD that states no years passes. Preferred years and skill-specific years are ignored |
| exclude_us_citizenship_required | jd_flags.us_citizenship_required is true |
| exclude_clearance | jd_flags.clearance is true |
| exclude_sponsorship_denied | jd_flags.sponsorship is false (explicitly refused). null (not mentioned) and true pass |

Years come from `--model` (default qwen3.5:9b); if that model has no usable result for a job, the first
other model with one is used. Rules in the config that are set but not applied yet (must_have_stacks,
avoid_stacks, locations, work_models, exclude_keywords) are listed in `filter_report.json`
under `rules_set_but_not_applied`.

### filtered.json

A list, one record per job in enriched.json order: `id`, `company`, `posting_name`, `stage` ("passed",
"rejected" or "closed"), `reasons` (list of {rule, detail, evidence}, empty when passed), `years_required`
({min, max} or null), `years_model`. Rejected jobs are kept with their reasons, not deleted.

`filter_report.json` has `counts`, `rejected_by_rule`, `rules_applied`, `rules_set_but_not_applied`, and
the list of rejected jobs with the rules that rejected them.

## merge step (scripts/merge.py)

    python3 scripts/merge.py [YYYY-MM-DD]

Merges one run into `jobs.json` (a list) and `rollups.json` (an object keyed by run date). Reads
`runs/<date>/enriched.json`, `filtered.json`, `boost/<id>.json` and `boost_rollup.json`. Prints one JSON line
(`jobs_in_run`, `added`, `filled`, `unchanged`, `rollup`). Missing or corrupt input: non-zero exit, nothing written.

Each job record is the enriched job (all clean fields, `enrichment` keyed by model, `jd_flags`) plus:

| field | notes |
|---|---|
| filter | `{stage, reasons, years_required, years_model}` from filtered.json, or null |
| boost | the job's boost record (only if it succeeded), or null. `spec_text` holds the text of its spec file when there is one |
| stage | the filter stage (passed, rejected, closed), else "enriched" |
| run_date | the date of the run in which the job was first merged |
| user_status | null, "applied" or "skipped". Set only by the page; the merge never touches it |

Never overwrites: a job already in `jobs.json` only gets sections that are missing or null (for example a boost plan
that arrived a day later, or an enrichment model that was not there). Nothing is rewritten, and no `.bak` made, when
nothing changed. A rollup date already in `rollups.json` is kept; to take in a regenerated rollup, delete that date's key
and merge again. `jobs.json`, `jobs.json.bak` and `rollups.json` are git ignored.

## jobs page (scripts/serve.py, scripts/jobs_query.py, web/)

    python3 scripts/serve.py [--port 8765]      then open http://localhost:8765

A standard library server bound to 127.0.0.1. It refuses to start when jobs.json is corrupt. Every request needs a Host
of localhost or 127.0.0.1.

| route | what |
|---|---|
| GET / and /static/<file> | the page (`web/`); nothing outside `web/` is served |
| GET /api/view?range=today\|yesterday\|from&from=&sort=score\|recent&show=all\|passed\|rejected\|flagged\|boost&status=any\|new\|applied\|skipped&model=&q= | cards for the left nav, a score series and average per model, counts per filter, the date range, `latest_run_date`, `empty` |
| GET /api/job/<id>, GET /api/rollup/<date> | the full record, that day's rollup |
| POST /api/job/<id>/status `{"status": "applied" \| "skipped" \| null}` | the only write: `user_status` of that one job (atomic, with a `.bak`) |

All filtering, sorting and graph series are computed in `scripts/jobs_query.py`. "Today" means jobs whose `run_date` is
today's date (if there are none the page offers the latest run). "Flagged" means sponsorship refused, a clearance or
citizenship flag, or a clean-step review flag. Sorting by score uses the chosen model (default qwen3.5:9b, or the
average), ties broken by the required-skills part, then id; "most recent" uses `scraped_at`.

The page (`web/index.html`, `style.css`, `app.js`) is plain HTML, CSS and JavaScript with no dependencies and no network
requests. Data is inserted with `textContent` only and links from data must be http or https. Layout: a top bar with a
score graph per model found in the data (dashed line is the average) and the controls, a scrollable nav of job cards with
a pinned rollup card, and the selected job on the right. Up and down (or j and k) move between jobs; the controls are
remembered in the browser.

## streaming pipeline (enqueue, worker, stop, start)

Jobs are processed while the scraper is still working. The scraper hands each job to `enqueue.py`; a worker
cleans, enriches, filters and merges it into `jobs.json` one job at a time (the overlap is scraping versus
processing, never two jobs at once); `stoppipeline.py` tells the worker that no more jobs are coming.
Paths in this section are relative to the project root. `queue/` and `.jobs.lock` are git ignored.

### queue layout and run state (scripts/jobqueue.py)

| path | what |
|---|---|
| `queue/pending/<id>.json` | the raw job, waiting |
| `queue/processing/<id>.json` | the job the worker has taken. On worker start everything here goes back to pending |
| `queue/failed/<id>.json` | `{job, error}` for a job that failed twice. Calling `enqueue.py` with the same id puts it back in pending |
| `queue/run.json` | `{date, stopping}`. The run date is fixed when the run begins (`start.py`, or the first `enqueue.py` when there is none) and is never read from the clock afterwards, so a run that crosses midnight stays one run |

Files are written under a temp name and renamed, so a reader never sees half a job. A corrupt `run.json` or job file raises
`QueueError`.

### enqueue.py

    python3 scripts/enqueue.py '<job json>'      or      python3 scripts/enqueue.py -     (JSON on stdin)

Validates with the clean step's rules (id of digits, posting_name, company, linkedin_url), appends the job to
`runs/<run date>/raw.json` (skipped if that id is already there, so a retry after a crash is safe) and drops it in
`queue/pending/`. Prints one JSON line `{status: queued | duplicate | requeued, id, date, pending}`. A duplicate is an id already in
`jobs.json`, `pending` or `processing`. Errors print `{status: "error", error}` and exit 1 with nothing queued: bad JSON, missing
fields, corrupt `jobs.json` or `raw.json`, or `run.json` has `stopping: true`. The text for the Cowork scraper prompt is in
`docs/scraper-prompt.md`.

### stoppipeline.py

    python3 scripts/stoppipeline.py

Sets `stopping: true` in `queue/run.json` (creating the run if there is none). Prints `{status: "stopping", date, pending, processing}`.
It does not stop anything by itself and never touches the web page.

### worker.py

    .venv/bin/python scripts/worker.py [--poll 2] [--prompt-version v2]

Preflight first (config, `models.enricher` downloaded and Ollama reachable, resume, prompt), so a problem exits non-zero before any job
is touched. Per job (`scripts/pipeline_job.py`): clean, `enrich_one` for every model that has no ok result yet for this prompt version,
`jd_flags`, `filter.evaluate`, then `merge.merge_one`. `runs/<date>/clean.json`, `enriched.json` and `filtered.json` are updated one
entry at a time (the entry with the same id is replaced) and keep the schemas above. Closed jobs and jobs without a description make
no model call. A job gets two attempts, then goes to `queue/failed/` and its partial entries are removed from the run files.

When `stopping` is set and the queue is empty, the end-of-run steps run: `filter_report.json` and `clean_report.json` are rebuilt,
boost runs (`models.booster`), then `merge.merge_run` adds the boost plans and the rollup. Then `queue/run.json` is removed, the models
are unloaded and the worker exits. If an end step fails, `run.json` stays and starting the worker again retries only those steps.
Prints one JSON line `{date, processed, failed, end_steps: ok | failed | skipped, error?}`; exit is non-zero if a job or an end step failed.

### start.py

    python3 scripts/start.py [--port 8765]

Starts `ollama serve` if Ollama does not answer, begins the run, starts the worker and the page (unless the port already answers).
One JSON status line per component. Ctrl-C stops only what it started. When the worker exits the page and Ollama keep running.

### jobs.json lock (scripts/jobslock.py)

`jobs_lock(root)` is one exclusive `flock` on `.jobs.lock`. `merge.merge_run`, `merge.merge_one`, `enqueue.py`, `stoppipeline.py` and the
page's status write all take it and re-read the file under it, so the worker and the page cannot overwrite each other.

### page polling

`web/app.js` fetches `/api/view` every 5 seconds while the tab is visible. A failed poll changes nothing. New cards get a green
outline until opened. The open job, the scroll positions and the open description are left alone unless that job's own card changed.

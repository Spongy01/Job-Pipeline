# Job-Pipeline

A pipeline that collects LinkedIn job postings, enriches and scores them against my resume, filters them and shows them on a local page. The scraper hands each job over as it extracts it, and the models work on it while the scraper carries on.

## Layout
- config.json: searches, resume path, models (enricher list, booster), filter rules. Edited by me.
- jobs.json: master record, a JSON list with one record per LinkedIn job id. rollups.json: boost rollup per run date.
- scripts/: all helper scripts (Python 3, standard library only for the pipeline logic).
- runs/<YYYY-MM-DD>/: per-run files (raw.json, clean.json, enriched.json, filtered.json, reports, boost/).
- queue/: pending, processing and failed jobs plus run.json (run date and stopping flag). Git ignored.
- web/: the jobs page. prompts/: model prompts. resume/: my resume. docs/: specs, plans, the scraper prompt text.
- SPEC.md: schemas and every step in detail. Read it before changing anything.

## How it works
1. Scraper (Cowork task, browser, not in this repo): calls `scripts/enqueue.py` once per job and `scripts/stoppipeline.py` when done. Text for its prompt: docs/scraper-prompt.md.
2. `scripts/start.py` starts Ollama, the worker and the page. The worker (`scripts/worker.py`) takes one job at a time: clean, enrich with every model in models.enricher, filter, merge into jobs.json.
3. After stoppipeline.py and an empty queue the worker runs boost, the rollup and the final merge, then exits. The page keeps running.
4. The old batch route still works: clean.py, enrich.py, filter.py, boost.py, merge.py on a run date.

Each job in jobs.json has a "stage": passed, rejected, closed or enriched.
"user_status" (applied, skipped, or null) belongs to my app and page. The pipeline never changes it.

## Rules
- Paths are relative to the project root. Scripts find the root from their own location and work from any current directory.
- Python standard library only, unless I approve a dependency.
- Writes to jobs.json are atomic (temp file then os.replace) and keep a jobs.json.bak first.
- Never overwrite an existing job's data in jobs.json.
- A corrupt JSON file must raise an error, never be treated as empty.
- Scripts print one line of JSON to stdout and exit non-zero on failure.
- Never edit or delete files in runs/ by hand. They are the record of what was scraped.
- Every script gets tests in tests/ (pytest or unittest) that run against temporary directories, never my real data.
- Do not change the Cowork scraper prompt or run the scraper. It needs the browser and lives in the scheduled task.
- Do not commit resume/, jobs.json, or runs/. Keep them in .gitignore.
- Do not add em dashes to any text I might copy into a job portal.
- Writes to jobs.json take the shared lock (scripts/jobslock.py) and re-read the file under it. Do not commit queue/ either.

## Structure
Job-Pipeline/
  CLAUDE.md, SPEC.md, config.json
  jobs.json, jobs.json.bak, rollups.json   (git ignored)
  resume/                                  (git ignored)
  queue/                                   (git ignored: pending, processing, failed, run.json)
  scripts/
    start.py                one command: Ollama, worker, page
    enqueue.py              called by the scraper, one job per call
    stoppipeline.py         called by the scraper when it is done
    worker.py, pipeline_job.py   the streaming worker and its per-job steps
    jobqueue.py, jobslock.py     the spool, run state and the jobs.json lock
    clean.py, enrich.py, filter.py, boost.py, merge.py, jd_flags.py, llm.py   the steps (also runnable as a batch)
    serve.py, jobs_query.py       the jobs page server
    seen.py
  web/                      the jobs page
  prompts/, docs/, tests/
  runs/<date>/              raw.json, clean.json, enriched.json, filtered.json, reports, boost/   (git ignored)

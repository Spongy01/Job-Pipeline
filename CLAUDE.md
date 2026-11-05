# Job-Pipeline

A daily pipeline that collects LinkedIn job postings, filters them, and scores them against my resume. The data feeds a future app that reads jobs.json.

## Layout
- config.json: searches (name, url, pages), resume path, filter rules. Edited by me.
- jobs.json: master record, a JSON object keyed by LinkedIn job id.
- scripts/: all helper scripts (Python 3, standard library only).
- runs/<YYYY-MM-DD>/: per-day files (raw.json, run_log.json, logs/, filtered.json, enriched.json).
- resume/: my resume.
- SPEC.md: schemas and the stage lifecycle. Read it before changing anything.

## How it works
1. Scraper (Cowork task, browser, not in this repo): writes runs/<date>/raw.json.
2. scripts/merge.py: adds new jobs from raw.json into jobs.json.
3. Filter: writes runs/<date>/filtered.json, then scripts/apply_filter.py updates jobs.json.
4. Enrichment: summary, skills, and score into runs/<date>/enriched.json, then merged into jobs.json.

Each job in jobs.json has a "stage": scraped, passed, rejected, enriched, or closed.
"user_status" (applied, skipped, or null) belongs to my app. The pipeline never changes it.

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


## Structure:
Job-Pipeline/
  CLAUDE.md              (new)
  SPEC.md                (planned: schemas and stage lifecycle)
  config.json
  jobs.json              (master record, keyed by job id)
  jobs.json.bak          (planned: written by merge.py)
  resume/
    Ali_Asgar_Resume.pdf
  scripts/
    seen.py
    merge.py             (planned)
    apply_filter.py      (planned)
  runs/
    2026-11-04/
      raw.json
      run_log.json
      logs/              (planned: per-page logs)
      filtered.json      (planned)
      enriched.json      (planned)
# Streaming pipeline: process jobs while they are being scraped

## Intent

Today the scraper (a Cowork task driving the browser) writes every job to `runs/<date>/raw.json`, and only after it
finishes (30 to 40 minutes, because it waits at least 2 seconds per page) does the owner start clean, enrich, filter and
merge, which takes more time again. The goal is to overlap the two: each job is handed to the pipeline the moment the
scraper extracts it, the local models work on it while the scraper moves on to the next page, and the job shows up on the
jobs page minutes after it was scraped. By the time the scraper is done, nearly everything is already processed.

## Goals

1. `enqueue.py`: the scraper calls `python3 scripts/enqueue.py '<job json>'` once per job.
2. A background worker takes jobs from the queue one at a time and runs each through clean, enrich (all models in
   `models.enricher`) and filter, then merges the job into `jobs.json`.
3. The jobs page shows new jobs as they land, without a manual reload.
4. `stoppipeline.py`: the scraper calls it when it has no more jobs. The worker finishes everything already queued, then
   runs boost, then the final merge (boost plans and the daily rollup), then exits. The web page keeps running.
5. One command starts Ollama (if not already running), the worker and the web page.

## Non-goals

- Processing several jobs at the same time. Models run one job at a time; the overlap is scraping versus processing.
- A message broker (Redis, RabbitMQ) or any new dependency. Standard library only, plus what the venv already has
  (ollama, pypdf) for the model steps.
- Changing the scraper prompt or running the scraper. The owner pastes two new lines into the Cowork prompt; this project
  only supplies the text of those lines.
- Changing the content of the clean, enrich, filter, boost or merge outputs, or their prompts and scoring.

## Anti-goals (a failure even if it technically works)

- A job lost or processed twice because of a crash, a restart or a double call.
- `jobs.json` damaged or updates lost because the worker and the web server write at the same time.
- Overwriting an existing job's data, or touching `user_status`.
- A corrupt JSON file treated as empty.
- Stop killing the web page.

## Decisions made by the owner

- Queue-and-worker model: Claude calls a Python script with the job JSON; a process picks jobs up as they arrive.
- Overlap is scraping versus processing only; no parallel model runs.
- Boost needs all jobs, so it waits. It and the rollup run automatically once stop has been called and the queue is empty.
- `stoppipeline.py` stops the worker (after draining), never the web page.
- A single start command brings up Ollama, the worker and the web page.

## Design (the builder may adjust details; the behaviors above are fixed)

Items marked **(my call)** were decided by the designer, not the owner, and are open to change.

### Queue **(my call: a spool folder)**

A folder `queue/` at the project root (git ignored) holding one JSON file per job:
`queue/pending/<id>.json`, `queue/processing/<id>.json`, `queue/failed/<id>.json` (with the error). Moving a file between
folders is atomic. Files are written to a temp name and renamed, so the worker never sees a half-written job. On worker
start, anything left in `processing/` goes back to `pending/` (crash recovery). A job that fails twice goes to `failed/`
and the worker carries on; failed jobs are reported at the end and can be re-queued by hand.

### enqueue.py

`python3 scripts/enqueue.py '<json>'` (also accepts the JSON on stdin with `-`). Behavior:

- Validates the minimum the clean step already requires: id (digits), posting_name, company, linkedin_url. Malformed JSON
  or a missing field: exit non-zero, one-line JSON error on stdout, nothing queued, nothing written.
- Skips, with exit 0 and `{"status": "duplicate"}`, a job id that is already in `jobs.json`, in the queue, or already in
  today's `raw.json`. Corrupt `jobs.json` is an error, never treated as empty.
- Refuses (non-zero) when the stop marker for today's run exists, so a late job is not silently dropped.
- Appends the job to `runs/<date>/raw.json` (kept as the record of what was scraped; written atomically, under the lock
  below) and drops it into `queue/pending/`. Prints `{"status": "queued", "id": ..., "queued": N}`.
- The run date is the date the job is enqueued **(my call)**, in local time, matching the existing `date.today()` default.

### Worker (scripts/worker.py)

Loop: take the oldest file from `pending/`, process it, repeat; sleep briefly when empty. Per job, reusing the existing
functions rather than copying logic:

1. clean (`clean.py` per-job logic), appended to `runs/<date>/clean.json` (and the clean report).
2. enrich with every model in `models.enricher` (`enrich_one`, `detect_jd_flags`), written to `runs/<date>/enriched.json`.
3. filter (`filter.evaluate`), written to `runs/<date>/filtered.json`.
4. merge this one job into `jobs.json` using the existing `build_record` and `fill_missing` rules (never overwrites; a
   `.bak` is written first; atomic replace). `stage` is the filter result, so rejected jobs also appear.

The per-run files (`clean.json`, `enriched.json`, `filtered.json`) keep the same schemas as SPEC.md, so boost and the
existing scripts still work on them unchanged. The worker keeps the Ollama models loaded between jobs for speed
**(my call)** and unloads them when it finishes. Closed jobs and jobs without a description follow the existing rules in
the clean and enrich steps.

### Stop and the end-of-run steps

`stoppipeline.py` creates a stop marker (`queue/STOP`, containing the run date) and prints one JSON line with how many
jobs are still queued. The worker sees the marker, drains `pending/`, and then runs `boost.run` and `merge.merge_run`
for that run date (these already exist and are idempotent, so a re-run only fills what is missing). Then it removes the
marker, unloads the models, and exits. If a job was `failed`, boost still runs on the rest. The worker's exit code is
non-zero if any job failed or boost failed; the summary is printed as one JSON line. Calling stop when no worker is
running is allowed: the marker stays and the next worker start drains and finishes immediately.

### Start command (scripts/start.py or start.sh) **(my call on the form)**

One foreground command, `python3 scripts/start.py`, that:

1. Checks whether Ollama answers at its usual address; if not, starts `ollama serve` itself and waits until ready. It
   only stops an Ollama it started.
2. Starts the worker (using `.venv/bin/python`).
3. Starts the web server (`serve.py`), skipping it with a note if the port already answers.
4. Prints one status line per component and stays in the foreground. Ctrl-C stops everything it started. When the worker
   exits after a stop, the web page and Ollama keep running until Ctrl-C.

The run date is fixed when the worker starts; one start command per run. Starting it again for a new run (after the worker
has exited) is the normal daily use.

### Live page

`serve.py` already re-reads `jobs.json` when the file changes. The page does not poll today, so add light polling in
`web/app.js` (every few seconds, only while the tab is visible), refreshing the cards without losing the selected job or
scroll position, and flagging new arrivals. No new dependencies or network requests beyond the page's own server.

### Shared file lock

The worker and `serve.py` (which writes `user_status`) can write `jobs.json` at the same moment. `serve.py`'s current lock
is in-process only. Add one cross-process lock (an `fcntl.flock` on a lock file, standard library) used by every writer of
`jobs.json` and `raw.json`: the worker, `serve.set_status` and `merge.py`. Each writer re-reads the file under the lock
before changing it.

## Calls settled after the builder check **(all my calls)**

These replace anything above that disagrees with them.

1. **Run date and stop marker.** One state file, `queue/run.json`: `{"date": "YYYY-MM-DD", "stopping": false}`. The run date
   is fixed when the run begins (the start command, or the first `enqueue.py` when none exists, using the local date then)
   and is read from this file by `enqueue.py`, the worker and `stoppipeline.py`, never from the clock afterwards. A run
   that crosses midnight stays one run. `stoppipeline.py` sets `stopping: true` (creating the file for today if it is
   missing). `enqueue.py` refuses while `stopping` is true. The worker deletes `run.json` only after the end-of-run steps
   succeed, so the next `enqueue.py` starts a fresh run with the new date. This replaces the separate `queue/STOP` file.
2. **Failed jobs.** Per-job appends to `clean.json`, `enriched.json` and `filtered.json` replace any entry with the same id,
   so a retry never duplicates. `enqueue.py` treats an id sitting in `queue/failed/` as re-queueable: it moves it back to
   `pending/` instead of reporting a duplicate. No separate requeue script.
3. **Scraper never calls stop.** The worker idles and the queue stays safe on disk. The owner runs `stoppipeline.py` by
   hand. No idle timeout.
4. **filter_report.json.** The worker rebuilds it once from `filtered.json` during the end-of-run steps, using the same
   report code as `filter.py` (factor it out of `filter.run` so both use it). Individual jobs do not touch it.
5. **End-of-run failure.** If boost or the final merge fails, `run.json` stays (still `stopping: true`), the worker exits
   non-zero with the error, and starting the worker again retries only the end-of-run steps, which are idempotent.
6. **Passing the job.** `enqueue.py` accepts argv or stdin (`-`). The two lines for the Cowork prompt use stdin with a quoted
   heredoc, because descriptions are long and full of quotes.

## Constraints (from CLAUDE.md, still in force)

Python 3 standard library only; paths relative to the project root, scripts work from any directory; atomic writes with a
`.bak` first for `jobs.json`; never overwrite an existing job or touch `user_status`; a corrupt JSON file raises; each
script prints one line of JSON and exits non-zero on failure; tests in `tests/` against temporary directories, never real
data, with a fake Ollama client; `runs/`, `resume/`, `jobs.json` and `queue/` stay out of git; no em dashes in any text the
owner may copy into a job portal. The scraper prompt and the scraper itself are not touched.

## Documentation to update

- `SPEC.md`: queue layout, enqueue, worker, stop, start, lock, page polling.
- `CLAUDE.md`: the flow description and structure section are out of date (it still describes `jobs.json` as an object keyed
  by id, filter before enrich, and `apply_filter.py`). Bring it in line with SPEC.md and add the new scripts.
- The two lines to paste into the Cowork scraper prompt (the `enqueue.py` call per job and the `stoppipeline.py` call at
  the end), given in the final summary, not applied.

## Left to the builder

File and function names beyond those given; the exact polling interval; how the worker is structured internally (threads
are not needed); the queue ordering tie-breaks; log format (progress to stderr, as the other scripts do); how per-job
appends to the run files are made efficient.

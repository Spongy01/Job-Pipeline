# Jobs merge and jobs page: design

Status: draft for approval, 2026-11-05. Nothing in this document has been built.

Two parts, built in this order. Part 1 puts the day's results into `jobs.json`. Part 2 is the page that reads it.

## Intent and why

The pipeline now produces, per day, a cleaned job list, enrichment from two models, keyword flags, a filter
decision and (for the best five) a boost plan. All of it lives in separate files under `runs/<date>/`. The author
wants one place to look at the day's jobs and decide where to apply and what to work on: a web page with a list of
jobs on the left and the full picture of the selected job on the right, in the style of LinkedIn's job search.
Part 1 gathers everything into the master record `jobs.json` (which the page reads, and which CLAUDE.md says the
author's app owns the `user_status` of). Part 2 is the page.

## Goals

1. `scripts/merge.py` adds every job of a run, with all its data, to `jobs.json`, and the daily rollup to
   `rollups.json`, without ever overwriting what is already there.
2. A page (`scripts/serve.py` plus static files in `web/`) with a top bar, a scrollable left nav of job cards
   and a main page for the selected job, as shown in the approved mockup
   (`docs/superpowers/specs/assets/jobs-page-mockup-v2.html`, open it in a browser; Sage palette is the chosen one, the
   palette switcher at the top of the mockup is only for comparison).
3. The author can mark a job applied or skipped from the page; that is the only thing the page writes.

## Non-goals

- No running of the pipeline from the page. No scraping, enrichment or boost buttons.
- No editing of the resume, plans or specs. No hosting, accounts or login. Not built for phones.
- No change to the scoring, the filter or the boost step.
- No build tooling, no npm, no external network requests (fonts, CDNs): the page works offline.

## Anti-goals

- A merge that loses or silently changes data already in `jobs.json`, or that touches `user_status`.
- A page that shows a model's guess as a fact: risky rewordings must stay marked as risky, and flags must show the
  quoted evidence sentence.
- A page that can write anywhere except `user_status` of one job.

## Part 1: merge (`scripts/merge.py`)

Usage: `python3 scripts/merge.py [YYYY-MM-DD]` (default: today). Standard library only.

Reads `runs/<date>/enriched.json` (every job with its clean fields, `enrichment`, `jd_flags`),
`runs/<date>/filtered.json`, `runs/<date>/boost/<id>.json` (if present), `runs/<date>/boost_rollup.json` (if present).
Writes `jobs.json` (and `jobs.json.bak` first) and `rollups.json`. Prints one JSON line of counts; exits non-zero
and writes nothing if an input is missing or corrupt. Writes are atomic.

`jobs.json` stays a JSON list. Each record is the enriched job (all its fields) plus:

- `filter`: `{stage, reasons, years_required, years_model}` copied from `filtered.json`.
- `boost`: the job's boost record, or null.
- `stage`: the filter stage (`passed`, `rejected`, `closed`).
- `run_date`: the date of the run in which the job was first merged.
- `user_status`: null. Owned by the page; the merge never sets or changes it.

Never overwrite: a job whose id is already in `jobs.json` keeps every existing section. Only sections that are
missing or null are filled in (for example `boost` arriving a day after the job). `rollups.json` is an object
keyed by run date and a date already present is left as is (to take in a regenerated rollup, delete that date's key and
merge again).

Decisions: all of the run's jobs are merged, including rejected ones, so the page can show them with their
reasons. Duplicate postings keep their own ids (the filter and boost steps already treat them).

## Part 2: the page

### How it is served

`python3 scripts/serve.py [--port 8765]` starts a standard library HTTP server bound to 127.0.0.1 only.

- `GET /` and `GET /static/...`: files from `web/` (`index.html`, `app.js`, `style.css`).
- `GET /api/view?range=today|yesterday|from&from=YYYY-MM-DD&sort=score|recent&show=all|passed|rejected|flagged|boost&status=any|new|applied|skipped&model=<name>&q=<text>`:
  the list for the left nav (card fields only), the graph series for every model in the same order, counts per
  filter, and the list's date range. All filtering, sorting and graph series are computed in Python
  (`scripts/jobs_query.py`), so they are tested without a browser.
- `GET /api/job/<id>`: the full record. `GET /api/rollup/<date>`: that day's rollup.
- `POST /api/job/<id>/status` with `{"status": "applied" | "skipped" | null}`: sets `user_status` on that one job
  and nothing else. It checks that the `Host` header is `localhost` or `127.0.0.1` and that the content type is JSON,
  rewrites `jobs.json` atomically with a `.bak`, and returns the new status. This is the only write.

### What the author sees (the approved mockup)

- **Top bar.** One small wide green line graph per model found in the data (x: the jobs in the current list, in the
  displayed order; y: that model's score), a dashed gray line at the average, with the model name, the average and
  the count above it. Controls: time frame (Today, Yesterday, From date plus a date picker; default Today); sort
  (Most scored, Most recent); Show (All, Passed filter, Rejected, Flagged, With boost plan), each with its count;
  score model (which model ranks the list; default qwen3.5:9b); status (Any, New, Applied, Skipped); search.
- **Left nav.** Independently scrollable cards: title, company, location, time, each model's score, badges (boost,
  rejected, models differ by 25 or more, no apply link). The selected card is highlighted. A pinned "Today's
  rollup" card sits above the job cards.
- **Main page, in this order.** Title; company, location, work model, posted time, filter result; Apply, LinkedIn,
  boost badge, scores, applied/skipped buttons, flag badges; a boost banner when a plan exists; summary per model;
  score boxes per model with the five-part breakdown; skills per model grouped as you have, similar, missing, each
  for required and then preferred; the boost section; the job description. Rejected jobs show their rejection
  reasons with the quoted evidence.
- **Boost section.** The verdict; project extensions with effort and the pathway; "add to your resume" skills the
  repos already show; new project with a copy button for its spec text; clean rewordings, then the risky ones under
  their own heading with their problems; skipped gaps; checks and warnings.
- **Rollup view.** The tally (skill, jobs, models, learnable share, in your repos), the "already in your repos, not
  on your resume" list, and the general project with its spec.
- **Look.** The Sage palette and the larger font of mockup v2. Keyboard: up and down (or j and k) move between jobs.
  The chosen filters are remembered in the browser.

## My calls (the author can overrule any)

1. **Rollup placement:** a pinned card at the top of the left nav that opens the rollup in the main page.
2. **"Today"** means `run_date` equals today's date. If there are no jobs for the chosen range, the page says so and
   offers to jump to the latest run date instead of showing a blank screen. "Yesterday" and "From date" use `run_date`
   too, not the posting date.
3. **"Flagged"** means the job has any keyword flag set (sponsorship refused, clearance, citizenship required) or a
   clean-step review flag (for example no apply link).
4. **"Most recent"** orders by `scraped_at`, newest first, then by id. "Most scored" orders by the selected score
   model, ties broken as in the boost step (required skills part, then id).
5. **Server side querying.** The browser only renders; every filter, sort and graph series comes from `/api/view`.
6. **Standard library server on port 8765**, no framework. The page is vanilla HTML, CSS and JavaScript in `web/`.
7. **jobs.json stays a list**, as the author decided; the server keeps an id index in memory.
8. **Model list** is read from the data (every model key in `enrichment`), not hard coded.

## Constraints

- Python standard library only for the server and merge (project rule); no JavaScript dependencies.
- Paths found from the script's own location; `jobs.json`, `jobs.json.bak` and `runs/` stay out of git.
- The server never serves anything outside `web/` and the three API families above (no directory listing, no path
  traversal).
- A corrupt `jobs.json` makes the server and the merge fail loudly, never behave as if it were empty.
- Tests for both parts use temporary directories and small fixtures, never the real data (CLAUDE.md).

## Left to the builder

- The exact JSON shape of `/api/view` (as long as it carries what the page above needs), file layout inside `web/`,
  how the graphs are drawn (inline SVG is the natural choice), and the text of empty and error states.
- How to avoid two writers racing on `jobs.json` (a lock around the server's write is enough; the merge runs when the
  server is not being used).
- Whether the model score ranks cards by the chosen model or by the average when "Average of both" is chosen (the
  mockup lists it as an option).

## Success looks like

After running the pipeline for a day and then `merge.py`, starting `serve.py` and opening the page shows that day's
jobs in the approved layout; switching sort, filter, time frame and score model changes the list, the graphs and the
counts correctly; a rejected job shows why; a job with a boost plan shows it with risky rewordings marked;
applied/skipped persists across a restart and nothing else in `jobs.json` changes; a second merge of the same day
changes nothing. Code level: all tests pass, and bad input fails loudly without partial writes.

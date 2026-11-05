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

    .venv/bin/python scripts/enrich.py [YYYY-MM-DD] --models M1 [M2 ...]
        [--limit N] [--prompt-version v1] [--input clean.json] [--out-dir DIR]
        [--force] [--num-ctx 8192] [--retries 1] [--think]

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

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

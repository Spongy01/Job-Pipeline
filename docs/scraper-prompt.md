Prompt text for the Claude chat / Cowork scraper (paste it into the task; this repo never changes the scraper itself).

```
You are scraping LinkedIn jobs for me. For every job you extract, hand it to my pipeline right away. Do not wait until the end and do not write raw.json yourself.

SENDING A JOB
After you extract each job, run this command with the job as one JSON object (same fields as before: id, posting_name, company, location, work_model, posted_text, posted_on, reposted, description, apply_type, apply_url, job_board_url, linkedin_url, source, scraped_at):

python3 /Users/aliasgar/Documents/Job-Pipeline/scripts/enqueue.py - <<'EOF'
{ ...the job JSON... }
EOF

Always pass the JSON through the quoted heredoc as shown, because descriptions are long and contain quotes.

The command prints one line of JSON:
- {"status": "queued", ...}: done, move on to the next job.
- {"status": "duplicate", ...}: the job was already handled, skip it and move on. This is normal.
- A non-zero exit or an error message: read the message, fix the JSON (for example a missing id, posting_name, company or linkedin_url), and try that job once more. If it still fails, note the job id and continue with the next one.
- An error saying the run is stopping: you called stop earlier, so do not send more jobs.

Send each job as soon as you have it, one command per job. Keep the 2 second minimum wait between pages as before.

WHEN YOU ARE FINISHED
When you have scraped every page of every search and sent every job, run this exactly once:

python3 /Users/aliasgar/Documents/Job-Pipeline/scripts/stoppipeline.py

It tells the pipeline that no more jobs are coming. The pipeline then finishes the jobs still waiting and runs its final steps. Do not run it before you are done, because after it runs, no more jobs are accepted.

In your final message, tell me how many jobs were queued, how many were duplicates, and the ids of any that failed.
```

# Boost step: design

Status: draft for approval, 2026-11-05. Nothing in this document has been built.

## Intent and why

Applying well to a job often comes down to one question: can I close a few gaps between my profile and
this JD with real work, and say so clearly on my resume? Answering it by hand means opening each JD,
collecting the missing skills, pasting the resume into a chat, and asking whether an existing project
could take the skill or a new one is needed. It is mundane, so it often gets skipped and the application
goes out as is.

The boost step does that work for the best jobs of the day, after the pipeline has scraped, cleaned,
enriched and filtered them. For each selected job it answers: what can I reword, what can I add to a
project I already have, and what new project would close the rest. It also looks across all of the
day's jobs and says which missing skills show up most, with one general project that would cover them.
Two side benefits: a list of real project ideas for GitHub, and spec documents that can be handed straight
to Claude Code to start building.

## Where it sits

scrape -> clean -> enrich (gemma4:e4b, qwen3.5:9b) -> filter -> **boost**

Boost reads files already produced by earlier steps and writes new files. It never changes them.

## Goals

1. For each of the top jobs, a personalized plan with any of three kinds of suggestion (one, two or all
   three, whatever fits that job):
   - **Rewordings** of existing resume lines so they line up with the JD's own wording.
   - **Project extensions**: add a missing skill to one of the author's existing projects, with a step by
     step pathway, only where it makes architectural sense for that project.
   - **A new project** when no existing project fits, with a basic spec document that can be given to
     Claude Code.
2. A daily rollup across all jobs that passed the filter: a count of every missing skill (how many jobs
   mention it), and one general project or project upgrade that covers the most common gaps.
3. Every suggestion that needs new work says so, with a rough effort estimate in hours.

## Non-goals

- Not applying to jobs, not writing cover letters, not producing a whole new resume.
- Not editing the resume PDF or any repository. It only suggests.
- Not changing enrichment scores, and not re-running enrichment.
- Not running for rejected, closed or low-ranked jobs.
- No web research. Everything comes from the files below.
- No use of the models other than qwen3.5:9b for the boost calls (see decisions).

## Anti-goals (a failure even if the code runs)

- **Overclaiming.** A suggestion that lets the resume say something that is not true. Rewordings may use
  only facts already in the resume. A suggested bullet for work that has not been done yet is marked "write
  this only after you have built it". No invented numbers.
- **Generic advice** ("learn AWS") with no link to a specific project or resume line.
- **Suggestions that do not fit the project**, for example adding Kubernetes to a project that is a
  single script, or adding RAG to a project where it has no purpose.
- **Unrealistic scope.** A weekend sized gap answered with a three month project.
- Em dashes in any text meant to be copied into a job portal or resume (project rule in CLAUDE.md).

## Inputs

All paths relative to the project root.

- `runs/<date>/filtered.json`: which jobs passed (`stage == "passed"`).
- `runs/<date>/enriched.json`: job fields, `jd_flags`, and per model skill lists with `in_resume`,
  `learnable_in_a_day`, scores.
- The resume text from the PDF at `config.json` `resume_path` (same extraction as the enrich step).
- `resume/projects.md`: verified summaries of the author's GitHub projects, including what each repo does
  not have and the analysis of natural extension points. This is the source of truth about the projects.
- `config.json`: an optional `boost` block, `{"top_n": 5, "model": "qwen3.5:9b"}`. Defaults apply when it
  is missing. The author edits config.json; the script never writes to it.

## Decisions made with the author

- Runs after the filter. Candidates are the jobs with `stage == "passed"`.
- The best 5 of the day, ranked by the qwen3.5:9b score, run through the full per-job plan. A fixed top N,
  not a score cutoff, because the 9B scores almost everything between 64 and 100.
- Boost calls use only qwen3.5:9b, the stronger model. The gap data from all enrichment models is passed
  in as input.
- Both a per-job plan and a daily rollup. The rollup is a count of missing skills plus a general project.
- Project extensions are written against the real projects in `resume/projects.md`, not the resume bullets
  alone.

## My calls (the author can overrule any of these)

1. **Duplicates.** The same posting can appear under several ids (Precisely, Mintive and Fuse Energy each do
   in the 2026-11-04 data). Before ranking, drop duplicates that have an identical description (compare a
   hash of the normalized text), keeping the first. Only boost selection does this; the filter and the data
   files keep every job.
2. **Rollup population.** The tally covers all jobs that passed the filter (16 on 2026-11-04), not only the
   top 5, because the question it answers is what to build to fit the market. Duplicates are removed here too.
3. **Gap agreement.** A gap is a skill whose `in_resume` is "no" or "similar" in a model's result. The tally
   and the per-job input record how many of the enrichment models flagged it. A gap flagged by only one model
   is passed on marked "flagged by one model", and the boost model is told to check it against the resume.
4. **Two calls per plan.** Call A returns a structured plan (JSON). Call B, only if the plan contains a new
   project, returns the spec document as Markdown. This keeps the structured reply short for a 9B model.
   The rollup's general project gets its spec the same way.
5. **Effort scale.** Suggestions are sized in hours and the default is weekend sized: project extensions
   up to about 16 hours, new projects up to about 40 hours. The author has not said how much time they have;
   this is the open question below.
6. **Checks in code.** The model's plan is checked against the files, in the same spirit as `evidence_found`
   in enrichment: a rewording's `current_text` must appear in the resume text, and a project extension must
   name a project that exists in `resume/projects.md`. A failed check does not drop the suggestion; it sets a
   flag on it (`current_text_found: false`, `project_known: false`) and the report shows the warning.
7. **No usable 9B score.** If a passed job has no successful qwen3.5:9b result, rank it by the next
   enrichment model's score instead, and say so in its record. Ties break on the required skills part of the
   score, then on job id, so the choice of the top 5 is repeatable.
8. **Prompt versioning** follows the enrichment pattern: `prompts/boost/vN.py`, `prompt_version` recorded in
   every output, changelog, a released version is never edited. The prompt is refined by running it on the
   real top 5 of 2026-11-04 several times before it is called v1.

## What it produces

All under `runs/<date>/` (git ignored). Written atomically. Existing results for the same job and prompt
version are kept unless `--force`.

- `boost/<job_id>.json`: the per-job plan, with the job's id, company, title, the gaps it was given, the
  model's plan, the code checks, `prompt_version`, timing.
- `boost/specs/<job_id>-<slug>.md` and `boost/specs/rollup-<slug>.md`: spec documents for new projects, ready
  to hand to Claude Code.
- `boost_rollup.json`: the skill tally (computed in code) and the general project suggestion.
- `boost.md`: one readable daily report. The rollup first, then each of the top jobs: verdict, rewordings,
  project extensions with pathways, new project, gaps deliberately skipped, warnings from the checks.
- stdout: one JSON line of counts. Non-zero exit if the server is down, the model is missing, an input file
  is missing or corrupt, or any job failed.

### Shape of a per-job plan (model output)

- `verdict`: two sentences, the best move for this job.
- `rewordings[]`: `resume_location`, `current_text` (quoted from the resume), `suggested_text`,
  `jd_skills_aligned`, `why`.
- `project_extensions[]`: `project`, `jd_skills_covered`, `why_it_fits`, `what_to_build`,
  `pathway[]` (ordered steps with a short detail), `effort_hours`, `resume_bullet_after_building`.
- `new_project` (or null): `name`, `one_liner`, `jd_skills_covered`, `why_no_existing_project_fits`,
  `effort_hours`, outline for the spec.
- `skipped_gaps[]`: `skill`, `reason` (for example "needs production experience, not a weekend task").

The model may return any subset. It returns a new project only when the project extensions leave important
gaps open.

### Shape of the rollup

- `tally[]`, computed in code over the passed jobs: canonical skill name, number of jobs, job ids, how many
  models flagged it, share of mentions the models called learnable in a day, required or preferred.
- `general_project`: from one model call that sees the top of the tally, the resume and `projects.md`:
  either an upgrade to an existing project or one new project, with the skills it covers and why, and a spec.

## Constraints

- Python, standard library only, plus the packages already approved (`ollama`, `pypdf`) in the project venv.
- Script in `scripts/`, prompts in `prompts/boost/`, tests in `tests/` using temporary directories and a fake
  Ollama client, never real data (project rules in CLAUDE.md).
- The Ollama MLX runner rejects structured output, so the JSON shape is requested in the prompt, parsed and
  validated, with one repair attempt, exactly as in the enrich step. The boost needs a larger context than
  enrichment (resume, project summaries, JD and gaps in one prompt); start at a 16384 token window.
- One model in memory at a time. The script unloads the model when it finishes.
- Never writes to `config.json`, `jobs.json`, `resume/`, `enriched.json` or `filtered.json`.

## Left to the builder

- Whether to share the model call, JSON extraction and repair code with `scripts/enrich.py` by moving it into
  a small shared module, or to import from enrich.
- How to normalize skill names for the tally (lowercase, drop parentheses, split alternatives, a small alias
  table in a data file). Skills written as an "(any one)" alternative group count under the group label.
- The exact prompt wording, the file layout of `boost.md`, the slug format, and the hashing for duplicates.
- How to parse the project names out of `projects.md` for the check (headings are `## 1. Name (resume
  project)`; the builder may also add a machine readable list at the top of that file, which the author
  edits).

## Success looks like

Running the step on `runs/2026-11-04` produces five plans and a rollup that the author can read in one
sitting and judge as: truthful against their resume and repos, specific to each JD, sensible for the named
project, and sized for the time they have. Code level success: all tests pass, a rerun changes nothing, and a
corrupt or missing input fails loudly without writing partial files.

## Open question for the author

How many hours can you realistically put into one project suggestion: a weekend (about 16 hours), a week
(about 40), or more? The effort caps above are set from this.

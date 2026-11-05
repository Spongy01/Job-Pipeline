# Boost prompt versions

A released version is never edited. To change the prompt, copy `vN.py` to `vN+1.py` and run
`scripts/boost.py --prompt-version vN+1`. Each output records the `prompt_version` it was made with.

## v1 (frozen 2026-11-05, refined on runs/2026-11-04, model qwen3.5:9b)

Three calls: PLAN (JSON per selected job), SPEC (Markdown, only for a new project), ROLLUP (JSON, once per day).
Honesty rules are in the prompt and enforced in code (see scripts/boost_data.py): em dashes are replaced,
rewording quotes are checked against the resume, project names against resume/projects.md.

### Round 1 findings (first run, 5 of 5 plans valid, 2 specs)

1. A job with no gaps (Morningstar, score 100) still got three unrelated extensions, and the verdict said
   none were needed. Prompt now says no gaps means no extensions or new project; code flags any extension that
   covers none of the listed gaps (`covers_gap`).
2. Rewordings that were identical to the original (Remolity, three of them) and rewordings that appended
   claims the resume does not make ("JSON", "Pydantic", "TypeScript frontend"). Code drops no-op rewordings and
   checks every skill a rewording names: on the resume is fine, in a project summary is noted (`adds_from_repo`),
   nowhere is a warning (`adds_unsupported`).
3. An invented number in a resume bullet ("reduce inference latency by 30%"), and 4 of 5 bullets without the
   "Write this only after you have built it" prefix. Code now adds the prefix and warns on any number that is
   not in the resume or the project summaries. Prompt asks for "[your measured result]".
4. The skipped gaps list was used for gaps the plan covers. Code removes them.
5. The rollup said BrokeNoMore "lacks a TypeScript frontend" when its frontend is Next.js with TypeScript, and
   asked for TypeScript as a gap although sundial, Solwards and BrokeNoMore all show it. The tally now checks
   every missing skill against `resume/projects.md` (only the parts that say what a project has, never the
   "Not in the repo" part) and lists skills "already in your repos, not on your resume" separately; the rollup
   prompt is told not to build those.
6. The tally had "AWS/GCP/Azure", "Azure", "cloud platforms" and "cloud infrastructure" as separate rows.
   Merged through a small alias table (`SKILL_ALIASES` in scripts/boost_data.py).
7. Pathway steps numbered by the model ("1 - ...") on top of the report's own numbering. Stripped in code.

### Round 2 findings (second run on the same 5 jobs)

Fixed: the padded Morningstar plan, no-op rewordings, invented numbers (placeholders used), skill grouping, the
repo-aware tally. Still wrong:

1. Rewordings drop specifics and inflate: "Flask SSE", "OpenAI, Anthropic, Ollama" and "under 2ms p95" were
   removed, and "top-K retrieval accuracy" became "AI solution accuracy". Code now lists what a rewording drops
   (numbers, ALL CAPS and capitalised tool names missing from the new text) as a warning, and the prompt says to
   keep every number, metric and named tool.
2. Rewordings that append a claim ("Added unit tests for core logic using pytest" when Conduit has none). Code now
   shows exactly what a rewording added (word level diff, `added_text`, printed as "New in this wording") and warns
   about words in it that are in neither the resume nor the project summaries.
3. A per-job plan skipped TypeScript as "small projects" although the repos prove it. Per job, gaps that the repos
   already show are now marked in the prompt ("no need to build it") and listed by code under "Add to your resume".
4. The rollup's general project still covered TypeScript although told not to; code now warns when it does.
5. Generic words (integration, testing, deployment) matched the repo summaries and showed up as "already in your
   repos"; a small stop list (`GENERIC_TERMS`) prevents it.

### Round 3 findings and the freeze

Fixed: the checks now surface every problem found in round 2 (dropped details, appended claims, repo-proven
skills). Still wrong, and not fixable by more prompt rounds on a 9B model: rewordings invent small claims
("analyzing JSON logs for error patterns", "object-oriented error handling logic"). Decision: keep them, but any
rewording with a problem is shown under "Risky rewordings" with its reasons, and the clean ones are listed first.
v1 is frozen here; further changes go into v2.

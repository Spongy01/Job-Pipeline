# Boost prompt versions

A released version is never edited. To change the prompt, copy `vN.py` to `vN+1.py` and run
`scripts/boost.py --prompt-version vN+1`. Each output records the `prompt_version` it was made with.

## v1 (in progress, refined on runs/2026-11-04, model qwen3.5:9b)

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

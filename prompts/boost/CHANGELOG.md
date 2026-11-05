# Boost prompt versions

A released version is never edited. To change the prompt, copy `vN.py` to `vN+1.py` and run
`scripts/boost.py --prompt-version vN+1`. Each output records the `prompt_version` it was made with.

## v1 (in progress)

Three calls: PLAN (JSON per selected job), SPEC (Markdown, only for a new project), ROLLUP (JSON, once per day).
Honesty rules are in the prompt and enforced in code (see scripts/boost_data.py): em dashes are replaced,
rewording quotes are checked against the resume, project names against resume/projects.md.

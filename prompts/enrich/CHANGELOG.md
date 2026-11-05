# Enrichment prompt versions

A released version is never edited. To change the prompt, copy `vN.py` to `vN+1.py`
and run `scripts/enrich.py --prompt-version vN+1`. Each entry in enriched.json
records the `prompt_version` it was made with.

## v1 (2026-11-05)

Models tried: gemma4:e4b, qwen3.5:4b (Ollama, MLX runner). Test set: the first 5 jobs of
runs/2026-11-04.

Findings that shaped v1, in the order they were found:
1. The MLX runner rejects `format=` (HTTP 501), so the JSON shape is requested in the prompt,
   parsed, validated with pydantic, and repaired once on failure.
2. Models invented "0 years" when the JD states no years, and listed soft skills and vague
   phrases as skills. Fixed in the prompt, and zero-year items are also dropped in code.
3. Language alternatives ("Python, Java, C# or Go") were listed as separate required skills.
   Prompt now asks for one "(any one)" item. Qwen follows this only some of the time.
4. The models' own score arithmetic was inconsistent with their reasoning. The score is now
   mostly computed in code from the skill lists (required 40, preferred 10, learnability 10);
   the model only gives experience_fit (0-20) and role_alignment (0-20).
5. Resume text from the PDF had kerning artifacts ("F AISS", "LLM T raining"). Fixed in
   `clean_resume_text`.
6. Qwen exaggerated experience ("1 year") from a 3 month internship. Prompt now says to count
   real duration.

Known weaknesses of v1: with temperature 0 the models are still noisy on borderline skills
("similar" vs "no"), tech stack lists with no required/preferred marking, and sometimes omit
the stated years of experience. Scores from the two models differ by up to 40 points on
ambiguous JDs. Treat scores as a rough ranking, not a precise number.

## v2 (2026-11-05)

Changes from v1, found by running v1 on 3 models (gemma4:e4b, qwen3.5:4b, qwen3.5:9b) on the first
5 jobs of runs/2026-11-04:
1. A degree, course or general title alone no longer counts as evidence of a specific skill
   (qwen3.5:9b marked object-oriented programming "yes" because of the degree).
2. The reasoning must not claim the candidate meets a years requirement the dated roles do not add up to.
3. An experience item is always added when the JD states a number of years.
4. Placeholders from the format example ("string") must not be copied; use "".
5. Optional text fields (resume_evidence, learn_note, learnable_in_a_day) default instead of failing
   validation (one gemma4:e4b reply failed because it left out resume_evidence).
6. score_breakdown has a "detail" block: the maximum of each part and, for skills, each skill's weight
   and points.

Tried and removed: asking the model for work authorization and clearance. The 9B model read location text
("remote anywhere in the US", "we hire globally") as an authorization requirement, and the small models
copied placeholders or misread export control text. These are now keyword flags in scripts/jd_flags.py,
stored as `jd_flags` on the job, independent of the model and the prompt version.

Known weaknesses of v2: scores are still noisy between models. qwen3.5:9b gives role_alignment 20 to
nearly every job, qwen3.5:4b sometimes inflates a score (95 for an entry level role it only half matches),
and 9B sometimes ignores the experience table.

v2 on all 25 jobs of runs/2026-11-04 (gemma4:e4b, qwen3.5:4b, qwen3.5:9b), 75 of 75 valid:

| model | repairs needed | avg seconds | score mean | score range | jobs at 70 or more |
|---|---|---|---|---|---|
| gemma4:e4b | 0 | 14 | 77 | 32 to 95 | 21 |
| qwen3.5:4b | 5 | 33 | 77 | 25 to 95 | 18 |
| qwen3.5:9b | 0 | 33 | 83 | 64 to 100 | 24 |

The scores of different models barely correlate (r between 0.09 and 0.19 per pair). qwen3.5:9b is the
most consistent but compresses scores into a narrow high band, so a cutoff of 70 on its score passes 24
of 25 jobs. As a ranking of jobs the score is weak; as a cutoff it does not separate jobs yet.

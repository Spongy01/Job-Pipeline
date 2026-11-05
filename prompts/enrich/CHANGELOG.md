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

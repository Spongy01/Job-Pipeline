"""Enrichment prompt, version v2 (v1 with stricter rules and fewer model errors).

Sponsorship, clearance and citizenship are NOT asked from the model: they are keyword flags
computed in scripts/jd_flags.py.

Do not edit a released version in place. Copy to v2.py and change the copy, so
old enriched.json entries (which record prompt_version) stay reproducible.

Contract used by scripts/enrich.py: VERSION, SYSTEM, USER_TEMPLATE, Output,
and score(output).
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

VERSION = "v2"

SYSTEM = """\
You are a strict, honest job-fit analyst. You read a candidate's resume and one job description (JD) and return a JSON object that follows the output format exactly.

GENERAL RULES
- Use "" for any text field that has nothing to say. Never write the word "string" or any other placeholder.
- Use only what is written in the JD and the resume. Never invent requirements or resume content.
- The resume is the single source of truth about the candidate. A skill is "in the resume" only if the resume names it or clearly shows it.
- Keep every text field short. No markdown. No filler.

summary
- 2 to 3 plain sentences about the JOB, not about the candidate: what the company does (if stated), what the person will do, and the seniority level.

experience
- Always add an item when the JD states a number of years ("at least 1 year", "3+ years"), even if the sentence is short. Add an item ONLY when the JD states a number of years of experience (for example "3+ years", "2-4 years of Python"). If the JD gives no number of years, return an empty list. Never output 0 years. Words like "early-career" or "senior" without a number are not years.
- area: "overall" for general experience, otherwise the specific skill or domain the years apply to.
- level: "required" for must-have wording, "preferred" for bonus / preferred / plus wording.
- years_min and years_max: one number N gives years_min = years_max = N ("3+" gives 3 and 3). A range "2-4" gives 2 and 4.
- evidence: a short quote from the JD that contains the number.

required_skills and preferred_skills
- Only specific technical skills: languages, frameworks, libraries, tools, platforms, techniques (for example "RAG", "fine-tuning") and concrete domains (for example "insurance"). Use the short name of the thing.
- Never list: soft skills (communication, attention to detail, judgment, ownership, teamwork, eagerness to learn), degrees, years of experience, work authorization, location, salary, or vague phrases such as "production", "software engineering", "scalable systems", "problem solving".
- required_skills: what the JD demands, including anything under headings like Requirements, Qualifications, What you need, What we are looking for, or in the duties when stated as a must.
- preferred_skills: only what the JD marks as preferred, bonus, plus, nice to have, or "exposure to".
- When the JD gives alternatives ("one of Python, Java, C# or Go", "such as Django, Flask or FastAPI", "AWS, GCP or Azure"), make ONE item that names the group, for example "Python/TypeScript/Java/C#/Go (any one)". It counts as "yes" if the resume has any one of them.
- A tech stack list ("Tech Stack", "Our stack", "You will work with") that is not marked preferred counts as required.
- Language lists such as "Rust, Python, TypeScript, Java, or similar" are alternatives: one item "Rust/Python/TypeScript/Java (any one)".
- A skill goes in exactly one list. If the JD mentions it as required anywhere, it is not preferred.
- Merge duplicates. At most 10 required and 8 preferred items. Keep the most important.

For each skill:
- skill: the skill as named in the JD.
- in_resume:
  - "yes": the resume names the skill, or clearly shows hands-on use of it in a project or job. A degree, a course or a general title alone is not evidence of a specific skill.
  - "similar": the skill is not in the resume, but the resume has a skill of the same kind that an employer would accept as transferable (JD says AWS and resume has GCP, JD says Django and resume has FastAPI). Do not stretch: Pydantic is not similar to SQLAlchemy, unit testing is not similar to CI/CD. When in doubt use "no".
  - "no": nothing in the resume is close.
- resume_evidence: the exact resume skill or project that supports "yes" or "similar". Empty string when "no".
- learnable_in_a_day: only for "similar" or "no". true if the candidate could reach basic working familiarity in about one day and then add it to the resume honestly and naturally (a library, a CLI tool, a cloud service, a testing tool, a framework close to one they know, a model API). false if it needs real time (a new programming language, a new domain, deep theory, certifications, years of practice, domain experience such as insurance or finance). null when in_resume is "yes".
- learn_note: only when learnable_in_a_day is true: one short sentence naming the specific resume project or skills line where it would honestly fit (for example "Add to the Conduit project, which already uses FastAPI"). Never a generic note. Otherwise an empty string.

SCORING RUBRIC (total 100)
The system computes three parts from your skill lists, so classify the skills carefully:
- required skills, 40 points: "yes" counts 1, "similar" counts 0.75, "no" but learnable in a day counts 0.5, "no" and not learnable counts 0.
- preferred skills, 10 points: same weights.
- learnability, 10 points: the share of gaps (skills that are not "yes") that are learnable in a day.
You give the other two parts as integers:
- experience_fit (0 to 20): compare the JD's required overall years with the candidate's experience. Take professional experience only from the dated roles in the Experience section and count the real duration (a 3 month internship is a quarter of a year, never "1 year"). Substantial projects count at a reduced rate.
  - JD asks 0 or 1 years, or is entry level or new grad: 17 to 20.
  - 2 years: 13 to 16.
  - 3 years: 9 to 12.
  - 4 or 5 years: 4 to 8.
  - 6 or more years, or a senior, staff, lead or principal title: 0 to 3.
  - No years stated and no seniority wording: 14.
- role_alignment (0 to 20): how well the day-to-day work and domain match the candidate's background. 20 for a direct match (LLM agents, backend APIs, RAG, fine-tuning, observability), 10 for adjacent software work, 0 for an unrelated role such as sales, support or operations.
- reasoning: 2 sentences that justify experience_fit and role_alignment. Never say the candidate meets or exceeds a years-of-experience requirement unless the dated roles on the resume add up to it.
"""

USER_TEMPLATE = """\
RESUME
<resume>
{resume}
</resume>

JOB DESCRIPTION
Title: {title}
Company: {company}
Location: {location}
Work model: {work_model}
<jd>
{description}
</jd>

{output_format}"""

OUTPUT_FORMAT = """\
OUTPUT FORMAT
Reply with one JSON object and nothing else: no markdown fences, no text before or after it. Use exactly these keys:
{
  "summary": "string",
  "experience": [
    {"area": "overall", "level": "required", "years_min": 0, "years_max": 0, "evidence": "string"}
  ],
  "required_skills": [
    {"skill": "string", "in_resume": "yes", "resume_evidence": "string", "learnable_in_a_day": null, "learn_note": "string"}
  ],
  "preferred_skills": [
    {"skill": "string", "in_resume": "similar", "resume_evidence": "string", "learnable_in_a_day": true, "learn_note": "string"}
  ],
  "scores": {"experience_fit": 0, "role_alignment": 0, "reasoning": "string"}
}
The example values only show the types. "level" is "required" or "preferred". "in_resume" is "yes", "similar" or "no". "learnable_in_a_day" is true, false or null. Lists may be empty. Numbers are plain numbers."""


class ExperienceItem(BaseModel):
    area: str
    level: Literal["required", "preferred"]
    years_min: float = Field(ge=0, le=40)
    years_max: float = Field(ge=0, le=40)
    evidence: str


class SkillMatch(BaseModel):
    skill: str
    in_resume: Literal["yes", "similar", "no"]
    resume_evidence: str = ""
    learnable_in_a_day: Optional[bool] = None
    learn_note: str = ""


class ModelScores(BaseModel):
    experience_fit: int = Field(ge=0, le=20)
    role_alignment: int = Field(ge=0, le=20)
    reasoning: str


class Output(BaseModel):
    summary: str
    experience: List[ExperienceItem]
    required_skills: List[SkillMatch]
    preferred_skills: List[SkillMatch]
    scores: ModelScores


SKILL_WEIGHTS = {"yes": 1.0, "similar": 0.75}  # "no": 0.5 if learnable in a day, else 0
REQUIRED_POINTS, PREFERRED_POINTS, LEARNABILITY_POINTS = 40, 10, 10


def _skill_weight(skill):
    if skill.in_resume in SKILL_WEIGHTS:
        return SKILL_WEIGHTS[skill.in_resume]
    return 0.5 if skill.learnable_in_a_day else 0.0


def _skills_points(skills, points, empty_value):
    if not skills:
        return empty_value
    return round(points * sum(_skill_weight(s) for s in skills) / len(skills), 1)


def _skill_items(skills, points):
    n = len(skills)
    return [{
        "skill": s.skill,
        "in_resume": s.in_resume,
        "learnable_in_a_day": s.learnable_in_a_day,
        "weight": _skill_weight(s),
        "points": round(points * _skill_weight(s) / n, 2),
    } for s in skills]


def score(output):
    """Return (total, breakdown). The skill parts are computed here, not by the model.

    breakdown has the five part scores, the model's reasoning, and "detail" with the
    maximum of every part and, for the skill parts, each skill's weight and points.
    """
    gaps = [s for s in output.required_skills + output.preferred_skills if s.in_resume != "yes"]
    learnable_gaps = sum(1 for s in gaps if s.learnable_in_a_day)
    learnability = (LEARNABILITY_POINTS if not gaps else round(LEARNABILITY_POINTS * learnable_gaps / len(gaps), 1))
    parts = {
        "required_skills": _skills_points(output.required_skills, REQUIRED_POINTS, REQUIRED_POINTS / 2),
        "preferred_skills": _skills_points(output.preferred_skills, PREFERRED_POINTS, PREFERRED_POINTS / 2),
        "learnability": learnability,
        "experience_fit": output.scores.experience_fit,
        "role_alignment": output.scores.role_alignment,
    }
    detail = {
        "required_skills": {"max": REQUIRED_POINTS, "empty_list_default": not output.required_skills,
                            "items": _skill_items(output.required_skills, REQUIRED_POINTS) if output.required_skills else []},
        "preferred_skills": {"max": PREFERRED_POINTS, "empty_list_default": not output.preferred_skills,
                             "items": _skill_items(output.preferred_skills, PREFERRED_POINTS) if output.preferred_skills else []},
        "learnability": {"max": LEARNABILITY_POINTS, "gaps": len(gaps), "learnable_gaps": learnable_gaps},
        "experience_fit": {"max": 20, "set_by": "model"},
        "role_alignment": {"max": 20, "set_by": "model"},
    }
    breakdown = {**parts, "reasoning": output.scores.reasoning, "detail": detail}
    return round(sum(parts.values())), breakdown


def postprocess(output):
    """Deterministic cleanup of things small models keep getting wrong."""
    # years of experience can never be 0: the model invents these when the JD states none
    output.experience = [e for e in output.experience if e.years_max > 0]
    # a skill belongs in one list: keep the required one
    required = {s.skill.strip().lower() for s in output.required_skills}
    output.preferred_skills = [s for s in output.preferred_skills if s.skill.strip().lower() not in required]
    return output

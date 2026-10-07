import anthropic
from pydantic import BaseModel

MODEL = "claude-haiku-5-5"

# Jobs scoring below this are moved to Rejected automatically (still reviewable there).
AUTO_REJECT_BELOW = 70

client = anthropic.Anthropic()


class FitScore(BaseModel):
    fit_score: int
    summary: str
    strengths: list[str]
    gaps: list[str]
    dealbreakers: list[str]


RUBRIC = """You screen job postings for one specific job seeker, scoring how well each \
posting fits them so they only spend time on the best matches.

Score fit from 0 to 100:
- 85-100: Strong match. Their experience clearly covers the core responsibilities and \
most stated requirements, at the right level, and nothing conflicts with their preferences.
- 65-84: Good match. Covers the core of the role with a few gaps they could credibly \
bridge; worth applying.
- 40-64: Partial match. Meaningful gaps in required experience, domain, or level; a \
stretch application at best.
- 0-39: Poor match. The role is really a different function, needs experience they \
don't have, is clearly the wrong level, or hits one of their dealbreakers.

Judge the substance of the role, not keyword overlap -- a "Technical Program Manager" \
title can hide a hands-on engineering or sales role. Weigh required qualifications more \
heavily than nice-to-haves. Treat location, remote policy, seniority, and salary \
conflicts with their stated preferences as dealbreakers only when the posting is \
explicit about them. When a posting is vague, score on what it does say and note the \
uncertainty in a gap rather than guessing.

Respond with:
- fit_score: the integer score
- summary: one sentence on the overall fit
- strengths: up to 4 short phrases on where their experience matches
- gaps: up to 4 short phrases on missing or weak requirements
- dealbreakers: explicit conflicts with their preferences or eligibility (often empty)"""


def _system_prompt(resume_text: str, preferences: str | None) -> str:
    return f"""{RUBRIC}

<job_seeker_preferences>
{preferences or "(none stated)"}
</job_seeker_preferences>

<resume>
{resume_text}
</resume>"""


def score_job(resume_text: str, preferences: str | None, job: dict) -> FitScore:
    # The system prompt is identical for every job in a scoring run, so it's the
    # cached prefix; only the job posting in the user turn changes per request.
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": "low"},
        system=[
            {
                "type": "text",
                "text": _system_prompt(resume_text, preferences),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"""Score this job posting:

Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Remote: {job.get("is_remote")}
Salary: {job.get("min_amount")} - {job.get("max_amount")} {job.get("currency") or ""}
Description:
{job.get("description") or "(no description available)"}""",
            }
        ],
        output_format=FitScore,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"No score returned (stop_reason={response.stop_reason})")
    return response.parsed_output

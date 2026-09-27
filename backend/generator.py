import anthropic

MODEL = "claude-sonnet-5"

client = anthropic.Anthropic()


def _complete(prompt: str) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def generate_resume(resume_text: str, job: dict) -> str:
    prompt = f"""You are helping a job seeker tailor their resume for a specific job posting.

Here is their current resume:
---
{resume_text}
---

Here is the job posting to tailor the resume for:
Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Description:
{job.get("description") or "(no description available)"}

Rewrite the resume, tailored to this specific job. Stay strictly truthful to the \
original resume's real experience, employers, titles, and dates -- never invent \
anything. Reorder, re-emphasize, and rephrase existing bullet points to foreground \
the experience most relevant to this job, and mirror the job posting's key \
terminology where it genuinely matches the candidate's real experience.

Output only the final resume in clean Markdown, with no commentary before or after."""
    return _complete(prompt)


def generate_cover_letter(resume_text: str, job: dict) -> str:
    prompt = f"""You are helping a job seeker write a cover letter for a specific job posting.

Here is their resume:
---
{resume_text}
---

Here is the job posting:
Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Description:
{job.get("description") or "(no description available)"}

Write a concise, specific cover letter (under 400 words) connecting the candidate's \
real experience from the resume to this job's stated requirements. Stay strictly \
truthful to the resume -- never invent experience. Use a generic greeting such as \
"Dear Hiring Team," if no contact name is available, and don't leave any \
placeholder brackets unfilled.

Output only the final cover letter in clean Markdown, with no commentary before or after."""
    return _complete(prompt)

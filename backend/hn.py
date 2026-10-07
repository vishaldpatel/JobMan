"""Job postings from a Hacker News "Who is hiring?" thread.

Each top-level comment is a free-form posting that can list several roles, so
Claude extracts the structured fields; the rest of the pipeline (Stage 1
filters, scoring) then treats them like any other scraped job.
"""

import html
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import anthropic
import httpx
import pandas as pd
from pydantic import BaseModel

MODEL = "claude-haiku-5-5"
ITEM_API = "https://hn.algolia.com/api/v1/items/{id}"
COMMENT_URL = "https://news.ycombinator.com/item?id={id}"

# Cheap keyword screen on the raw comment before paying Claude to extract it --
# a big thread has 500+ postings, most of them engineering-only.
COMMENT_PATTERN = r"\b(?:pro(?:ject|duct|gram)\s+manag|TPMs?\b|PMs?\b)"
# Of the roles Claude extracts, only these titles are kept, since one posting
# often lists engineering roles alongside the one you want.
TITLE_PATTERN = r"pro(?:ject|duct|gram)\s+manag|\bTPM\b"
EXTRACT_WORKERS = 8
COLUMNS = [
    "id", "site", "job_url", "job_url_direct", "title", "company", "location",
    "date_posted", "is_remote", "min_amount", "max_amount", "currency", "description",
]

client = anthropic.Anthropic()


class Posting(BaseModel):
    title: str
    company: str
    location: str | None
    is_remote: bool | None
    min_salary: int | None
    max_salary: int | None
    apply_url: str | None


class Postings(BaseModel):
    postings: list[Posting]


EXTRACT_PROMPT = """Below is a comment from a Hacker News "Who is hiring?" thread. \
List every distinct job role it advertises, one entry per role:
- title: the role's job title as written
- company: the hiring company
- location: where the role is based as written (e.g. "San Diego, CA", \
"Remote (US)"), or null if not stated
- is_remote: true if the role can be done fully remotely, false if it can't, \
null if not stated
- min_salary / max_salary: the yearly salary range in whole dollars (e.g. 150000), \
or null if not stated or not yearly; a single figure goes in both
- apply_url: the URL for applying to or reading about this role, or null if none

If the comment isn't a job posting (a question, a reply, a "seeking work" post), \
return an empty list.

<comment>
{text}
</comment>"""


def thread_id(url_or_id: str) -> str:
    """Accepts a news.ycombinator.com item link or a bare item id."""
    match = re.search(r"(?:[?&]id=)?(\d+)\s*$", url_or_id.strip())
    if not match:
        raise ValueError(f"Not a Hacker News item link: {url_or_id}")
    return match.group(1)


def comment_text(raw_html: str) -> str:
    """HN comment HTML -> plain text. Link text is shown truncated ("..."), so
    links are replaced with their full href."""
    text = re.sub(r'<a [^>]*href="([^"]*)"[^>]*>.*?</a>', r"\1", raw_html, flags=re.S)
    text = re.sub(r"<p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _extract(text: str) -> list[Posting]:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=8192,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
        output_format=Postings,
    )
    if response.parsed_output is None:
        return []
    return response.parsed_output.postings


def _rows(comment: dict, text: str, postings: list[Posting]) -> pd.DataFrame:
    """One comment's matching roles, with the JobSpy column names the rest of
    the app expects."""
    posted = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
    rows = []
    for n, posting in enumerate(postings):
        if not re.search(TITLE_PATTERN, posting.title, re.I):
            continue
        rows.append(
            {
                # Rescanning a thread can number a comment's roles differently;
                # the title+description dedupe key still catches those repeats.
                "id": f"hn-{comment['id']}-{n}",
                "site": "hackernews",
                "job_url": COMMENT_URL.format(id=comment["id"]),
                "job_url_direct": posting.apply_url,
                "title": posting.title,
                "company": posting.company,
                "location": posting.location,
                "date_posted": posted.date().isoformat(),
                "is_remote": posting.is_remote,
                "min_amount": posting.min_salary,
                "max_amount": posting.max_salary,
                "currency": "USD" if posting.max_salary else None,
                "description": text,
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def scrape_hn_thread(url_or_id: str) -> Iterator[pd.DataFrame]:
    """Yields the matching roles posted in the thread's top-level comments, one
    comment at a time as each extraction finishes, so they can be stored and
    scored without waiting for the rest of the thread."""
    response = httpx.get(ITEM_API.format(id=thread_id(url_or_id)), timeout=60)
    response.raise_for_status()
    comments = [
        c
        for c in response.json().get("children", [])
        if c.get("text") and re.search(COMMENT_PATTERN, c["text"], re.I)
    ]

    with ThreadPoolExecutor(EXTRACT_WORKERS) as pool:
        futures = {}
        for comment in comments:
            text = comment_text(comment["text"])
            futures[pool.submit(_extract, text)] = (comment, text)
        for future in as_completed(futures):
            comment, text = futures[future]
            yield _rows(comment, text, future.result())

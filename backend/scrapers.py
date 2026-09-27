import csv
import re
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
from ats_scrapers import Client as AtsClient
from jobspy import scrape_jobs

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
SEARCH_TERM = "Technical Project Manager or Technical Product Manager, on-site or hybrid or remote in San Diego County, CA or Carlsbad, CA or Hawaii, United States"
LOCATION = "San Diego County, CA"
HOURS_OLD = 24

# ats-scrapers' hosted dataset is a daily snapshot whose newest postings are
# already ~1 day old when published, so a 24h window usually comes back empty.
ATS_HOURS_OLD = 48
ATS_TITLE_PATTERN = r"Technical Pro(?:ject|duct|gram) Manager"
ATS_LOCATION_PATTERN = r"San Diego|Carlsbad|Hawaii|Honolulu|Remote"

# Stage 1 filters, applied to every scrape (LinkedIn included) before a job is
# stored or sent to Claude for scoring. All patterns are case-insensitive.
# "Associate" alone is junior, but "Associate (Engineering) Director" is senior.
EXCLUDE_TITLE_PATTERN = (
    r"\b(?:Intern|Internship|Junior|Jr\.?|VP|Vice President|Associate(?!\s+(?:\w+\s+)?Director))\b"
)
# A location naming another country is dropped unless it also names the US,
# so "Remote - US or Canada" survives but "Canada Remote" doesn't.
NON_US_LOCATION_PATTERN = (
    r"Canada|Mexico|Brazil|Argentina|Colombia|United Kingdom|\bUK\b|Ireland|Germany|"
    r"France|Spain|Portugal|Netherlands|Poland|Romania|India|Philippines|Singapore|"
    r"Australia|Japan|Israel|Europe|EMEA|APAC|LATAM"
)
US_LOCATION_PATTERN = r"United States|\bUSA?\b|\bU\.S\.|California|\bCA\b|Hawaii|\bHI\b"
EXCLUDE_DESCRIPTION_PATTERN = (
    r"TS/SCI|active (?:TS|secret|top secret|security) clearance|"
    r"security clearance (?:is )?required"
)
# Only applied when a posting lists a yearly salary; unlisted salaries pass.
MIN_SALARY = 130_000

WORKDAY_URL_PATTERN = re.compile(
    r"^https://(?P<tenant>[^.]+)\.(?P<instance>wd\d+)\.myworkdayjobs\.com/(?P<site>[^/?#]+)(?P<path>/job/.*)$"
)

def scrape_daily_jobs(
    search_term: str = SEARCH_TERM,
    location: str = LOCATION,
    hours_old: int = HOURS_OLD,
    results_wanted: int = 200,
):
    """Scrape jobs posted within the last `hours_old` hours across supported sites."""
    jobs = scrape_jobs(
        site_name=["linkedin"],
        search_term=search_term,
        google_search_term=f"{search_term} jobs near {location} since yesterday",
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        country_indeed="USA",
        linkedin_fetch_description=True,
    )
    return jobs


def _contains(series: pd.Series, pattern: str) -> pd.Series:
    return series.fillna("").astype(str).str.contains(pattern, case=False, regex=True)


def apply_prefilters(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drops jobs that are obviously not a fit before they're stored or scored.
    Returns the kept jobs and how many were dropped."""
    location = df["location"] if "location" in df else pd.Series("", index=df.index)
    description = df["description"] if "description" in df else pd.Series("", index=df.index)

    # Hourly/monthly LinkedIn salaries would look tiny next to a yearly floor.
    yearly = df["interval"].isna() | (df["interval"] == "yearly") if "interval" in df else True
    max_amount = pd.to_numeric(df.get("max_amount"), errors="coerce")
    underpaid = yearly & max_amount.notna() & (max_amount < MIN_SALARY)

    drop = (
        _contains(df["title"], EXCLUDE_TITLE_PATTERN)
        | (_contains(location, NON_US_LOCATION_PATTERN) & ~_contains(location, US_LOCATION_PATTERN))
        | _contains(description, EXCLUDE_DESCRIPTION_PATTERN)
        | underpaid
    )
    return df[~drop], int(drop.sum())


def _matches_search(df: pd.DataFrame, title_pattern: str, location_pattern: str) -> pd.Series:
    return df["title"].str.contains(title_pattern, case=False, na=False) & df[
        "location"
    ].str.contains(location_pattern, case=False, na=False)


def _scrape_dated_ats(
    client: AtsClient,
    ats: str,
    make_id,
    title_pattern: str,
    location_pattern: str,
    hours_old: int,
) -> pd.DataFrame:
    """Pull recent postings for one ATS whose hosted-dataset rows carry
    posted_at, renamed to the JobSpy column names the rest of the app expects.
    `make_id` maps the matching rows to stable job ids."""
    df = client.load(ats=ats)

    # ISO8601: iCIMS mixes offset and naive timestamps, which the default
    # parser silently turns into NaT for a large share of rows.
    posted_at = pd.to_datetime(df["posted_at"], utc=True, errors="coerce", format="ISO8601")
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours_old)
    matches = _matches_search(df, title_pattern, location_pattern) & (posted_at >= cutoff)
    df = df[matches]

    return pd.DataFrame(
        {
            "id": make_id(df),
            "site": ats,
            "job_url": df["url"],
            "title": df["title"],
            "company": df["company"],
            "location": df["location"],
            "date_posted": posted_at[matches].dt.date.astype(str),
            "is_remote": df["is_remote"],
            "min_amount": pd.to_numeric(df["salary_min"], errors="coerce"),
            "max_amount": pd.to_numeric(df["salary_max"], errors="coerce"),
            "currency": df["salary_currency"],
            "department": df["department"],
            "description": df["description"],
        }
    )


def scrape_greenhouse_jobs(
    client: AtsClient,
    title_pattern: str = ATS_TITLE_PATTERN,
    location_pattern: str = ATS_LOCATION_PATTERN,
    hours_old: int = ATS_HOURS_OLD,
) -> pd.DataFrame:
    return _scrape_dated_ats(
        client,
        "greenhouse",
        lambda df: "gh-" + df["ats_id"].astype(str),
        title_pattern,
        location_pattern,
        hours_old,
    )


def scrape_icims_jobs(
    client: AtsClient,
    title_pattern: str = ATS_TITLE_PATTERN,
    location_pattern: str = ATS_LOCATION_PATTERN,
    hours_old: int = ATS_HOURS_OLD,
) -> pd.DataFrame:
    # ats_id is only unique within a tenant (careers-sas.icims.com -> careers-sas).
    return _scrape_dated_ats(
        client,
        "icims",
        lambda df: "ic-"
        + df["url"].str.extract(r"^https://([^.]+)\.icims\.com", expand=False)
        + "-"
        + df["ats_id"].astype(str),
        title_pattern,
        location_pattern,
        hours_old,
    )


def _workday_start_date(http: httpx.Client, job_url: str) -> str | None:
    """The hosted dataset has no posting date for Workday, so ask the tenant's
    job-detail endpoint -- its `startDate` is the posting date (YYYY-MM-DD)."""
    m = WORKDAY_URL_PATTERN.match(job_url)
    if not m:
        return None
    detail_url = (
        f"https://{m['tenant']}.{m['instance']}.myworkdayjobs.com"
        f"/wday/cxs/{m['tenant']}/{m['site']}{m['path']}"
    )
    try:
        response = http.get(detail_url)
        response.raise_for_status()
        return response.json().get("jobPostingInfo", {}).get("startDate")
    except (httpx.HTTPError, ValueError):
        return None  # posting taken down or tenant unreachable -- skip it


def scrape_workday_jobs(
    client: AtsClient,
    title_pattern: str = ATS_TITLE_PATTERN,
    location_pattern: str = ATS_LOCATION_PATTERN,
    hours_old: int = ATS_HOURS_OLD,
) -> pd.DataFrame:
    """Like scrape_greenhouse_jobs, but posting dates come from one live
    request per title/location match, since the dataset doesn't carry them."""
    df = client.load(ats="workday")
    df = df[_matches_search(df, title_pattern, location_pattern)]

    with httpx.Client(
        timeout=20, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    ) as http:
        start_dates = df["url"].map(lambda url: _workday_start_date(http, url))

    # startDate has day granularity only, so compare whole days.
    cutoff = (pd.Timestamp.now() - pd.Timedelta(hours=hours_old)).date()
    recent = start_dates.map(lambda d: d is not None and date.fromisoformat(d) >= cutoff)
    df = df[recent]

    return pd.DataFrame(
        {
            # ats_id is only unique within a tenant, so prefix the tenant.
            "id": "wd-"
            + df["url"].str.extract(WORKDAY_URL_PATTERN)["tenant"]
            + "-"
            + df["ats_id"].astype(str),
            "site": "workday",
            "job_url": df["url"],
            "title": df["title"],
            "company": df["company"],
            "location": df["location"],
            "date_posted": start_dates[recent],
            "is_remote": df["is_remote"],
            "min_amount": df["salary_min"],
            "max_amount": df["salary_max"],
            "currency": df["salary_currency"],
            "job_type": df["employment_type"],
            "description": df["description"],
        }
    )


def scrape_ats_jobs() -> pd.DataFrame:
    with AtsClient() as client:
        return pd.concat(
            [
                scrape_greenhouse_jobs(client),
                scrape_workday_jobs(client),
                scrape_icims_jobs(client),
            ],
            ignore_index=True,
        )


if __name__ == "__main__":
    jobs = scrape_daily_jobs()
    print(f"Found {len(jobs)} jobs")
    print(jobs.head())

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs.to_csv(
        OUTPUT_DIR / "jobs.csv",
        quoting=csv.QUOTE_NONNUMERIC,
        escapechar="\\",
        index=False,
    )

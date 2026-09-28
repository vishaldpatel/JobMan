import csv
import hashlib
import re
from pathlib import Path

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from ats_scrapers.manifest import DEFAULT_MANIFEST_URL
from jobspy import scrape_jobs

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
ATS_CACHE_DIR = OUTPUT_DIR / "ats_cache"
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


class AtsDataset:
    """Reads ats-scrapers' hosted per-ATS Parquet files through an on-disk cache.

    The hosted dataset is republished once a day, so every scrape after the
    first on a given day would otherwise re-download identical bytes (~130 MB
    for Greenhouse). Files are keyed by the manifest's sha256: a new
    snapshot has a new hash, so it's fetched once and replaces the old file.
    """

    def __init__(self, http: httpx.Client):
        self.http = http
        response = http.get(DEFAULT_MANIFEST_URL)
        response.raise_for_status()
        self.manifest = response.json()

    def _parquet_path(self, ats: str) -> Path:
        entry = self.manifest["by_ats"][ats]
        sha256 = entry["parquet_sha256"]
        path = ATS_CACHE_DIR / f"{ats}-{sha256[:16]}.parquet"
        if path.exists():
            return path

        ATS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        digest = hashlib.sha256()
        with self.http.stream("GET", entry["parquet"]) as response, partial.open("wb") as f:
            response.raise_for_status()
            for chunk in response.iter_bytes(1 << 20):
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != sha256:
            partial.unlink()
            raise RuntimeError(f"Checksum mismatch downloading the {ats} dataset")

        for stale in ATS_CACHE_DIR.glob(f"{ats}-*.parquet"):
            stale.unlink()
        partial.rename(path)
        return path

    def load(self, ats: str, title_pattern: str) -> pd.DataFrame:
        """Returns only rows whose title matches. Row groups are read one at a
        time and skipped entirely when no title matches, since descriptions make
        up most of each file and would otherwise all be decompressed at once
        (Greenhouse's are ~950 MB uncompressed)."""
        parquet = pq.ParquetFile(self._parquet_path(ats))
        matching = []
        for i in range(parquet.num_row_groups):
            titles = parquet.read_row_group(i, columns=["title"]).column("title")
            mask = pc.match_substring_regex(titles, pattern=title_pattern, ignore_case=True)
            if pc.any(mask).as_py():
                matching.append(parquet.read_row_group(i).filter(mask))
        if not matching:
            return parquet.schema_arrow.empty_table().to_pandas()
        return pa.concat_tables(matching).to_pandas()


def _contains(series: pd.Series, pattern: str) -> pd.Series:
    return series.fillna("").astype(str).str.contains(pattern, case=False, regex=True)


def apply_prefilters(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drops jobs that are obviously not a fit before they're stored or scored.
    Returns the kept jobs and how many were dropped."""
    location = df["location"] if "location" in df else pd.Series("", index=df.index)
    description = df["description"] if "description" in df else pd.Series("", index=df.index)

    # Hourly/monthly LinkedIn salaries would look tiny next to a yearly floor.
    yearly = df["interval"].isna() | (df["interval"] == "yearly") if "interval" in df else True
    max_amount = pd.to_numeric(df.get("max_amount", pd.Series(index=df.index)), errors="coerce")
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
    dataset: AtsDataset,
    ats: str,
    make_id,
    title_pattern: str,
    location_pattern: str,
    hours_old: int,
) -> pd.DataFrame:
    """Pull recent postings for one ATS whose hosted-dataset rows carry
    posted_at, renamed to the JobSpy column names the rest of the app expects.
    `make_id` maps the matching rows to stable job ids."""
    df = dataset.load(ats, title_pattern)

    # ISO8601: some sources mix offset and naive timestamps, which the
    # default parser silently turns into NaT.
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
    dataset: AtsDataset,
    title_pattern: str = ATS_TITLE_PATTERN,
    location_pattern: str = ATS_LOCATION_PATTERN,
    hours_old: int = ATS_HOURS_OLD,
) -> pd.DataFrame:
    return _scrape_dated_ats(
        dataset,
        "greenhouse",
        lambda df: "gh-" + df["ats_id"].astype(str),
        title_pattern,
        location_pattern,
        hours_old,
    )


def scrape_ats_jobs() -> pd.DataFrame:
    with httpx.Client(timeout=120, follow_redirects=True) as http:
        return scrape_greenhouse_jobs(AtsDataset(http))


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

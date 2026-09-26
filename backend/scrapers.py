import csv
from pathlib import Path

from jobspy import scrape_jobs

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
SEARCH_TERM = "Technical Project Manager or Technical Product Manager, on-site or hybrid or remote in San Diego County, CA or Carlsbad, CA or Hawaii, United States"
LOCATION = "San Diego County, CA"
HOURS_OLD = 24


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

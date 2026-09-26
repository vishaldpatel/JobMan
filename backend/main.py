from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scrapers import scrape_daily_jobs

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JOBS_CSV = DATA_DIR / "jobs.csv"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="JobMan")


def _jobs_from_df(df: pd.DataFrame) -> list[dict]:
    # astype(object) first: on a numeric column, pandas casts None from
    # .where() back into NaN to preserve dtype, which json.dumps rejects.
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


@app.post("/api/scrape")
def trigger_scrape():
    jobs_df = scrape_daily_jobs()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    jobs_df.to_csv(JOBS_CSV, index=False)

    jobs = _jobs_from_df(jobs_df)
    return {"count": len(jobs), "jobs": jobs}


@app.get("/api/jobs")
def get_jobs():
    if not JOBS_CSV.exists():
        return {"count": 0, "jobs": []}

    jobs_df = pd.read_csv(JOBS_CSV)
    jobs = _jobs_from_df(jobs_df)
    return {"count": len(jobs), "jobs": jobs}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

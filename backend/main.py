import queue
import threading
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
from generator import generate_cover_letter, generate_resume
from scrapers import scrape_daily_jobs

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

store.DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="JobMan")

generation_queue: "queue.Queue[str]" = queue.Queue()


class JobIds(BaseModel):
    ids: list[str]


def _jobs_from_df(df: pd.DataFrame) -> list[dict]:
    # astype(object) first: on a numeric column, pandas casts None from
    # .where() back into NaN to preserve dtype, which json.dumps rejects.
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


def _find_job(jobs: list[dict], job_id: str) -> dict | None:
    return next((job for job in jobs if job["id"] == job_id), None)


def _generate_documents(job_id: str) -> None:
    """Runs the (slow) API calls outside the lock, then records the result."""
    resume_text = store.read_resume()
    if not resume_text:
        return

    with store.LOCK:
        job_snapshot = _find_job(store.load_jobs(), job_id)
    if job_snapshot is None or job_snapshot["application_status"] == "resume_created":
        return  # already done -- avoids duplicate work if a job got queued twice

    resume_md = generate_resume(resume_text, job_snapshot)
    cover_letter_md = generate_cover_letter(resume_text, job_snapshot)

    app_dir = store.APPLICATIONS_DIR / job_id
    app_dir.mkdir(parents=True, exist_ok=True)
    resume_path = app_dir / "resume.md"
    cover_letter_path = app_dir / "cover_letter.md"
    resume_path.write_text(resume_md)
    cover_letter_path.write_text(cover_letter_md)

    with store.LOCK:
        jobs = store.load_jobs()
        job = _find_job(jobs, job_id)
        if job is not None:
            job["resume_path"] = str(resume_path.relative_to(store.DATA_DIR))
            job["cover_letter_path"] = str(cover_letter_path.relative_to(store.DATA_DIR))
            job["application_status"] = "resume_created"
            job.pop("generation_error", None)
        store.save_jobs(jobs)


def _generation_worker() -> None:
    while True:
        job_id = generation_queue.get()
        try:
            _generate_documents(job_id)
        except Exception as exc:  # keep the worker alive across bad jobs/network blips
            with store.LOCK:
                jobs = store.load_jobs()
                job = _find_job(jobs, job_id)
                if job is not None:
                    job["application_status"] = "error"
                    job["generation_error"] = str(exc)
                store.save_jobs(jobs)
        finally:
            generation_queue.task_done()


def _enqueue_pending_jobs() -> None:
    """Enqueues every applied job still awaiting generation -- used on server
    start (resuming jobs left mid-flight by a previous run) and right after a
    resume is uploaded (jobs that already gave up because no resume existed
    yet won't retry themselves otherwise)."""
    with store.LOCK:
        jobs = store.load_jobs()
    for job in jobs:
        if job["status"] == "applied" and job["application_status"] in ("started", "error"):
            generation_queue.put(job["id"])


threading.Thread(target=_generation_worker, daemon=True).start()
_enqueue_pending_jobs()


@app.post("/api/scrape")
def trigger_scrape():
    jobs_df = scrape_daily_jobs()
    scraped = _jobs_from_df(jobs_df)

    with store.LOCK:
        jobs = store.load_jobs()
        jobs = store.merge_scraped_jobs(jobs, scraped)
        store.save_jobs(jobs)

    new_jobs = [job for job in jobs if job["status"] == "new"]
    return {"count": len(new_jobs), "jobs": new_jobs}


@app.get("/api/jobs")
def get_jobs(status: str = "new"):
    with store.LOCK:
        jobs = [job for job in store.load_jobs() if job["status"] == status]
    return {"count": len(jobs), "jobs": jobs}


@app.post("/api/jobs/reject")
def reject_jobs(body: JobIds):
    ids = set(body.ids)
    with store.LOCK:
        jobs = store.load_jobs()
        for job in jobs:
            if job["id"] in ids:
                job["status"] = "rejected"
        store.save_jobs(jobs)
    return {"rejected": len(ids)}


@app.post("/api/jobs/apply")
def apply_to_jobs(body: JobIds):
    ids = set(body.ids)
    with store.LOCK:
        jobs = store.load_jobs()
        selected = [job for job in jobs if job["id"] in ids]
        for job in selected:
            job["status"] = "applied"
            job["application_status"] = "started"
            job.pop("generation_error", None)
        store.save_jobs(jobs)

    if store.has_resume():
        for job in selected:
            generation_queue.put(job["id"])

    return {"applied": len(selected), "jobs": selected}


@app.post("/api/jobs/retry")
def retry_jobs(body: JobIds):
    ids = set(body.ids)
    with store.LOCK:
        jobs = store.load_jobs()
        selected = [job for job in jobs if job["id"] in ids]
        for job in selected:
            job["application_status"] = "started"
            job.pop("generation_error", None)
        store.save_jobs(jobs)

    if store.has_resume():
        for job in selected:
            generation_queue.put(job["id"])

    return {"retried": len(selected)}


@app.post("/api/resume")
async def upload_resume(file: UploadFile):
    content = (await file.read()).decode("utf-8")
    store.save_resume(content)
    _enqueue_pending_jobs()
    return {"uploaded": True, "filename": file.filename, "length": len(content)}


@app.get("/api/resume")
def get_resume_status():
    resume_text = store.read_resume()
    return {"uploaded": resume_text is not None, "text": resume_text}


app.mount("/files", StaticFiles(directory=store.DATA_DIR), name="files")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

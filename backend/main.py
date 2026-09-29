import asyncio
import queue
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
from generator import generate_cover_letter, generate_resume
from pdf import markdown_to_pdf
from scorer import AUTO_REJECT_BELOW, score_job
from scrapers import apply_prefilters, scrape_ats_jobs, scrape_daily_jobs

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Rejected jobs are permanently deleted this long after being rejected.
DELETE_REJECTED_AFTER = timedelta(hours=48)
# Deleted jobs are remembered this long so scrapes don't re-add them; scrapes
# only look back a couple of days, so this is generous.
FORGET_DELETED_AFTER = timedelta(days=30)
HOUSEKEEPING_INTERVAL_SECONDS = 600

store.DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="JobMan")

generation_queue: "queue.Queue[str]" = queue.Queue()
scoring_queue: "queue.Queue[str]" = queue.Queue()

# Pushes "something changed, refetch" over WebSocket instead of the frontend
# polling. The generation worker runs on a plain thread, not the asyncio event
# loop, so it hops onto the loop via run_coroutine_threadsafe.
_ws_clients: set[WebSocket] = set()
_ws_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_event_loop():
    global _ws_loop
    _ws_loop = asyncio.get_running_loop()


async def _broadcast_jobs_changed():
    for ws in list(_ws_clients):
        try:
            await ws.send_json({"type": "jobs_changed"})
        except Exception:
            _ws_clients.discard(ws)


def notify_jobs_changed():
    if _ws_loop is not None:
        asyncio.run_coroutine_threadsafe(_broadcast_jobs_changed(), _ws_loop)


@app.websocket("/ws/jobs")
async def jobs_ws(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


class JobIds(BaseModel):
    ids: list[str]


class Profile(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    github_url: str | None = None
    citizenship_status: str | None = None
    requires_sponsorship: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    gender: str | None = None
    ethnicity: str | None = None
    desired_salary: str | None = None
    earliest_start_date: str | None = None
    notice_period: str | None = None
    job_preferences: str | None = None


def _jobs_from_df(df: pd.DataFrame) -> list[dict]:
    # JobSpy returns date_posted as datetime.date, which json.dumps rejects.
    # Converted before the None fill, since map() re-infers dtypes and would
    # turn None back into NaN.
    df = df.map(lambda v: v.isoformat() if isinstance(v, date) and pd.notnull(v) else v)
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
    resume_path = app_dir / "resume.pdf"
    cover_letter_path = app_dir / "cover_letter.pdf"
    resume_path.write_bytes(markdown_to_pdf(resume_md))
    cover_letter_path.write_bytes(markdown_to_pdf(cover_letter_md))

    with store.LOCK:
        jobs = store.load_jobs()
        job = _find_job(jobs, job_id)
        if job is not None:
            job["resume_path"] = str(resume_path.relative_to(store.DATA_DIR))
            job["cover_letter_path"] = str(cover_letter_path.relative_to(store.DATA_DIR))
            job["application_status"] = "resume_created"
            job.pop("generation_error", None)
        store.save_jobs(jobs)
    notify_jobs_changed()


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
            notify_jobs_changed()
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


def _score(job_id: str) -> None:
    """Rates one job against the resume; jobs below AUTO_REJECT_BELOW go
    straight to Rejected (flagged auto_rejected so you can tell them apart)."""
    resume_text = store.read_resume()
    if not resume_text:
        return

    with store.LOCK:
        job_snapshot = _find_job(store.load_jobs(), job_id)
    if job_snapshot is None or job_snapshot.get("fit_score") is not None:
        return  # already scored -- avoids duplicate work if a job got queued twice

    preferences = store.load_profile().get("job_preferences")
    try:
        fit = score_job(resume_text, preferences, job_snapshot)
    except Exception as exc:  # keep the worker alive across bad jobs/network blips
        updates = {"fit_error": str(exc)}
    else:
        updates = {
            "fit_score": fit.fit_score,
            "fit_summary": fit.summary,
            "fit_strengths": fit.strengths,
            "fit_gaps": fit.gaps,
            "fit_dealbreakers": fit.dealbreakers,
            "fit_error": None,
        }

    with store.LOCK:
        jobs = store.load_jobs()
        job = _find_job(jobs, job_id)
        if job is not None:
            job.update(updates)
            score = job.get("fit_score")
            if job["status"] == "new" and score is not None and score < AUTO_REJECT_BELOW:
                _reject(job, auto=True)
        store.save_jobs(jobs)
    notify_jobs_changed()


def _scoring_worker() -> None:
    while True:
        job_id = scoring_queue.get()
        try:
            _score(job_id)
        finally:
            scoring_queue.task_done()


def _enqueue_unscored_jobs() -> None:
    """Enqueues every new job without a score -- used after a scrape, on server
    start (resuming a run cut short by a restart), and after a resume upload."""
    with store.LOCK:
        jobs = store.load_jobs()
    for job in jobs:
        if job["status"] == "new" and job.get("fit_score") is None:
            scoring_queue.put(job["id"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _reject(job: dict, auto: bool = False) -> None:
    job["status"] = "rejected"
    job["rejected_at"] = _now()
    if auto:
        job["auto_rejected"] = True


def _housekeep() -> None:
    """Applies the time- and threshold-based moves:
    - new jobs scoring under AUTO_REJECT_BELOW -> Rejected (catches jobs scored
      before the threshold was raised)
    - rejected jobs older than DELETE_REJECTED_AFTER -> permanently deleted
    - jobs marked as applied before today -> the Applied tab (status "submitted")
    """
    now = datetime.now()
    changed = False
    with store.LOCK:
        jobs = store.load_jobs()
        deleted = store.load_deleted()
        kept = []
        for job in jobs:
            score = job.get("fit_score")
            if job["status"] == "new" and score is not None and score < AUTO_REJECT_BELOW:
                _reject(job, auto=True)
                changed = True

            if job["status"] == "rejected":
                if not job.get("rejected_at"):
                    # Rejected before timestamps existed: start the clock now.
                    job["rejected_at"] = _now()
                    changed = True
                elif now - datetime.fromisoformat(job["rejected_at"]) > DELETE_REJECTED_AFTER:
                    deleted.append(store.tombstone(job, _now()))
                    changed = True
                    continue

            if job["status"] == "applied" and job.get("autofill_status") == "submitted":
                # Marked before submitted_at existed -> necessarily before today.
                submitted = job.get("submitted_at")
                if not submitted or datetime.fromisoformat(submitted).date() < date.today():
                    job["status"] = "submitted"
                    changed = True
            kept.append(job)

        forget_before = now - FORGET_DELETED_AFTER
        fresh = [d for d in deleted if datetime.fromisoformat(d["deleted_at"]) > forget_before]
        changed = changed or len(fresh) != len(deleted)
        if changed:
            store.save_jobs(kept)
            store.save_deleted(fresh)
    if changed:
        notify_jobs_changed()


def _housekeeping_worker() -> None:
    while True:
        time.sleep(HOUSEKEEPING_INTERVAL_SECONDS)
        _housekeep()


_housekeep()
threading.Thread(target=_generation_worker, daemon=True).start()
threading.Thread(target=_scoring_worker, daemon=True).start()
threading.Thread(target=_housekeeping_worker, daemon=True).start()
_enqueue_pending_jobs()
_enqueue_unscored_jobs()


def _store_scraped(jobs_df: pd.DataFrame) -> dict:
    kept_df, filtered_out = apply_prefilters(jobs_df)
    scraped = _jobs_from_df(kept_df)

    with store.LOCK:
        jobs = store.load_jobs()
        jobs, duplicates = store.merge_scraped_jobs(jobs, scraped, store.load_deleted())
        store.save_jobs(jobs)
    notify_jobs_changed()
    _enqueue_unscored_jobs()

    new_jobs = [job for job in jobs if job["status"] == "new"]
    return {
        "count": len(new_jobs),
        "jobs": new_jobs,
        "filtered_out": filtered_out,
        "duplicates": duplicates,
    }


@app.post("/api/scrape")
def trigger_scrape():
    return _store_scraped(scrape_daily_jobs())


@app.post("/api/scrape/ats")
def trigger_ats_scrape():
    return _store_scraped(scrape_ats_jobs())


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
                _reject(job)
        store.save_jobs(jobs)
    notify_jobs_changed()
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
    notify_jobs_changed()

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
            job["resume_path"] = None
            job["cover_letter_path"] = None
            job.pop("generation_error", None)
        store.save_jobs(jobs)
    notify_jobs_changed()

    if store.has_resume():
        for job in selected:
            generation_queue.put(job["id"])

    return {"retried": len(selected)}


@app.post("/api/jobs/mark-applied")
def mark_applied(body: JobIds):
    ids = set(body.ids)
    with store.LOCK:
        jobs = store.load_jobs()
        selected = [job for job in jobs if job["id"] in ids]
        for job in selected:
            job["autofill_status"] = "submitted"
            job["submitted_at"] = _now()
            job.pop("autofill_message", None)
        store.save_jobs(jobs)
    notify_jobs_changed()
    return {"marked": len(selected)}


@app.post("/api/resume")
async def upload_resume(file: UploadFile):
    content = (await file.read()).decode("utf-8")
    store.save_resume(content)
    _enqueue_pending_jobs()
    _enqueue_unscored_jobs()
    return {"uploaded": True, "filename": file.filename, "length": len(content)}


@app.get("/api/resume")
def get_resume_status():
    resume_text = store.read_resume()
    return {"uploaded": resume_text is not None, "text": resume_text}


@app.get("/api/profile")
def get_profile():
    return store.load_profile()


@app.post("/api/profile")
def update_profile(profile: Profile):
    store.save_profile(profile.model_dump())
    return {"saved": True}


app.mount("/files", StaticFiles(directory=store.DATA_DIR), name="files")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

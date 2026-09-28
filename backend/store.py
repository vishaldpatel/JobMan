import json
import re
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_PATH = DATA_DIR / "jobs_store.json"
RESUME_PATH = DATA_DIR / "resume.md"
APPLICATIONS_DIR = DATA_DIR / "applications"
PROFILE_PATH = DATA_DIR / "profile.json"
# Jobs purged from the Rejected pile. Remembered so the next scrape can't bring
# them back as "new" (and pay to score them again).
DELETED_PATH = DATA_DIR / "deleted_jobs.json"

# Guards read-modify-write access to jobs_store.json: the generation worker
# thread and request handlers both load-mutate-save the full file.
LOCK = threading.Lock()


def load_jobs() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    return json.loads(STORE_PATH.read_text())


def save_jobs(jobs: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(jobs, indent=2))


def _dedupe_key(job: dict) -> str:
    """Same title + same opening of the description = same role, even when it's
    listed on several boards (LinkedIn and Greenhouse, or two Greenhouse boards
    for sister companies) under different ids and company names."""
    title = re.sub(r"[^a-z0-9]", "", (job.get("title") or "").lower())
    body = job.get("description") or job.get("company") or ""
    body = re.sub(r"[^a-z0-9]", "", body.lower())[:300]
    return f"{title}|{body}"


def merge_scraped_jobs(
    store: list[dict], scraped: list[dict], deleted: list[dict]
) -> tuple[list[dict], int]:
    """Adds newly scraped jobs as status="new"; leaves already-known and
    previously deleted jobs out. Also returns how many scraped jobs were
    skipped as duplicates of known ones."""
    known_ids = {job["id"] for job in store} | {d["id"] for d in deleted}
    known_keys = {_dedupe_key(job) for job in store} | {d["key"] for d in deleted}
    new_jobs = []
    duplicates = 0
    for job in scraped:
        if job["id"] in known_ids:
            continue
        key = _dedupe_key(job)
        if key in known_keys:
            duplicates += 1
            continue
        known_ids.add(job["id"])
        known_keys.add(key)
        new_jobs.append(
            {
                **job,
                "status": "new",
                "application_status": None,
                "resume_path": None,
                "cover_letter_path": None,
            }
        )
    return store + new_jobs, duplicates


def load_deleted() -> list[dict]:
    if not DELETED_PATH.exists():
        return []
    return json.loads(DELETED_PATH.read_text())


def save_deleted(deleted: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DELETED_PATH.write_text(json.dumps(deleted, indent=2))


def tombstone(job: dict, deleted_at: str) -> dict:
    return {"id": job["id"], "key": _dedupe_key(job), "deleted_at": deleted_at}


def has_resume() -> bool:
    return RESUME_PATH.exists()


def read_resume() -> str | None:
    if not RESUME_PATH.exists():
        return None
    return RESUME_PATH.read_text()


def save_resume(text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_PATH.write_text(text)


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text())


def save_profile(profile: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2))

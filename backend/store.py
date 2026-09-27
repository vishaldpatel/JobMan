import json
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_PATH = DATA_DIR / "jobs_store.json"
RESUME_PATH = DATA_DIR / "resume.md"
APPLICATIONS_DIR = DATA_DIR / "applications"

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


def merge_scraped_jobs(store: list[dict], scraped: list[dict]) -> list[dict]:
    """Adds newly scraped jobs as status="new"; leaves already-known jobs untouched."""
    known_ids = {job["id"] for job in store}
    new_jobs = [
        {
            **job,
            "status": "new",
            "application_status": None,
            "resume_path": None,
            "cover_letter_path": None,
        }
        for job in scraped
        if job["id"] not in known_ids
    ]
    return store + new_jobs


def has_resume() -> bool:
    return RESUME_PATH.exists()


def read_resume() -> str | None:
    if not RESUME_PATH.exists():
        return None
    return RESUME_PATH.read_text()


def save_resume(text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_PATH.write_text(text)

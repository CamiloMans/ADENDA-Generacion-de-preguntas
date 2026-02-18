from __future__ import annotations

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services import job_service
from app.services.storage_service import remove_job_dir


def main() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        now = job_service.utcnow()
        jobs = job_service.list_expired_jobs(db, now=now)
        for job in jobs:
            remove_job_dir(settings.data_dir, job.id)
            job_service.delete_artifacts(db, job_id=job.id)
            job_service.update_job(
                db,
                job,
                status="expired",
                stage="finalizing",
                progress=100,
                error_code=job.error_code,
                error_message=job.error_message,
            )
        print(f"Expired jobs processed: {len(jobs)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

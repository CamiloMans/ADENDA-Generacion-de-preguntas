from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobArtifact

_UNSET = object()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_job(
    db: Session,
    *,
    job_id: UUID,
    original_filename: str,
    content_type: str,
    file_size_bytes: int,
    storage_path: Path,
    expires_at: datetime,
) -> Job:
    job = Job(
        id=job_id,
        status="queued",
        stage="queued",
        progress=0,
        original_filename=original_filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
        storage_path=str(storage_path),
        expires_at=expires_at,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: UUID) -> Job | None:
    return db.get(Job, job_id)


def list_expired_jobs(db: Session, now: datetime) -> list[Job]:
    stmt = select(Job).where(Job.expires_at <= now, Job.status != "expired")
    return list(db.scalars(stmt).all())


def update_job(
    db: Session,
    job: Job,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    error_code: str | None | object = _UNSET,
    error_message: str | None | object = _UNSET,
    summary: dict[str, Any] | None | object = _UNSET,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> Job:
    if status is not None:
        job.status = status
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = progress
    if error_code is not _UNSET:
        job.error_code = error_code
    if error_message is not _UNSET:
        job.error_message = error_message
    if summary is not _UNSET:
        job.summary = summary
    if started_at is not None:
        job.started_at = started_at
    if finished_at is not None:
        job.finished_at = finished_at

    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def mark_running(db: Session, job: Job) -> Job:
    return update_job(
        db,
        job,
        status="running",
        stage="extracting",
        progress=5,
        started_at=utcnow(),
        error_code=None,
        error_message=None,
    )


def mark_failed(db: Session, job: Job, *, error_code: str, error_message: str) -> Job:
    return update_job(
        db,
        job,
        status="failed",
        stage="finalizing",
        progress=100,
        error_code=error_code,
        error_message=error_message[:3900],
        finished_at=utcnow(),
    )


def mark_done(db: Session, job: Job, summary: dict[str, Any]) -> Job:
    return update_job(
        db,
        job,
        status="done",
        stage="finalizing",
        progress=100,
        summary=summary,
        finished_at=utcnow(),
    )


def add_artifact(
    db: Session,
    *,
    job_id: UUID,
    name: str,
    path: Path,
    size_bytes: int,
    sha256: str | None = None,
) -> JobArtifact:
    artifact = JobArtifact(
        job_id=job_id,
        name=name,
        path=str(path),
        size_bytes=size_bytes,
        sha256=sha256,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def get_artifact(db: Session, *, job_id: UUID, name: str) -> JobArtifact | None:
    stmt = select(JobArtifact).where(JobArtifact.job_id == job_id, JobArtifact.name == name)
    return db.scalar(stmt)


def delete_artifacts(db: Session, *, job_id: UUID) -> None:
    db.execute(delete(JobArtifact).where(JobArtifact.job_id == job_id))
    db.commit()


def delete_job(db: Session, job: Job) -> None:
    db.delete(job)
    db.commit()

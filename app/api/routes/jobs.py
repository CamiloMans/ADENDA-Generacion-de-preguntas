from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from redis import Redis
from sqlalchemy.orm import Session

from app.api.schemas.jobs import JobCreateResponse, JobResultResponse, JobStatusResponse
from app.core.config import get_settings
from app.core.security import require_api_key
from app.db.models import Job
from app.db.session import get_db
from app.services.google_drive_service import DriveServiceError, GoogleDriveService
from app.services import job_service
from app.services.storage_service import (
    ALLOWED_PDF_CONTENT_TYPES,
    remove_job_dir,
    save_upload_as_pdf,
    validate_artifact_name,
)
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])
PREGUNTAS_CLASIFICADAS_FILENAME: Final[str] = "preguntas_clasificadas.json"


def _primary_result_filename(job: Job) -> str:
    for artifact in job.artifacts:
        if artifact.name.endswith("_revisado.json"):
            return artifact.name
    if any(artifact.name == PREGUNTAS_CLASIFICADAS_FILENAME for artifact in job.artifacts):
        return PREGUNTAS_CLASIFICADAS_FILENAME
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Primary result artifact not found.")


def _must_get_job(db: Session, job_id: UUID) -> Job:
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


def _job_status_payload(job: Job) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job.id,
        adenda_id=job.adenda_id,
        status=job.status,
        progress=job.progress,
        stage=job.stage,
        drive_folder_url=job.drive_folder_url,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        expires_at=job.expires_at,
    )


def _artifact_file_response(db: Session, job_id: UUID, filename: str) -> Response:
    artifact = job_service.get_artifact(db, job_id=job_id, name=filename)
    if not artifact:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Artifact '{filename}' not found.")
    media_type = "application/json" if filename.endswith(".json") else None
    if artifact.storage_backend == "drive":
        if not artifact.external_file_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Artifact file '{filename}' missing remote file id.",
            )
        try:
            drive_service = GoogleDriveService.from_settings()
            content = drive_service.download_file_bytes(artifact.external_file_id)
        except DriveServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not retrieve artifact '{filename}' from Google Drive.",
            ) from exc
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return Response(content=content, media_type=artifact.mime_type or media_type, headers=headers)

    if not artifact.path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Artifact file '{filename}' missing.")
    path = Path(artifact.path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Artifact file '{filename}' missing.")
    return FileResponse(path=path, filename=filename, media_type=artifact.mime_type or media_type)


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    file: UploadFile = File(...),
    id_adenda: int = Form(...),
    classify: bool = Form(default=True),
    include_png: bool = Form(default=True),
    db: Session = Depends(get_db),
) -> JobCreateResponse:
    settings = get_settings()
    if (file.content_type or "").lower().strip() not in ALLOWED_PDF_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF uploads are allowed.",
        )
    if not classify:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires classify=true.",
        )
    if not include_png:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires include_png=true.",
        )

    try:
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        if not redis_client.ping():
            raise RuntimeError("Redis ping failed")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Queue backend unavailable.",
        ) from exc

    job_id = uuid4()

    _, size_bytes = save_upload_as_pdf(
        file,
        base_dir=settings.data_dir,
        job_id=job_id,
        max_pdf_bytes=settings.max_pdf_bytes,
    )

    expires_at = job_service.utcnow() + timedelta(days=settings.job_ttl_days)
    job = job_service.create_job(
        db,
        job_id=job_id,
        adenda_id=id_adenda,
        original_filename=file.filename or "upload.pdf",
        content_type=file.content_type or "application/pdf",
        file_size_bytes=size_bytes,
        storage_path=settings.data_dir / str(job_id),
        expires_at=expires_at,
    )

    try:
        celery_app.send_task(
            "app.tasks.pipeline_tasks.process_job",
            args=[str(job.id), classify, include_png],
            retry=False,
        )
    except Exception as exc:  # noqa: BLE001
        job_service.mark_failed(db, job, error_code="QUEUE_ERROR", error_message=str(exc))
        remove_job_dir(settings.data_dir, job_id=job.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not enqueue job.",
        ) from exc

    return JobCreateResponse(job_id=job.id, adenda_id=job.adenda_id, status=job.status, created_at=job.created_at)


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: UUID, db: Session = Depends(get_db)) -> JobStatusResponse:
    job = _must_get_job(db, job_id)
    return _job_status_payload(job)


@router.get("/{job_id}/result", response_model=JobResultResponse)
def get_job_result(job_id: UUID, request: Request, db: Session = Depends(get_db)) -> JobResultResponse:
    job = _must_get_job(db, job_id)

    if job.status == "expired":
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Job expired.")
    if job.status == "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": job.error_code, "error_message": job.error_message},
        )
    if job.status != "done":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Job status is {job.status}.")

    base = str(request.base_url).rstrip("/")
    artifacts = {
        a.name: f"{base}/v1/jobs/{job_id}/artifacts/{a.name}"
        for a in job.artifacts
    }
    return JobResultResponse(
        job_id=job.id,
        adenda_id=job.adenda_id,
        status=job.status,
        drive_folder_url=job.drive_folder_url,
        artifacts=artifacts,
        summary=job.summary,
    )


@router.get("/{job_id}/result/preguntas_clasificadas.json")
def get_result_preguntas_clasificadas(job_id: UUID, db: Session = Depends(get_db)) -> Response:
    job = _must_get_job(db, job_id)

    if job.status == "expired":
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Job expired.")
    if job.status == "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": job.error_code, "error_message": job.error_message},
        )
    if job.status != "done":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Job status is {job.status}.")

    return _artifact_file_response(db, job.id, _primary_result_filename(job))


@router.get("/{job_id}/preguntas_clasificadas")
def get_preguntas_clasificadas_legacy(job_id: UUID, db: Session = Depends(get_db)) -> Response:
    return get_result_preguntas_clasificadas(job_id=job_id, db=db)


@router.get("/{job_id}/artifacts/{filename}")
def get_job_artifact(job_id: UUID, filename: str, db: Session = Depends(get_db)) -> Response:
    validate_artifact_name(filename)
    _must_get_job(db, job_id)
    return _artifact_file_response(db, job_id, filename)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: UUID, db: Session = Depends(get_db)) -> Response:
    settings = get_settings()
    job = _must_get_job(db, job_id)
    remove_job_dir(settings.data_dir, job_id=job_id)
    job_service.delete_job(db, job)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

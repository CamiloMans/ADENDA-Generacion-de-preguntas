from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.pipeline.classify import run_classification
from app.pipeline.extract import run_extraction
from app.services import job_service
from app.services.google_drive_service import DriveServiceError, DriveUploadBundle, GoogleDriveService
from app.services.question_service import publish_job_results
from app.services.storage_service import remove_job_dir
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.pipeline_tasks.process_job")
def process_job(job_id: str, classify: bool = True, include_png: bool = True) -> dict:
    settings = get_settings()
    parsed_job_id = UUID(job_id)
    db = SessionLocal()
    upload_bundle: DriveUploadBundle | None = None
    drive_service: GoogleDriveService | None = None
    job = None
    try:
        if not classify:
            raise ValueError("This worker requires classify=True.")
        if not include_png:
            raise ValueError("This worker requires include_png=True.")

        job = job_service.get_job(db, parsed_job_id)
        if not job:
            return {"job_id": job_id, "status": "missing"}

        job_service.mark_running(db, job)

        job_dir = Path(job.storage_path)
        out_dir = job_dir / "outputs"
        input_pdf = job_dir / "input.pdf"
        out_dir.mkdir(parents=True, exist_ok=True)

        extraction = run_extraction(pdf_path=input_pdf, out_dir=out_dir, include_png=include_png)
        job = job_service.update_job(db, job, stage="classifying", progress=70)

        classification = run_classification(preguntas_json_path=out_dir / "preguntas.json", out_dir=out_dir)
        job = job_service.update_job(db, job, stage="uploading", progress=85)

        drive_service = GoogleDriveService.from_settings()
        upload_bundle = drive_service.upload_job_outputs(
            adenda_id=job.adenda_id,
            job_id=job.id,
            classified_json_path=out_dir / "preguntas_clasificadas.json",
            media_dir=out_dir / "outputs_png",
        )
        job = job_service.update_job(db, job, stage="persisting", progress=92)

        summary = {
            "pages": extraction.pages,
            "capitulos": extraction.capitulos,
            "bisagras": extraction.bisagras,
            "preguntas": extraction.preguntas,
            "tablas": extraction.tablas,
            "figuras": extraction.figuras,
            "total_detections": extraction.total_detections,
            "classified": classification.classified if classification else None,
            "unclassified": classification.unclassified if classification else None,
        }

        previous_jobs = job_service.list_jobs_by_adenda(db, job.adenda_id, exclude_job_id=job.id)
        old_run_folder_ids = publish_job_results(
            db,
            job=job,
            summary=summary,
            classified_json_path=out_dir / "preguntas_clasificadas.json",
            upload_bundle=upload_bundle,
            previous_jobs=previous_jobs,
        )

        for folder_id in old_run_folder_ids:
            try:
                drive_service.delete_file(folder_id)
            except DriveServiceError:
                logger.warning("Could not delete previous Drive run folder %s for adenda %s", folder_id, job.adenda_id)

        return {
            "job_id": job_id,
            "status": "done",
            "summary": summary,
            "drive_folder_url": job.drive_folder_url,
        }
    except FileNotFoundError as exc:
        logger.exception("File not found while processing job %s", job_id)
        if upload_bundle and drive_service:
            try:
                drive_service.delete_file(upload_bundle.run_folder.folder_id)
            except DriveServiceError:
                logger.warning("Could not clean Drive run folder %s after failure", upload_bundle.run_folder.folder_id)
        job = job_service.get_job(db, parsed_job_id)
        if job:
            job_service.mark_failed(db, job, error_code="INVALID_PDF", error_message=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error while processing job %s", job_id)
        if upload_bundle and drive_service:
            try:
                drive_service.delete_file(upload_bundle.run_folder.folder_id)
            except DriveServiceError:
                logger.warning("Could not clean Drive run folder %s after failure", upload_bundle.run_folder.folder_id)
        job = job_service.get_job(db, parsed_job_id)
        if job:
            job_service.mark_failed(db, job, error_code="PROCESSING_ERROR", error_message=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}
    finally:
        remove_job_dir(settings.data_dir, job_id=parsed_job_id)
        db.close()

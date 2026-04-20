from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.pipeline.classify import run_classification
from app.pipeline.extract import run_extraction
from app.pipeline.review import run_review
from app.services import job_service
from app.services.google_drive_service import (
    DriveFileRef,
    DriveRunContext,
    DriveServiceError,
    DriveUploadBundle,
    GoogleDriveService,
)
from app.services.result_service import publish_job_results
from app.services.storage_service import remove_job_dir, sanitize_artifact_stem
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _mark_job_failed_safe(db, parsed_job_id: UUID, *, error_code: str, error_message: str) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("Could not rollback transaction for failed job %s", parsed_job_id)

    job = job_service.get_job(db, parsed_job_id)
    if job:
        job_service.mark_failed(db, job, error_code=error_code, error_message=error_message)


def _rewrite_review_media_urls(reviewed_json_path: Path, media_files: dict[str, DriveFileRef]) -> None:
    payload = json.loads(reviewed_json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Reviewed JSON must be a list.")

    for obs in payload:
        for table in obs.get("tables", []):
            table_path = table.get("table_file")
            if table_path:
                uploaded = media_files.get(str(Path(table_path).resolve()))
                if uploaded:
                    table["table_file"] = uploaded.web_view_url
        for image in obs.get("images", []):
            image_path = image.get("image_file")
            if image_path:
                uploaded = media_files.get(str(Path(image_path).resolve()))
                if uploaded:
                    image["image_file"] = uploaded.web_view_url

    reviewed_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _upload_review_outputs(
    drive_service: GoogleDriveService,
    *,
    adenda_id: int,
    job_id: UUID,
    review_json_path: Path,
    audit_json_path: Path,
    report_md_path: Path,
    tables_dir: Path,
    images_dir: Path,
) -> DriveUploadBundle:
    context = drive_service.create_job_run_context(adenda_id=adenda_id, job_id=job_id)
    try:
        media_files: dict[str, DriveFileRef] = {}

        if tables_dir.exists():
            tables_folder = drive_service.create_folder(
                name="tables",
                parent_id=context.media_folder.folder_id,
            )
            media_files.update(
                drive_service.upload_directory_files(
                    directory=tables_dir,
                    parent_id=tables_folder.folder_id,
                )
            )

        if images_dir.exists():
            images_folder = drive_service.create_folder(
                name="images",
                parent_id=context.media_folder.folder_id,
            )
            media_files.update(
                drive_service.upload_directory_files(
                    directory=images_dir,
                    parent_id=images_folder.folder_id,
                )
            )

        _rewrite_review_media_urls(review_json_path, media_files)

        artifact_files = drive_service.upload_artifact_files(
            files=[review_json_path, audit_json_path, report_md_path],
            parent_id=context.run_folder.folder_id,
        )

        return DriveUploadBundle(
            adenda_folder=context.adenda_folder,
            run_folder=context.run_folder,
            artifact_files=artifact_files,
            media_files=media_files,
        )
    except Exception:
        try:
            drive_service.delete_file(context.run_folder.folder_id)
        except DriveServiceError:
            logger.warning("Could not clean Drive run folder %s after upload failure", context.run_folder.folder_id)
        raise


def _build_summary(*, extraction, classification, review) -> dict[str, int]:
    return {
        "pages": extraction.pages,
        "observaciones": review.total_observaciones,
        "tablas": extraction.tablas,
        "imagenes": extraction.imagenes,
        "clasificadas": classification.classified,
        "sin_clasificar": classification.unclassified,
        "ids_faltantes_detectados": review.ids_faltantes_detectados,
        "ids_extraidas_desde_pdf": review.ids_extraidas_desde_pdf,
        "ids_no_localizadas": review.ids_no_localizadas,
        "correcciones_revision": review.correcciones_revision,
    }


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

        artifact_stem = sanitize_artifact_stem(job.original_filename)

        job_service.mark_running(db, job)

        job_dir = Path(job.storage_path)
        out_dir = job_dir / "outputs"
        input_pdf = job_dir / "input.pdf"
        out_dir.mkdir(parents=True, exist_ok=True)

        extraction = run_extraction(
            pdf_path=input_pdf,
            out_dir=out_dir,
            include_png=include_png,
            artifact_stem=artifact_stem,
        )
        job = job_service.update_job(db, job, stage="classifying", progress=40)

        classification = run_classification(
            observaciones_json_path=extraction.output_json,
            out_dir=out_dir,
            artifact_stem=artifact_stem,
        )
        job = job_service.update_job(db, job, stage="reviewing", progress=70)

        review = run_review(
            input_json_path=classification.output_json,
            input_pdf_path=input_pdf,
            out_dir=out_dir,
            artifact_stem=artifact_stem,
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            pdf_dpi=settings.anthropic_pdf_dpi,
        )
        job = job_service.update_job(db, job, stage="uploading", progress=85)

        drive_service = GoogleDriveService.from_settings()
        upload_bundle = _upload_review_outputs(
            drive_service,
            adenda_id=job.adenda_id,
            job_id=job.id,
            review_json_path=review.output_json,
            audit_json_path=review.audit_json,
            report_md_path=review.report_md,
            tables_dir=extraction.tables_dir,
            images_dir=extraction.images_dir,
        )
        job = job_service.update_job(db, job, stage="persisting", progress=92)

        summary = _build_summary(
            extraction=extraction,
            classification=classification,
            review=review,
        )

        previous_jobs = job_service.list_jobs_by_adenda(db, job.adenda_id, exclude_job_id=job.id)
        old_run_folder_ids = publish_job_results(
            db,
            job=job,
            summary=summary,
            reviewed_json_path=review.output_json,
            upload_bundle=upload_bundle,
            previous_jobs=previous_jobs,
        )

        for folder_id in old_run_folder_ids:
            try:
                drive_service.delete_file(folder_id)
            except DriveServiceError:
                logger.warning(
                    "Could not delete previous Drive run folder %s for adenda %s",
                    folder_id,
                    job.adenda_id,
                )

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
        _mark_job_failed_safe(db, parsed_job_id, error_code="INVALID_PDF", error_message=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error while processing job %s", job_id)
        if upload_bundle and drive_service:
            try:
                drive_service.delete_file(upload_bundle.run_folder.folder_id)
            except DriveServiceError:
                logger.warning("Could not clean Drive run folder %s after failure", upload_bundle.run_folder.folder_id)
        _mark_job_failed_safe(db, parsed_job_id, error_code="PROCESSING_ERROR", error_message=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}
    finally:
        remove_job_dir(settings.data_dir, job_id=parsed_job_id)
        db.close()

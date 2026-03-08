from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobArtifact, Question, QuestionMedia
from app.services.google_drive_service import DriveUploadBundle
from app.services.job_service import utcnow


def publish_job_results(
    db: Session,
    *,
    job: Job,
    summary: dict[str, Any],
    classified_json_path: Path,
    upload_bundle: DriveUploadBundle,
    previous_jobs: list[Job],
) -> list[str]:
    questions_payload = _load_questions(classified_json_path)
    old_run_folder_ids = [prev.drive_folder_id for prev in previous_jobs if prev.drive_folder_id]
    previous_job_ids = [prev.id for prev in previous_jobs]

    try:
        _delete_questions_for_adenda(db, adenda_id=job.adenda_id)

        if previous_job_ids:
            db.execute(delete(JobArtifact).where(JobArtifact.job_id.in_(previous_job_ids)))
            for previous_job in previous_jobs:
                previous_job.drive_folder_id = None
                previous_job.drive_folder_url = None
                db.add(previous_job)

        db.execute(delete(JobArtifact).where(JobArtifact.job_id == job.id))

        for item in questions_payload:
            question = Question(
                job_id=job.id,
                adenda_id=job.adenda_id,
                numero=int(item["numero"]),
                capitulo=item.get("capitulo", "") or "",
                bisagra=item.get("bisagra"),
                texto=item.get("texto", "") or "",
                tema_principal=item.get("tema_principal"),
                tema_principal_id=item.get("tema_principal_id"),
                temas_principales=item.get("temas_principales"),
                temas_principales_id=item.get("temas_principales_id"),
                score=item.get("score"),
                temas_secundarios=item.get("temas_secundarios"),
                keywords_match=item.get("keywords_match"),
            )
            db.add(question)
            db.flush()

            for media_item in item.get("tablas_figuras", []):
                filename = media_item["png"]
                uploaded_media = upload_bundle.media_files.get(filename)
                if uploaded_media is None:
                    raise RuntimeError(f"Uploaded media metadata not found for '{filename}'.")

                db.add(
                    QuestionMedia(
                        question_id=question.id,
                        filename=filename,
                        tipo=media_item["tipo"],
                        parte=int(media_item["parte"]),
                        mime_type=uploaded_media.mime_type,
                        drive_file_id=uploaded_media.file_id,
                        drive_preview_url=uploaded_media.preview_url,
                        drive_web_view_url=uploaded_media.web_view_url,
                    )
                )

        job.drive_folder_id = upload_bundle.run_folder.folder_id
        job.drive_folder_url = upload_bundle.run_folder.web_view_url
        job.summary = summary
        job.status = "done"
        job.stage = "finalizing"
        job.progress = 100
        job.finished_at = utcnow()
        db.add(job)

        db.add(
            JobArtifact(
                job_id=job.id,
                name="preguntas_clasificadas.json",
                path=None,
                size_bytes=upload_bundle.json_file.size_bytes,
                sha256=upload_bundle.json_file.sha256,
                storage_backend="drive",
                mime_type=upload_bundle.json_file.mime_type,
                external_url=upload_bundle.json_file.download_url,
                external_file_id=upload_bundle.json_file.file_id,
            )
        )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return [folder_id for folder_id in old_run_folder_ids if folder_id]


def _load_questions(classified_json_path: Path) -> list[dict[str, Any]]:
    if not classified_json_path.exists():
        raise FileNotFoundError(f"Classified questions JSON not found: {classified_json_path}")
    content = classified_json_path.read_text(encoding="utf-8")
    payload = json.loads(content)
    if not isinstance(payload, list):
        raise ValueError("Classified questions JSON must be a list.")
    return payload


def _delete_questions_for_adenda(db: Session, *, adenda_id: int) -> None:
    question_ids = list(db.scalars(select(Question.id).where(Question.adenda_id == adenda_id)).all())
    if not question_ids:
        return
    db.execute(delete(QuestionMedia).where(QuestionMedia.question_id.in_(question_ids)))
    db.execute(delete(Question).where(Question.id.in_(question_ids)))

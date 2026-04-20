from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.text_normalization import repair_mojibake_data, repair_mojibake_text
from app.db.models import Job, JobArtifact, Question, QuestionMedia
from app.services.google_drive_service import DriveFileRef, DriveUploadBundle
from app.services.job_service import utcnow

logger = logging.getLogger(__name__)


def publish_job_results(
    db: Session,
    *,
    job: Job,
    summary: dict[str, Any],
    reviewed_json_path: Path,
    upload_bundle: DriveUploadBundle,
    previous_jobs: list[Job],
) -> list[str]:
    questions_payload = repair_mojibake_data(_load_reviewed_questions(reviewed_json_path))
    old_run_folder_ids = [prev.drive_folder_id for prev in previous_jobs if prev.drive_folder_id]
    previous_job_ids = [prev.id for prev in previous_jobs]
    media_lookup = _build_media_lookup(upload_bundle.media_files)

    try:
        _delete_questions_for_adenda(db, adenda_id=job.adenda_id)

        if previous_job_ids:
            db.execute(delete(JobArtifact).where(JobArtifact.job_id.in_(previous_job_ids)))
            for previous_job in previous_jobs:
                previous_job.drive_folder_id = None
                previous_job.drive_folder_url = None
                db.add(previous_job)

        db.execute(delete(JobArtifact).where(JobArtifact.job_id == job.id))

        used_numbers: set[int] = set()
        for order, item in enumerate(questions_payload, start=1):
            question = _build_question(job=job, item=item, order=order, used_numbers=used_numbers)
            db.add(question)
            db.flush()

            for media_row in _build_question_media_rows(
                question_id=question.id,
                item=item,
                media_lookup=media_lookup,
            ):
                db.add(media_row)

        for artifact_name, artifact_ref in upload_bundle.artifact_files.items():
            db.add(
                JobArtifact(
                    job_id=job.id,
                    name=artifact_name,
                    path=None,
                    size_bytes=artifact_ref.size_bytes,
                    sha256=artifact_ref.sha256,
                    storage_backend="drive",
                    mime_type=artifact_ref.mime_type,
                    external_url=artifact_ref.download_url,
                    external_file_id=artifact_ref.file_id,
                )
            )

        job.drive_folder_id = upload_bundle.run_folder.folder_id
        job.drive_folder_url = upload_bundle.run_folder.web_view_url
        job.summary = repair_mojibake_data(summary)
        job.status = "done"
        job.stage = "finalizing"
        job.progress = 100
        job.finished_at = utcnow()
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return [folder_id for folder_id in old_run_folder_ids if folder_id]


def _load_reviewed_questions(reviewed_json_path: Path) -> list[dict[str, Any]]:
    if not reviewed_json_path.exists():
        raise FileNotFoundError(f"Reviewed questions JSON not found: {reviewed_json_path}")
    payload = json.loads(reviewed_json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Reviewed questions JSON must be a list.")
    return [item for item in payload if isinstance(item, dict)]


def _build_question(
    *,
    job: Job,
    item: dict[str, Any],
    order: int,
    used_numbers: set[int],
) -> Question:
    item = repair_mojibake_data(item)
    observation_id = _observation_id_for_item(item=item, order=order)
    section_1 = _safe_text(item.get("section_1"))
    section_2 = _nullable_text(item.get("section_2"))
    requirement_types = _string_list(item.get("requirement_types"))
    clasificacion = item.get("clasificacion") if isinstance(item.get("clasificacion"), dict) else {}
    temas_principales = _dict_list(clasificacion.get("temas_principales"))
    temas_secundarios = _dict_list(clasificacion.get("temas_secundarios"))
    keywords_match = _string_list(clasificacion.get("keywords_match"))
    numero = _assign_question_number(
        observation_id=observation_id,
        order=order,
        used_numbers=used_numbers,
    )

    return Question(
        job_id=job.id,
        adenda_id=job.adenda_id,
        numero=numero,
        observation_id=observation_id,
        orden=order,
        capitulo=section_1,
        bisagra=section_2,
        section_1=section_1,
        section_2=section_2,
        texto=_safe_text(item.get("text")),
        requirement_types=requirement_types,
        tema_principal=_nullable_text(clasificacion.get("tema_principal")),
        tema_principal_id=_nullable_text(clasificacion.get("tema_principal_id")),
        clasificacion_json=clasificacion or None,
        temas_principales=[_safe_text(entry.get("nombre")) for entry in temas_principales if _safe_text(entry.get("nombre"))],
        temas_principales_id=[_safe_text(entry.get("id")) for entry in temas_principales if _safe_text(entry.get("id"))],
        temas_principales_json=temas_principales or None,
        score=_nullable_float(clasificacion.get("score")),
        temas_secundarios=temas_secundarios or None,
        keywords_match=keywords_match or None,
        raw_question_json=item,
    )


def _build_question_media_rows(
    *,
    question_id: int,
    item: dict[str, Any],
    media_lookup: dict[str, DriveFileRef],
) -> list[QuestionMedia]:
    rows: list[QuestionMedia] = []

    for index, table in enumerate(_dict_list(item.get("tables")), start=1):
        table_ref = table.get("table_file")
        if not _has_media_ref(table_ref):
            logger.warning(
                "Skipping table media for question %s because table_file is missing.",
                question_id,
            )
            continue

        try:
            uploaded = _resolve_media_ref(table_ref, media_lookup)
        except RuntimeError:
            logger.warning(
                "Skipping table media for question %s because table_file '%s' could not be resolved.",
                question_id,
                table_ref,
            )
            continue
        rows.append(
            QuestionMedia(
                question_id=question_id,
                filename=uploaded.filename,
                tipo="tabla",
                parte=index,
                caption=_nullable_text(table.get("caption")),
                table_rows=table.get("rows") if isinstance(table.get("rows"), list) else None,
                mime_type=uploaded.mime_type,
                drive_file_id=uploaded.file_id,
                drive_preview_url=uploaded.preview_url,
                drive_web_view_url=uploaded.web_view_url,
            )
        )

    for index, image in enumerate(_dict_list(item.get("images")), start=1):
        image_ref = image.get("image_file")
        if not _has_media_ref(image_ref):
            logger.warning(
                "Skipping image media for question %s because image_file is missing.",
                question_id,
            )
            continue

        try:
            uploaded = _resolve_media_ref(image_ref, media_lookup)
        except RuntimeError:
            logger.warning(
                "Skipping image media for question %s because image_file '%s' could not be resolved.",
                question_id,
                image_ref,
            )
            continue
        rows.append(
            QuestionMedia(
                question_id=question_id,
                filename=uploaded.filename,
                tipo="figura",
                parte=index,
                caption=_nullable_text(image.get("caption")),
                table_rows=None,
                mime_type=uploaded.mime_type,
                drive_file_id=uploaded.file_id,
                drive_preview_url=uploaded.preview_url,
                drive_web_view_url=uploaded.web_view_url,
            )
        )

    return rows


def _build_media_lookup(media_files: dict[str, DriveFileRef]) -> dict[str, DriveFileRef]:
    lookup: dict[str, DriveFileRef] = {}
    for key, media_ref in media_files.items():
        lookup[key] = media_ref
        lookup[str(Path(key).resolve())] = media_ref
        lookup[media_ref.filename] = media_ref
        lookup[media_ref.web_view_url] = media_ref
        lookup[media_ref.preview_url] = media_ref
        lookup[media_ref.download_url] = media_ref
    return lookup


def _has_media_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _resolve_media_ref(value: Any, media_lookup: dict[str, DriveFileRef]) -> DriveFileRef:
    if not _has_media_ref(value):
        raise RuntimeError("Question media item is missing its file reference.")

    candidate = value.strip()
    uploaded = media_lookup.get(candidate)
    if uploaded is not None:
        return uploaded

    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"}:
        raise RuntimeError(f"Uploaded media metadata not found for '{candidate}'.")

    resolved = str(Path(candidate).resolve())
    uploaded = media_lookup.get(resolved)
    if uploaded is not None:
        return uploaded

    uploaded = media_lookup.get(Path(candidate).name)
    if uploaded is not None:
        return uploaded

    raise RuntimeError(f"Uploaded media metadata not found for '{candidate}'.")


def _observation_id_for_item(*, item: dict[str, Any], order: int) -> str:
    observation_id = _safe_text(item.get("observation_id"))
    return observation_id or f"OBS-{order}"


def _assign_question_number(
    *,
    observation_id: str,
    order: int,
    used_numbers: set[int],
) -> int:
    packed = _pack_observation_id(observation_id)
    if packed is not None and packed not in used_numbers:
        used_numbers.add(packed)
        return packed

    fallback = order
    while fallback in used_numbers:
        fallback += 1
    used_numbers.add(fallback)
    return fallback


def _pack_observation_id(observation_id: str) -> int | None:
    parts = [segment for segment in observation_id.strip().split(".") if segment]
    if not parts or not all(part.isdigit() for part in parts):
        return None

    packed = 0
    for part in parts[:6]:
        packed = (packed * 1000) + int(part)
    return packed


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return repair_mojibake_text(value).strip()
    return repair_mojibake_text(str(value)).strip()


def _nullable_text(value: Any) -> str | None:
    text = _safe_text(value)
    return text or None


def _nullable_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [_safe_text(item) for item in value]
    return [item for item in items if item]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _delete_questions_for_adenda(db: Session, *, adenda_id: int) -> None:
    question_ids = list(db.scalars(select(Question.id).where(Question.adenda_id == adenda_id)).all())
    if not question_ids:
        return
    db.execute(delete(QuestionMedia).where(QuestionMedia.question_id.in_(question_ids)))
    db.execute(delete(Question).where(Question.id.in_(question_ids)))

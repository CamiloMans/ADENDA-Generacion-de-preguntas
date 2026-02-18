from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from app.db.session import SessionLocal
from app.pipeline.classify import run_classification
from app.pipeline.extract import run_extraction
from app.services import job_service
from app.services.storage_service import make_outputs_zip, sha256_file
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.pipeline_tasks.process_job")
def process_job(job_id: str, classify: bool = True, include_png: bool = True) -> dict:
    db = SessionLocal()
    try:
        parsed_job_id = UUID(job_id)
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

        classification = None
        if classify:
            classification = run_classification(preguntas_json_path=out_dir / "preguntas.json", out_dir=out_dir)
        else:
            classification = None

        job = job_service.update_job(db, job, stage="finalizing", progress=90)

        zip_path = make_outputs_zip(out_dir) if include_png else None

        job_service.delete_artifacts(db, job_id=job.id)
        artifact_files = [
            out_dir / "preguntas.json",
            out_dir / "preguntas.txt",
            out_dir / "chapters_hinges.json",
            out_dir / "texto_total.txt",
        ]
        if classify:
            artifact_files.extend(
                [
                    out_dir / "preguntas_clasificadas.json",
                    out_dir / "preguntas_clasificadas_detalle.json",
                ]
            )
        if zip_path and zip_path.exists():
            artifact_files.append(zip_path)

        for path in artifact_files:
            if not path.exists():
                continue
            job_service.add_artifact(
                db,
                job_id=job.id,
                name=path.name,
                path=path,
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )

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
        job_service.mark_done(db, job, summary=summary)

        return {"job_id": job_id, "status": "done", "summary": summary}
    except FileNotFoundError as exc:
        logger.exception("File not found while processing job %s", job_id)
        job = job_service.get_job(db, UUID(job_id))
        if job:
            job_service.mark_failed(db, job, error_code="INVALID_PDF", error_message=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error while processing job %s", job_id)
        job = job_service.get_job(db, UUID(job_id))
        if job:
            job_service.mark_failed(db, job, error_code="PROCESSING_ERROR", error_message=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}
    finally:
        db.close()

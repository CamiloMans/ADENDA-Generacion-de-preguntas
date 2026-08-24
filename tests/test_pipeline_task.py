from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.db.models import JobArtifact, Question, QuestionMedia
from app.db.session import SessionLocal
from app.pipeline.types import ClassificationSummary, ExtractionSummary, ReviewSummary
from app.services import job_service
from app.services.google_drive_service import DriveFileRef, DriveFolder, DriveRunContext
from app.tasks import pipeline_tasks


def _create_job(*, adenda_id: int, job_id: UUID, job_dir: Path) -> None:
    db = SessionLocal()
    try:
        job_service.create_job(
            db,
            job_id=job_id,
            adenda_id=adenda_id,
            original_filename="ICSARA Demo.pdf",
            content_type="application/pdf",
            file_size_bytes=8,
            storage_path=job_dir,
            expires_at=job_service.utcnow() + timedelta(days=1),
        )
    finally:
        db.close()


def _write_parser_outputs(
    out_dir: Path,
    *,
    stem: str,
    table_files: list[str] | None = None,
    image_files: list[str] | None = None,
) -> ExtractionSummary:
    table_files = table_files or []
    image_files = image_files or []

    tables_dir = out_dir / "tables"
    images_dir = out_dir / "images"
    tables_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    for filename in table_files:
        (tables_dir / filename).write_bytes(b"table")
    for filename in image_files:
        (images_dir / filename).write_bytes(b"image")

    payload = [
        {
            "observation_id": "1.1.",
            "section_1": "Seccion 1",
            "section_2": None,
            "requirement_types": ["aclarar"],
            "text": "Observacion 1",
            "tables": [
                {
                    "table_file": str((tables_dir / filename).resolve()),
                    "rows": [["a", "b"]],
                }
                for filename in table_files
            ],
            "images": [
                {
                    "image_file": str((images_dir / filename).resolve()),
                    "caption": "Imagen",
                }
                for filename in image_files
            ],
        }
    ]

    output_json = out_dir / f"{stem}.json"
    tables_json = out_dir / "tablas_detectadas.json"
    summary_json = out_dir / "resumen.json"

    output_json.write_text(json.dumps(payload), encoding="utf-8")
    tables_json.write_text(json.dumps({"tables": []}), encoding="utf-8")
    summary_json.write_text(
        json.dumps(
            {
                "total_observaciones": len(payload),
                "total_tablas": len(table_files),
                "total_imagenes": len(image_files),
            }
        ),
        encoding="utf-8",
    )

    return ExtractionSummary(
        pages=2,
        observaciones=len(payload),
        tablas=len(table_files),
        imagenes=len(image_files),
        output_dir=out_dir,
        output_json=output_json,
        tables_json=tables_json,
        summary_json=summary_json,
        images_dir=images_dir,
        tables_dir=tables_dir,
    )


def _write_review_outputs(
    out_dir: Path,
    *,
    stem: str,
    tables_dir: Path,
    images_dir: Path,
) -> ReviewSummary:
    output_json = out_dir / f"{stem}_revisado.json"
    audit_json = out_dir / "auditoria_cambios.json"
    report_md = out_dir / "verificacion_icsara.md"

    payload = [
        {
            "observation_id": "1.1.",
            "section_1": "Seccion 1",
            "section_2": None,
            "requirement_types": ["aclarar"],
            "clasificacion": {
                "tema_principal": "Tema A",
                "tema_principal_id": "TEMA_A",
                "score": 3.0,
                "temas_principales": [{"id": "TEMA_A", "nombre": "Tema A", "score": 3.0}],
                "temas_secundarios": [],
                "keywords_match": ["tema a"],
            },
            "text": "Observacion revisada",
            "tables": [
                {
                    "table_file": str((tables_dir / "tabla_001.png").resolve()),
                    "rows": [["1", "2"]],
                }
            ]
            if (tables_dir / "tabla_001.png").exists()
            else [],
            "images": [
                {
                    "image_file": str((images_dir / "image_001.png").resolve()),
                    "caption": "Imagen",
                }
            ]
            if (images_dir / "image_001.png").exists()
            else [],
        }
    ]

    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_json.write_text(json.dumps([{"tipo": "C2C3_correccion"}]), encoding="utf-8")
    report_md.write_text("# reporte", encoding="utf-8")

    return ReviewSummary(
        total_observaciones=len(payload),
        ids_faltantes_detectados=1,
        ids_extraidas_desde_pdf=1,
        ids_no_localizadas=0,
        correcciones_revision=1,
        output_json=output_json,
        audit_json=audit_json,
        report_md=report_md,
    )


class _FakeDriveService:
    def __init__(self) -> None:
        self.deleted_ids: list[str] = []
        self.uploaded_artifacts: dict[str, bytes] = {}

    def create_job_run_context(self, *, adenda_id: int, job_id: UUID) -> DriveRunContext:
        return DriveRunContext(
            adenda_folder=DriveFolder(
                folder_id=f"adenda-{adenda_id}",
                name=f"adenda_{adenda_id}",
                web_view_url=f"https://drive.example/adenda/{adenda_id}",
            ),
            run_folder=DriveFolder(
                folder_id=f"run-{job_id}",
                name=f"run_{job_id}",
                web_view_url=f"https://drive.example/run/{job_id}",
            ),
            media_folder=DriveFolder(
                folder_id=f"media-{job_id}",
                name="media",
                web_view_url=f"https://drive.example/run/{job_id}/media",
            ),
        )

    def create_folder(self, *, name: str, parent_id: str) -> DriveFolder:
        return DriveFolder(
            folder_id=f"{parent_id}-{name}",
            name=name,
            web_view_url=f"https://drive.example/{parent_id}/{name}",
        )

    def upload_directory_files(self, *, directory: Path, parent_id: str) -> dict[str, DriveFileRef]:
        files: dict[str, DriveFileRef] = {}
        for file_path in sorted(directory.glob("*")):
            files[str(file_path.resolve())] = DriveFileRef(
                file_id=f"media-{file_path.name}",
                filename=file_path.name,
                mime_type="image/png",
                web_view_url=f"https://drive.example/media/{file_path.name}",
                preview_url=f"https://drive.example/media/{file_path.name}/preview",
                download_url=f"https://drive.example/media/{file_path.name}/download",
                size_bytes=file_path.stat().st_size,
                sha256=f"sha-{file_path.name}",
            )
        return files

    def upload_artifact_files(self, *, files: list[Path], parent_id: str) -> dict[str, DriveFileRef]:
        uploaded: dict[str, DriveFileRef] = {}
        for file_path in files:
            self.uploaded_artifacts[file_path.name] = file_path.read_bytes()
            uploaded[file_path.name] = DriveFileRef(
                file_id=f"artifact-{file_path.name}",
                filename=file_path.name,
                mime_type="application/json" if file_path.suffix == ".json" else "text/markdown",
                web_view_url=f"https://drive.example/artifacts/{file_path.name}",
                preview_url=f"https://drive.example/artifacts/{file_path.name}/preview",
                download_url=f"https://drive.example/artifacts/{file_path.name}/download",
                size_bytes=file_path.stat().st_size,
                sha256=f"sha-{file_path.name}",
            )
        return uploaded

    def delete_file(self, file_id: str) -> None:
        self.deleted_ids.append(file_id)


def test_process_job_persists_dynamic_artifacts_rewrites_media_and_cleans_local_dir(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"
    settings.anthropic_adenda_validacion_icsara_api_key = "test-key"
    settings.anthropic_model = "claude-test"

    job_id = uuid4()
    job_dir = settings.data_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")
    _create_job(adenda_id=777, job_id=job_id, job_dir=job_dir)

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True, artifact_stem: str | None = None) -> ExtractionSummary:
        assert artifact_stem == "ICSARA_Demo"
        return _write_parser_outputs(
            Path(out_dir),
            stem=artifact_stem or "ICSARA_Demo",
            table_files=["tabla_001.png"],
            image_files=["image_001.png"],
        )

    def fake_classification(observaciones_json_path: Path | str, out_dir: Path | str, artifact_stem: str | None = None) -> ClassificationSummary:
        output_json = Path(out_dir) / f"{artifact_stem}_clasificado.json"
        output_json.write_text("[]", encoding="utf-8")
        return ClassificationSummary(
            total=1,
            classified=1,
            unclassified=0,
            output_json=output_json,
            output_detail_json=Path(out_dir) / "resumen_clasificado.json",
        )

    def fake_review(
        input_json_path: Path | str,
        input_pdf_path: Path | str,
        out_dir: Path | str,
        artifact_stem: str,
        *,
        api_key: str,
        model: str,
        pdf_dpi: int = 150,
    ) -> ReviewSummary:
        assert api_key == "test-key"
        assert model == "claude-test"
        return _write_review_outputs(
            Path(out_dir),
            stem=artifact_stem,
            tables_dir=Path(out_dir) / "tables",
            images_dir=Path(out_dir) / "images",
        )

    fake_drive = _FakeDriveService()
    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)
    monkeypatch.setattr(pipeline_tasks, "run_review", fake_review)
    monkeypatch.setattr(pipeline_tasks.GoogleDriveService, "from_settings", staticmethod(lambda: fake_drive))

    result = pipeline_tasks.process_job(str(job_id))

    assert result["status"] == "done"
    assert result["summary"]["observaciones"] == 1
    assert result["summary"]["tablas"] == 1
    assert result["summary"]["imagenes"] == 1
    assert result["drive_folder_url"] == f"https://drive.example/run/{job_id}"
    assert not job_dir.exists()

    reviewed_payload = json.loads(fake_drive.uploaded_artifacts["ICSARA_Demo_revisado.json"].decode("utf-8"))
    assert reviewed_payload[0]["tables"][0]["table_file"] == "https://drive.example/media/tabla_001.png"
    assert reviewed_payload[0]["images"][0]["image_file"] == "https://drive.example/media/image_001.png"

    db = SessionLocal()
    try:
        job = job_service.get_job(db, job_id)
        artifacts = db.query(JobArtifact).order_by(JobArtifact.name.asc()).all()
        questions = db.query(Question).order_by(Question.numero.asc()).all()
        media = db.query(QuestionMedia).order_by(QuestionMedia.tipo.asc(), QuestionMedia.parte.asc()).all()

        assert job is not None
        assert job.status == "done"
        assert job.drive_folder_id == f"run-{job_id}"
        assert {artifact.name for artifact in artifacts} == {
            "ICSARA_Demo_revisado.json",
            "auditoria_cambios.json",
            "verificacion_icsara.md",
        }
        assert len(questions) == 1
        assert questions[0].observation_id == "1.1."
        assert questions[0].numero == 1001
        assert questions[0].orden == 1
        assert questions[0].capitulo == "Seccion 1"
        assert questions[0].section_1 == "Seccion 1"
        assert questions[0].requirement_types == ["aclarar"]
        assert questions[0].tema_principal == "Tema A"
        assert questions[0].tema_principal_id == "TEMA_A"
        assert questions[0].temas_principales == ["Tema A"]
        assert questions[0].temas_principales_id == ["TEMA_A"]
        assert questions[0].keywords_match == ["tema a"]
        assert questions[0].raw_question_json is not None
        assert len(media) == 2
        assert {item.tipo for item in media} == {"figura", "tabla"}
        assert media[0].drive_web_view_url.startswith("https://drive.example/media/")
    finally:
        db.close()


def test_process_job_replaces_previous_adenda_artifacts(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"
    settings.anthropic_adenda_validacion_icsara_api_key = "test-key"
    settings.anthropic_model = "claude-test"

    first_job_id = uuid4()
    second_job_id = uuid4()

    for job_id in (first_job_id, second_job_id):
        job_dir = settings.data_dir / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")
        _create_job(adenda_id=999, job_id=job_id, job_dir=job_dir)

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True, artifact_stem: str | None = None) -> ExtractionSummary:
        return _write_parser_outputs(Path(out_dir), stem=artifact_stem or "ICSARA_Demo")

    def fake_classification(observaciones_json_path: Path | str, out_dir: Path | str, artifact_stem: str | None = None) -> ClassificationSummary:
        output_json = Path(out_dir) / f"{artifact_stem}_clasificado.json"
        output_json.write_text("[]", encoding="utf-8")
        return ClassificationSummary(
            total=1,
            classified=1,
            unclassified=0,
            output_json=output_json,
            output_detail_json=Path(out_dir) / "resumen_clasificado.json",
        )

    def fake_review(
        input_json_path: Path | str,
        input_pdf_path: Path | str,
        out_dir: Path | str,
        artifact_stem: str,
        *,
        api_key: str,
        model: str,
        pdf_dpi: int = 150,
    ) -> ReviewSummary:
        return _write_review_outputs(
            Path(out_dir),
            stem=artifact_stem,
            tables_dir=Path(out_dir) / "tables",
            images_dir=Path(out_dir) / "images",
        )

    fake_drive = _FakeDriveService()
    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)
    monkeypatch.setattr(pipeline_tasks, "run_review", fake_review)
    monkeypatch.setattr(pipeline_tasks.GoogleDriveService, "from_settings", staticmethod(lambda: fake_drive))

    first_result = pipeline_tasks.process_job(str(first_job_id))
    second_result = pipeline_tasks.process_job(str(second_job_id))

    assert first_result["status"] == "done"
    assert second_result["status"] == "done"
    assert f"run-{first_job_id}" in fake_drive.deleted_ids

    db = SessionLocal()
    try:
        first_job = job_service.get_job(db, first_job_id)
        second_job = job_service.get_job(db, second_job_id)
        second_artifacts = db.query(JobArtifact).filter(JobArtifact.job_id == second_job_id).all()
        second_questions = db.query(Question).filter(Question.job_id == second_job_id).all()

        assert first_job is not None
        assert first_job.drive_folder_url is None
        assert not first_job.artifacts
        assert not first_job.questions
        assert second_job is not None
        assert second_job.drive_folder_id == f"run-{second_job_id}"
        assert len(second_artifacts) == 3
        assert len(second_questions) == 1
    finally:
        db.close()


def test_process_job_fails_when_anthropic_config_is_missing(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"
    settings.anthropic_adenda_validacion_icsara_api_key = ""
    settings.anthropic_api_key = ""
    settings.anthropic_model = ""

    job_id = uuid4()
    job_dir = settings.data_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")
    _create_job(adenda_id=123, job_id=job_id, job_dir=job_dir)

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True, artifact_stem: str | None = None) -> ExtractionSummary:
        return _write_parser_outputs(Path(out_dir), stem=artifact_stem or "ICSARA_Demo")

    def fake_classification(observaciones_json_path: Path | str, out_dir: Path | str, artifact_stem: str | None = None) -> ClassificationSummary:
        output_json = Path(out_dir) / f"{artifact_stem}_clasificado.json"
        output_json.write_text("[]", encoding="utf-8")
        return ClassificationSummary(
            total=1,
            classified=1,
            unclassified=0,
            output_json=output_json,
            output_detail_json=Path(out_dir) / "resumen_clasificado.json",
        )

    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)

    result = pipeline_tasks.process_job(str(job_id))

    assert result["status"] == "failed"
    assert "ANTHROPIC_ADENDA_VALIDACION_ICSARA_API_KEY" in result["error"]


def test_process_job_marks_failure_and_cleans_drive_run_on_review_error(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"
    settings.anthropic_adenda_validacion_icsara_api_key = "test-key"
    settings.anthropic_model = "claude-test"

    job_id = uuid4()
    job_dir = settings.data_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")
    _create_job(adenda_id=444, job_id=job_id, job_dir=job_dir)

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True, artifact_stem: str | None = None) -> ExtractionSummary:
        return _write_parser_outputs(
            Path(out_dir),
            stem=artifact_stem or "ICSARA_Demo",
            table_files=["tabla_001.png"],
        )

    def fake_classification(observaciones_json_path: Path | str, out_dir: Path | str, artifact_stem: str | None = None) -> ClassificationSummary:
        output_json = Path(out_dir) / f"{artifact_stem}_clasificado.json"
        output_json.write_text("[]", encoding="utf-8")
        return ClassificationSummary(
            total=1,
            classified=1,
            unclassified=0,
            output_json=output_json,
            output_detail_json=Path(out_dir) / "resumen_clasificado.json",
        )

    def fake_review(*args, **kwargs) -> ReviewSummary:
        raise RuntimeError("Claude review failed")

    fake_drive = _FakeDriveService()
    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)
    monkeypatch.setattr(pipeline_tasks, "run_review", fake_review)
    monkeypatch.setattr(pipeline_tasks.GoogleDriveService, "from_settings", staticmethod(lambda: fake_drive))

    result = pipeline_tasks.process_job(str(job_id))

    assert result["status"] == "failed"
    assert not job_dir.exists()

    db = SessionLocal()
    try:
        job = job_service.get_job(db, job_id)
        assert job is not None
        assert job.status == "failed"
        assert db.query(JobArtifact).count() == 0
        assert db.query(Question).count() == 0
        assert db.query(QuestionMedia).count() == 0
    finally:
        db.close()

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.db.models import JobArtifact, Question, QuestionMedia
from app.db.session import SessionLocal
from app.pipeline.types import ClassificationSummary, ExtractionSummary
from app.services import job_service
from app.services.google_drive_service import DriveFileRef, DriveFolder, DriveUploadBundle
from app.tasks import pipeline_tasks


def _write_dataset(out_dir: Path, dataset: list[dict]) -> None:
    outputs_png = out_dir / "outputs_png"
    outputs_png.mkdir(parents=True, exist_ok=True)
    (out_dir / "preguntas.json").write_text("[]", encoding="utf-8")
    (out_dir / "preguntas_clasificadas.json").write_text(json.dumps(dataset), encoding="utf-8")
    for item in dataset:
        for media in item.get("tablas_figuras", []):
            (outputs_png / media["png"]).write_bytes(b"png")


class _FakeDriveService:
    def __init__(self) -> None:
        self.deleted_ids: list[str] = []

    def upload_job_outputs(self, *, adenda_id: int, job_id: UUID, classified_json_path: Path, media_dir: Path) -> DriveUploadBundle:
        media_files: dict[str, DriveFileRef] = {}
        for file_path in sorted(media_dir.glob("*.png")):
            media_files[file_path.name] = DriveFileRef(
                file_id=f"file-{job_id}-{file_path.name}",
                filename=file_path.name,
                mime_type="image/png",
                web_view_url=f"https://drive.example/{job_id}/{file_path.name}",
                preview_url=f"https://drive.example/{job_id}/{file_path.name}/preview",
                download_url=f"https://drive.example/{job_id}/{file_path.name}/download",
                size_bytes=file_path.stat().st_size,
                sha256=f"sha-{file_path.name}",
            )
        return DriveUploadBundle(
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
            json_file=DriveFileRef(
                file_id=f"json-{job_id}",
                filename="preguntas_clasificadas.json",
                mime_type="application/json",
                web_view_url=f"https://drive.example/run/{job_id}/json",
                preview_url=f"https://drive.example/run/{job_id}/json/preview",
                download_url=f"https://drive.example/run/{job_id}/json/download",
                size_bytes=classified_json_path.stat().st_size,
                sha256="sha-json",
            ),
            media_files=media_files,
        )

    def delete_file(self, file_id: str) -> None:
        self.deleted_ids.append(file_id)


def test_process_job_persists_questions_media_and_cleans_local_dir(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"

    job_id = uuid4()
    job_dir = settings.data_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")

    db = SessionLocal()
    try:
        job_service.create_job(
            db,
            job_id=job_id,
            adenda_id=777,
            original_filename="input.pdf",
            content_type="application/pdf",
            file_size_bytes=8,
            storage_path=job_dir,
            expires_at=job_service.utcnow() + timedelta(days=1),
        )
    finally:
        db.close()

    dataset = [
        {
            "numero": 1,
            "capitulo": "Capitulo 1",
            "bisagra": "Bisagra 1",
            "tema_principal": "Tema A",
            "tema_principal_id": "TEMA_A",
            "temas_principales": ["Tema A"],
            "temas_principales_id": ["TEMA_A"],
            "score": 5.0,
            "temas_secundarios": [],
            "keywords_match": ["tema a"],
            "texto": "Pregunta 1",
            "tablas_figuras": [{"tipo": "tabla", "parte": 1, "png": "p001_parte001_tabla.png"}],
        },
        {
            "numero": 2,
            "capitulo": "Capitulo 2",
            "bisagra": None,
            "tema_principal": "Tema B",
            "tema_principal_id": "TEMA_B",
            "temas_principales": ["Tema B"],
            "temas_principales_id": ["TEMA_B"],
            "score": 4.0,
            "temas_secundarios": [],
            "keywords_match": ["tema b"],
            "texto": "Pregunta 2",
            "tablas_figuras": [],
        },
    ]

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True) -> ExtractionSummary:
        out_path = Path(out_dir)
        _write_dataset(out_path, dataset)
        return ExtractionSummary(
            pages=1,
            capitulos=2,
            bisagras=1,
            preguntas=2,
            tablas=1,
            figuras=0,
            total_detections=1,
            output_dir=out_path,
        )

    def fake_classification(preguntas_json_path: Path | str, out_dir: Path | str) -> ClassificationSummary:
        out_path = Path(out_dir)
        return ClassificationSummary(
            total=2,
            classified=2,
            unclassified=0,
            output_json=out_path / "preguntas_clasificadas.json",
            output_detail_json=out_path / "preguntas_clasificadas_detalle.json",
        )

    fake_drive = _FakeDriveService()
    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)
    monkeypatch.setattr(pipeline_tasks.GoogleDriveService, "from_settings", staticmethod(lambda: fake_drive))

    result = pipeline_tasks.process_job(str(job_id))

    assert result["status"] == "done"
    assert result["drive_folder_url"] == f"https://drive.example/run/{job_id}"
    assert not job_dir.exists()

    db = SessionLocal()
    try:
        job = job_service.get_job(db, job_id)
        assert job is not None
        assert job.status == "done"
        assert job.drive_folder_id == f"run-{job_id}"
        questions = db.query(Question).order_by(Question.numero.asc()).all()
        media = db.query(QuestionMedia).order_by(QuestionMedia.filename.asc()).all()
        artifacts = db.query(JobArtifact).all()

        assert len(questions) == 2
        assert len(media) == 1
        assert questions[0].adenda_id == 777
        assert media[0].drive_file_id == f"file-{job_id}-p001_parte001_tabla.png"
        assert artifacts[0].storage_backend == "drive"
        assert artifacts[0].external_file_id == f"json-{job_id}"
    finally:
        db.close()


def test_process_job_replaces_previous_adenda_dataset(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"

    first_job_id = uuid4()
    second_job_id = uuid4()
    datasets = {
        str(first_job_id): [
            {
                "numero": 1,
                "capitulo": "Capitulo 1",
                "bisagra": None,
                "tema_principal": "Tema A",
                "tema_principal_id": "TEMA_A",
                "temas_principales": ["Tema A"],
                "temas_principales_id": ["TEMA_A"],
                "score": 5.0,
                "temas_secundarios": [],
                "keywords_match": ["tema a"],
                "texto": "Primera corrida",
                "tablas_figuras": [{"tipo": "tabla", "parte": 1, "png": "p001_parte001_tabla.png"}],
            }
        ],
        str(second_job_id): [
            {
                "numero": 3,
                "capitulo": "Capitulo 3",
                "bisagra": None,
                "tema_principal": "Tema C",
                "tema_principal_id": "TEMA_C",
                "temas_principales": ["Tema C"],
                "temas_principales_id": ["TEMA_C"],
                "score": 3.0,
                "temas_secundarios": [],
                "keywords_match": ["tema c"],
                "texto": "Segunda corrida",
                "tablas_figuras": [
                    {"tipo": "figura", "parte": 1, "png": "p003_parte001_figura.png"},
                    {"tipo": "figura", "parte": 2, "png": "p003_parte002_figura.png"},
                ],
            }
        ],
    }

    for job_id in (first_job_id, second_job_id):
        job_dir = settings.data_dir / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")
        db = SessionLocal()
        try:
            job_service.create_job(
                db,
                job_id=job_id,
                adenda_id=999,
                original_filename="input.pdf",
                content_type="application/pdf",
                file_size_bytes=8,
                storage_path=job_dir,
                expires_at=job_service.utcnow() + timedelta(days=1),
            )
        finally:
            db.close()

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True) -> ExtractionSummary:
        job_id = Path(out_dir).parent.name
        dataset = datasets[job_id]
        out_path = Path(out_dir)
        _write_dataset(out_path, dataset)
        total_media = sum(len(item.get("tablas_figuras", [])) for item in dataset)
        return ExtractionSummary(
            pages=1,
            capitulos=1,
            bisagras=1,
            preguntas=len(dataset),
            tablas=total_media,
            figuras=0,
            total_detections=total_media,
            output_dir=out_path,
        )

    def fake_classification(preguntas_json_path: Path | str, out_dir: Path | str) -> ClassificationSummary:
        dataset = datasets[Path(out_dir).parent.name]
        return ClassificationSummary(
            total=len(dataset),
            classified=len(dataset),
            unclassified=0,
            output_json=Path(out_dir) / "preguntas_clasificadas.json",
            output_detail_json=Path(out_dir) / "preguntas_clasificadas_detalle.json",
        )

    fake_drive = _FakeDriveService()
    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)
    monkeypatch.setattr(pipeline_tasks.GoogleDriveService, "from_settings", staticmethod(lambda: fake_drive))

    first_result = pipeline_tasks.process_job(str(first_job_id))
    second_result = pipeline_tasks.process_job(str(second_job_id))

    assert first_result["status"] == "done"
    assert second_result["status"] == "done"
    assert f"run-{first_job_id}" in fake_drive.deleted_ids

    db = SessionLocal()
    try:
        questions = db.query(Question).order_by(Question.numero.asc()).all()
        media = db.query(QuestionMedia).order_by(QuestionMedia.parte.asc()).all()
        first_job = job_service.get_job(db, first_job_id)
        second_job = job_service.get_job(db, second_job_id)

        assert len(questions) == 1
        assert questions[0].job_id == second_job_id
        assert questions[0].numero == 3
        assert len(media) == 2
        assert [item.parte for item in media] == [1, 2]
        assert first_job is not None
        assert first_job.drive_folder_url is None
        assert not first_job.artifacts
        assert second_job is not None
        assert second_job.drive_folder_id == f"run-{second_job_id}"
    finally:
        db.close()


def test_process_job_marks_failure_and_cleans_drive_run_on_persist_error(monkeypatch, tmp_path) -> None:
    settings = pipeline_tasks.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.google_drive_parent_folder_id = "parent-folder"

    job_id = uuid4()
    job_dir = settings.data_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input.pdf").write_bytes(b"%PDF-1.4")

    db = SessionLocal()
    try:
        job_service.create_job(
            db,
            job_id=job_id,
            adenda_id=444,
            original_filename="input.pdf",
            content_type="application/pdf",
            file_size_bytes=8,
            storage_path=job_dir,
            expires_at=job_service.utcnow() + timedelta(days=1),
        )
    finally:
        db.close()

    dataset = [
        {
            "numero": 1,
            "capitulo": "Capitulo 1",
            "bisagra": None,
            "tema_principal": "Tema A",
            "tema_principal_id": "TEMA_A",
            "temas_principales": ["Tema A"],
            "temas_principales_id": ["TEMA_A"],
            "score": 5.0,
            "temas_secundarios": [],
            "keywords_match": ["tema a"],
            "texto": "Pregunta 1",
            "tablas_figuras": [{"tipo": "tabla", "parte": 1, "png": "p001_parte001_tabla.png"}],
        }
    ]

    def fake_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True) -> ExtractionSummary:
        out_path = Path(out_dir)
        _write_dataset(out_path, dataset)
        return ExtractionSummary(
            pages=1,
            capitulos=1,
            bisagras=1,
            preguntas=1,
            tablas=1,
            figuras=0,
            total_detections=1,
            output_dir=out_path,
        )

    def fake_classification(preguntas_json_path: Path | str, out_dir: Path | str) -> ClassificationSummary:
        out_path = Path(out_dir)
        return ClassificationSummary(
            total=1,
            classified=1,
            unclassified=0,
            output_json=out_path / "preguntas_clasificadas.json",
            output_detail_json=out_path / "preguntas_clasificadas_detalle.json",
        )

    fake_drive = _FakeDriveService()
    monkeypatch.setattr(pipeline_tasks, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline_tasks, "run_classification", fake_classification)
    monkeypatch.setattr(pipeline_tasks.GoogleDriveService, "from_settings", staticmethod(lambda: fake_drive))
    monkeypatch.setattr(
        pipeline_tasks,
        "publish_job_results",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db failure")),
    )

    result = pipeline_tasks.process_job(str(job_id))

    assert result["status"] == "failed"
    assert f"run-{job_id}" in fake_drive.deleted_ids
    assert not job_dir.exists()

    db = SessionLocal()
    try:
        job = job_service.get_job(db, job_id)
        assert job is not None
        assert job.status == "failed"
        assert db.query(Question).count() == 0
    finally:
        db.close()

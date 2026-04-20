from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.routes import jobs as jobs_routes
from app.db.session import SessionLocal
from app.main import app
from app.services import job_service


class _FakeDriveService:
    def __init__(self) -> None:
        self.files = {
            "drive-revisado": b'{"ok": true}',
            "drive-audit": b'{"audit": true}',
            "drive-md": b"# reporte\n",
        }

    def download_file_bytes(self, file_id: str) -> bytes:
        return self.files[file_id]


def test_result_wrapper_and_dynamic_artifact_downloads(monkeypatch, tmp_path) -> None:
    job_id = uuid4()
    job_storage = tmp_path / str(job_id)
    job_storage.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        job = job_service.create_job(
            db,
            job_id=job_id,
            adenda_id=123,
            original_filename="ICSARA Demo.pdf",
            content_type="application/pdf",
            file_size_bytes=10,
            storage_path=job_storage,
            expires_at=job_service.utcnow() + timedelta(days=1),
        )
        job = job_service.update_job(
            db,
            job,
            status="done",
            stage="finalizing",
            progress=100,
            summary={"observaciones": 1, "correcciones_revision": 1},
            finished_at=job_service.utcnow(),
        )
        job_service.add_artifact(
            db,
            job_id=job_id,
            name="ICSARA_Demo_revisado.json",
            path=None,
            size_bytes=12,
            storage_backend="drive",
            mime_type="application/json",
            external_file_id="drive-revisado",
        )
        job_service.add_artifact(
            db,
            job_id=job_id,
            name="auditoria_cambios.json",
            path=None,
            size_bytes=12,
            storage_backend="drive",
            mime_type="application/json",
            external_file_id="drive-audit",
        )
        job_service.add_artifact(
            db,
            job_id=job_id,
            name="verificacion_icsara.md",
            path=None,
            size_bytes=12,
            storage_backend="drive",
            mime_type="text/markdown",
            external_file_id="drive-md",
        )
    finally:
        db.close()

    monkeypatch.setattr(jobs_routes.GoogleDriveService, "from_settings", staticmethod(lambda: _FakeDriveService()))

    with TestClient(app) as client:
        wrapper = client.get(
            f"/v1/jobs/{job_id}/result",
            headers={"X-API-Key": "change-this-key"},
        )
        assert wrapper.status_code == 200
        payload = wrapper.json()
        assert payload["summary"]["observaciones"] == 1
        assert set(payload["artifacts"]) == {
            "ICSARA_Demo_revisado.json",
            "auditoria_cambios.json",
            "verificacion_icsara.md",
        }

        legacy = client.get(
            f"/v1/jobs/{job_id}/result/preguntas_clasificadas.json",
            headers={"X-API-Key": "change-this-key"},
        )
        assert legacy.status_code == 200
        assert legacy.json() == {"ok": True}

        legacy_alias = client.get(
            f"/v1/jobs/{job_id}/preguntas_clasificadas",
            headers={"X-API-Key": "change-this-key"},
        )
        assert legacy_alias.status_code == 200
        assert legacy_alias.json() == {"ok": True}

        audit = client.get(
            f"/v1/jobs/{job_id}/artifacts/auditoria_cambios.json",
            headers={"X-API-Key": "change-this-key"},
        )
        assert audit.status_code == 200
        assert audit.json() == {"audit": True}

        report = client.get(
            f"/v1/jobs/{job_id}/artifacts/verificacion_icsara.md",
            headers={"X-API-Key": "change-this-key"},
        )
        assert report.status_code == 200
        assert report.text == "# reporte\n"

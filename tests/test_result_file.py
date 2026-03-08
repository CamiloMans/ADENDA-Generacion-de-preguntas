from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.api.routes import jobs as jobs_routes
from app.db.session import SessionLocal
from app.services import job_service


class _FakeDriveService:
    def download_file_bytes(self, file_id: str) -> bytes:
        assert file_id == "drive-file-123"
        return b'{"ok": true}'


def test_result_direct_json_file_endpoint(monkeypatch, tmp_path) -> None:
    job_id = uuid4()
    job_storage = tmp_path / str(job_id)
    job_storage.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        job = job_service.create_job(
            db,
            job_id=job_id,
            adenda_id=123,
            original_filename="test.pdf",
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
            summary={"preguntas": 1},
            finished_at=job_service.utcnow(),
        )
        job_service.add_artifact(
            db,
            job_id=job_id,
            name="preguntas_clasificadas.json",
            path=None,
            size_bytes=12,
            storage_backend="drive",
            mime_type="application/json",
            external_file_id="drive-file-123",
        )
    finally:
        db.close()

    monkeypatch.setattr(jobs_routes.GoogleDriveService, "from_settings", staticmethod(lambda: _FakeDriveService()))

    with TestClient(app) as client:
        response = client.get(
            f"/v1/jobs/{job_id}/result/preguntas_clasificadas.json",
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("application/json")
        assert response.json() == {"ok": True}

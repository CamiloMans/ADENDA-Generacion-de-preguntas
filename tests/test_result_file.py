from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.db.session import SessionLocal
from app.services import job_service


def test_result_direct_json_file_endpoint(tmp_path) -> None:
    job_id = uuid4()
    job_storage = tmp_path / str(job_id)
    job_storage.mkdir(parents=True, exist_ok=True)
    artifact_path = tmp_path / "preguntas_clasificadas.json"
    artifact_path.write_text('{"ok": true}', encoding="utf-8")

    db = SessionLocal()
    try:
        job = job_service.create_job(
            db,
            job_id=job_id,
            original_filename="test.pdf",
            content_type="application/pdf",
            file_size_bytes=10,
            storage_path=job_storage,
            expires_at=job_service.utcnow() + timedelta(days=1),
        )
        job_service.mark_done(db, job, summary={"preguntas": 1})
        job_service.add_artifact(
            db,
            job_id=job_id,
            name="preguntas_clasificadas.json",
            path=artifact_path,
            size_bytes=artifact_path.stat().st_size,
        )
    finally:
        db.close()

    with TestClient(app) as client:
        response = client.get(
            f"/v1/jobs/{job_id}/result/preguntas_clasificadas.json",
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("application/json")
        assert response.json() == {"ok": True}

from fastapi.testclient import TestClient
from uuid import UUID

from app.main import app
from app.api.routes import jobs as jobs_routes
from app.db.session import SessionLocal
from app.services import job_service


class _FakeRedis:
    def ping(self) -> bool:
        return True


def test_jobs_require_api_key() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/jobs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 401


def test_jobs_not_found_with_valid_api_key() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/v1/jobs/00000000-0000-0000-0000-000000000000",
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 404


def test_create_job_rejects_non_pdf() -> None:
    with TestClient(app) as client:
        files = {"file": ("bad.txt", b"hello", "text/plain")}
        response = client.post(
            "/v1/jobs",
            files=files,
            data={"id_adenda": "123"},
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 400


def test_create_job_requires_id_adenda() -> None:
    with TestClient(app) as client:
        files = {"file": ("input.pdf", b"%PDF-1.4", "application/pdf")}
        response = client.post(
            "/v1/jobs",
            files=files,
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 422


def test_create_job_rejects_disabled_pipeline_flags() -> None:
    with TestClient(app) as client:
        files = {"file": ("input.pdf", b"%PDF-1.4", "application/pdf")}
        response = client.post(
            "/v1/jobs",
            files=files,
            data={"id_adenda": "123", "classify": "false"},
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 400
        assert "classify=true" in response.json()["detail"]

        response = client.post(
            "/v1/jobs",
            files=files,
            data={"id_adenda": "123", "include_png": "false"},
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 400
        assert "include_png=true" in response.json()["detail"]


def test_create_job_persists_adenda_id(monkeypatch, tmp_path) -> None:
    settings = jobs_routes.get_settings()
    settings.data_dir = tmp_path / "jobs"
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(jobs_routes.Redis, "from_url", staticmethod(lambda *args, **kwargs: _FakeRedis()))
    monkeypatch.setattr(jobs_routes.celery_app, "send_task", lambda *args, **kwargs: None)

    with TestClient(app) as client:
        files = {"file": ("input.pdf", b"%PDF-1.4", "application/pdf")}
        response = client.post(
            "/v1/jobs",
            files=files,
            data={"id_adenda": "321"},
            headers={"X-API-Key": "change-this-key"},
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["adenda_id"] == 321

    db = SessionLocal()
    try:
        job = job_service.get_job(db, UUID(payload["job_id"]))
        assert job is not None
        assert job.adenda_id == 321
    finally:
        db.close()

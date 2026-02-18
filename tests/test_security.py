from fastapi.testclient import TestClient

from app.main import app


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
            headers={"X-API-Key": "change-this-key"},
        )
        assert response.status_code == 400

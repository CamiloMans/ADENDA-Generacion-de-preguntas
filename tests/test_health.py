from fastapi.testclient import TestClient

from app.main import app


def test_health_live() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

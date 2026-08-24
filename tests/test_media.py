from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.routes import media as media_routes
from app.db.models import Job, Question, QuestionMedia
from app.db.session import SessionLocal
from app.main import app
from app.services.google_drive_service import DriveServiceError

API_KEY_HEADERS = {"X-API-Key": "change-this-key"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _FakeDriveService:
    def __init__(self, *, content: bytes | None = None, error: Exception | None = None) -> None:
        self._content = content or b""
        self._error = error
        self.requested_file_ids: list[str] = []

    def download_file_bytes(self, file_id: str) -> bytes:
        self.requested_file_ids.append(file_id)
        if self._error:
            raise self._error
        return self._content


def _patch_drive(monkeypatch, fake: _FakeDriveService) -> _FakeDriveService:
    monkeypatch.setattr(
        media_routes.GoogleDriveService,
        "from_settings",
        classmethod(lambda cls: fake),
    )
    return fake


def _create_media(**overrides) -> int:
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        job = Job(
            id=uuid4(),
            adenda_id=1,
            status="succeeded",
            stage="done",
            progress=100,
            original_filename="input.pdf",
            content_type="application/pdf",
            file_size_bytes=1024,
            storage_path="/tmp/input.pdf",
            expires_at=now + timedelta(days=7),
        )
        db.add(job)
        db.flush()

        question = Question(job_id=job.id, adenda_id=1, numero=1, capitulo="", texto="Observacion 1")
        db.add(question)
        db.flush()

        fields = {
            "question_id": question.id,
            "filename": "table_016_part1_p036.png",
            "tipo": "tabla",
            "parte": 1,
            "mime_type": "image/png",
            "drive_file_id": "drive-file-id",
            "drive_preview_url": "https://drive.google.com/file/d/drive-file-id/preview",
            "drive_web_view_url": "https://drive.google.com/file/d/drive-file-id/view",
        }
        fields.update(overrides)

        media = QuestionMedia(**fields)
        db.add(media)
        db.commit()
        return media.id
    finally:
        db.close()


def test_media_requires_api_key() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/media/1/content")
        assert response.status_code == 401


def test_media_not_found() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/media/999999/content", headers=API_KEY_HEADERS)
        assert response.status_code == 404


def test_media_rejects_non_integer_id() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/media/abc/content", headers=API_KEY_HEADERS)
        assert response.status_code == 422


def test_media_returns_image_bytes(monkeypatch) -> None:
    media_id = _create_media()
    fake = _patch_drive(monkeypatch, _FakeDriveService(content=PNG_BYTES))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG_BYTES))
    assert response.content == PNG_BYTES
    assert fake.requested_file_ids == ["drive-file-id"]


def test_media_infers_mime_type_from_filename(monkeypatch) -> None:
    media_id = _create_media(mime_type=None)
    _patch_drive(monkeypatch, _FakeDriveService(content=PNG_BYTES))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_media_rejects_unsupported_tipo(monkeypatch) -> None:
    media_id = _create_media(tipo="anexo")
    _patch_drive(monkeypatch, _FakeDriveService(content=PNG_BYTES))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 400


def test_media_rejects_unsupported_mime_type(monkeypatch) -> None:
    media_id = _create_media(filename="tabla.pdf", mime_type="application/pdf")
    _patch_drive(monkeypatch, _FakeDriveService(content=PNG_BYTES))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 400


def test_media_drive_failure_returns_502(monkeypatch) -> None:
    media_id = _create_media()
    _patch_drive(monkeypatch, _FakeDriveService(error=DriveServiceError("boom")))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 502


def test_media_empty_file_returns_502(monkeypatch) -> None:
    media_id = _create_media()
    _patch_drive(monkeypatch, _FakeDriveService(content=b""))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 502


def test_media_oversized_file_returns_413(monkeypatch) -> None:
    media_id = _create_media()
    _patch_drive(monkeypatch, _FakeDriveService(content=b"0" * (media_routes.MAX_MEDIA_BYTES + 1)))

    with TestClient(app) as client:
        response = client.get(f"/v1/media/{media_id}/content", headers=API_KEY_HEADERS)

    assert response.status_code == 413

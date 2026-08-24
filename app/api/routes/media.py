from __future__ import annotations

import mimetypes
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.security import require_api_key
from app.db.models import QuestionMedia
from app.db.session import get_db
from app.services.google_drive_service import DriveServiceError, GoogleDriveService

router = APIRouter(prefix="/media", tags=["media"], dependencies=[Depends(require_api_key)])

ALLOWED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
ALLOWED_MEDIA_TYPES: Final[frozenset[str]] = frozenset({"tabla", "figura"})
MAX_MEDIA_BYTES: Final[int] = 5 * 1024 * 1024


def _resolve_mime_type(media: QuestionMedia) -> str:
    declared = (media.mime_type or "").split(";", 1)[0].strip().lower()
    if declared:
        return declared
    guessed, _ = mimetypes.guess_type(media.filename or "")
    return (guessed or "").lower()


@router.get("/{media_id}/content")
def get_media_content(media_id: int, db: Session = Depends(get_db)) -> Response:
    media = db.get(QuestionMedia, media_id)
    if not media:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found.")

    if (media.tipo or "").strip().lower() not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Media {media_id} is not a table or figure.",
        )

    mime_type = _resolve_mime_type(media)
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Media {media_id} has an unsupported content type.",
        )

    file_id = (media.drive_file_id or "").strip()
    if not file_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media {media_id} has no remote file id.",
        )

    try:
        drive_service = GoogleDriveService.from_settings()
        content = drive_service.download_file_bytes(file_id)
    except DriveServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not retrieve media {media_id} from Google Drive.",
        ) from exc

    if not content:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google Drive returned an empty file for media {media_id}.",
        )
    if len(content) > MAX_MEDIA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Media {media_id} exceeds the 5 MB limit.",
        )

    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Length": str(len(content)),
            "Cache-Control": "private, max-age=3600",
        },
    )

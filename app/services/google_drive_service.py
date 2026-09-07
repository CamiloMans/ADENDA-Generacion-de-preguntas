from __future__ import annotations

import hashlib
import io
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from app.core.config import get_settings

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class DriveServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class DriveFolder:
    folder_id: str
    name: str
    web_view_url: str


@dataclass(slots=True)
class DriveFileRef:
    file_id: str
    filename: str
    mime_type: str
    web_view_url: str
    preview_url: str
    download_url: str
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class DriveChild:
    file_id: str
    name: str
    mime_type: str
    web_view_url: str

    @property
    def is_folder(self) -> bool:
        return self.mime_type == DRIVE_FOLDER_MIME_TYPE


@dataclass(slots=True)
class DriveRunContext:
    adenda_folder: DriveFolder
    run_folder: DriveFolder
    media_folder: DriveFolder


@dataclass(slots=True)
class DriveUploadBundle:
    adenda_folder: DriveFolder
    run_folder: DriveFolder
    artifact_files: dict[str, DriveFileRef]
    media_files: dict[str, DriveFileRef]


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class GoogleDriveService:
    def __init__(self, service: Resource) -> None:
        self._service = service

    @classmethod
    def from_settings(cls) -> "GoogleDriveService":
        settings = get_settings()
        token_file = settings.google_token_file
        client_secret_file = settings.google_client_secret_file

        creds: Credentials | None = None
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), DRIVE_SCOPES)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")

        if not creds or not creds.valid:
            if not client_secret_file.exists():
                raise DriveServiceError(f"Google client secret file not found: {client_secret_file}")
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), DRIVE_SCOPES)
                creds = flow.run_local_server(port=0)
            except Exception as exc:  # noqa: BLE001
                raise DriveServiceError("Google Drive credentials are invalid and interactive auth failed.") from exc
            token_file.write_text(creds.to_json(), encoding="utf-8")

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return cls(service)

    def create_job_run_context(self, *, adenda_id: int, job_id: UUID) -> DriveRunContext:
        settings = get_settings()
        parent_folder_id = settings.google_drive_parent_folder_id.strip()
        if not parent_folder_id:
            raise DriveServiceError("GOOGLE_DRIVE_PARENT_FOLDER_ID is required.")

        adenda_folder = self.ensure_folder(name=f"adenda_{adenda_id}", parent_id=parent_folder_id)
        run_folder = self.create_folder(name=f"run_{job_id}", parent_id=adenda_folder.folder_id)
        media_folder = self.create_folder(name="media", parent_id=run_folder.folder_id)
        return DriveRunContext(
            adenda_folder=adenda_folder,
            run_folder=run_folder,
            media_folder=media_folder,
        )

    def upload_job_outputs(self, *, adenda_id: int, job_id: UUID, classified_json_path: Path, media_dir: Path) -> DriveUploadBundle:
        if not classified_json_path.exists():
            raise DriveServiceError(f"Missing classified JSON output: {classified_json_path}")

        context = self.create_job_run_context(adenda_id=adenda_id, job_id=job_id)
        try:
            artifact_files = self.upload_artifact_files(
                files=[classified_json_path],
                parent_id=context.run_folder.folder_id,
            )
            media_files = self.upload_directory_files(
                directory=media_dir,
                parent_id=context.media_folder.folder_id,
            )

            return DriveUploadBundle(
                adenda_folder=context.adenda_folder,
                run_folder=context.run_folder,
                artifact_files=artifact_files,
                media_files=media_files,
            )
        except Exception:
            try:
                self.delete_file(context.run_folder.folder_id)
            except DriveServiceError:
                pass
            raise

    def upload_directory_files(self, *, directory: Path, parent_id: str) -> dict[str, DriveFileRef]:
        uploaded_files: dict[str, DriveFileRef] = {}
        if not directory.exists():
            return uploaded_files

        for file_path in sorted(directory.glob("*")):
            if not file_path.is_file():
                continue
            mime_type = _guess_mime_type(file_path)
            uploaded_files[str(file_path.resolve())] = self.upload_file(
                path=file_path,
                parent_id=parent_id,
                mime_type=mime_type,
            )
        return uploaded_files

    def upload_artifact_files(self, *, files: list[Path], parent_id: str) -> dict[str, DriveFileRef]:
        uploaded_files: dict[str, DriveFileRef] = {}
        for file_path in files:
            if not file_path.exists():
                raise DriveServiceError(f"Missing artifact output: {file_path}")
            uploaded_files[file_path.name] = self.upload_file(
                path=file_path,
                parent_id=parent_id,
                mime_type=_guess_mime_type(file_path),
            )
        return uploaded_files

    def ensure_folder(self, *, name: str, parent_id: str) -> DriveFolder:
        escaped_name = name.replace("'", "\\'")
        query = (
            f"trashed=false and mimeType='{DRIVE_FOLDER_MIME_TYPE}' "
            f"and name='{escaped_name}' and '{parent_id}' in parents"
        )
        try:
            response = self._service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=10,
                corpora="allDrives",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveServiceError("Could not query Google Drive folders.") from exc

        files = response.get("files", [])
        if files:
            folder = files[0]
            return DriveFolder(
                folder_id=folder["id"],
                name=folder["name"],
                web_view_url=self.folder_url(folder["id"]),
            )
        return self.create_folder(name=name, parent_id=parent_id)

    def create_folder(self, *, name: str, parent_id: str) -> DriveFolder:
        metadata = {
            "name": name,
            "mimeType": DRIVE_FOLDER_MIME_TYPE,
            "parents": [parent_id],
        }
        try:
            folder = self._service.files().create(
                body=metadata,
                fields="id, name",
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveServiceError(f"Could not create Google Drive folder '{name}'.") from exc
        return DriveFolder(
            folder_id=folder["id"],
            name=folder["name"],
            web_view_url=self.folder_url(folder["id"]),
        )

    def upload_file(self, *, path: Path, parent_id: str, mime_type: str) -> DriveFileRef:
        metadata = {
            "name": path.name,
            "parents": [parent_id],
        }
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
        try:
            uploaded = self._service.files().create(
                body=metadata,
                media_body=media,
                fields="id, name, mimeType, webViewLink",
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveServiceError(f"Could not upload '{path.name}' to Google Drive.") from exc

        file_id = uploaded["id"]
        return DriveFileRef(
            file_id=file_id,
            filename=uploaded["name"],
            mime_type=uploaded.get("mimeType", mime_type),
            web_view_url=uploaded.get("webViewLink") or self.file_view_url(file_id),
            preview_url=self.file_preview_url(file_id),
            download_url=self.file_download_url(file_id),
            size_bytes=path.stat().st_size,
            sha256=_sha256_file(path),
        )

    def download_file_bytes(self, file_id: str) -> bytes:
        request = self._service.files().get_media(fileId=file_id, supportsAllDrives=True)
        output = io.BytesIO()
        downloader = MediaIoBaseDownload(output, request)
        done = False
        try:
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            raise DriveServiceError(f"Could not download Google Drive file '{file_id}'.") from exc
        return output.getvalue()

    def delete_file(self, file_id: str) -> None:
        try:
            self._service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except HttpError as exc:
            raise DriveServiceError(f"Could not delete Google Drive file '{file_id}'.") from exc

    def list_children(self, folder_id: str) -> list[DriveChild]:
        """Return every non-trashed direct child (files and folders) of a folder."""
        children: list[DriveChild] = []
        page_token: str | None = None
        while True:
            try:
                response = self._service.files().list(
                    q=f"trashed=false and '{folder_id}' in parents",
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                    pageSize=1000,
                    pageToken=page_token,
                    corpora="allDrives",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                ).execute()
            except HttpError as exc:
                raise DriveServiceError(f"Could not list Google Drive folder '{folder_id}'.") from exc
            for item in response.get("files", []):
                children.append(
                    DriveChild(
                        file_id=item["id"],
                        name=item["name"],
                        mime_type=item.get("mimeType", ""),
                        web_view_url=item.get("webViewLink") or self.file_view_url(item["id"]),
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                return children

    def copy_file(self, *, file_id: str, name: str, parent_id: str) -> DriveChild:
        """Server-side copy of a file into another folder (works across shared drives)."""
        try:
            copied = self._service.files().copy(
                fileId=file_id,
                body={"name": name, "parents": [parent_id]},
                fields="id, name, mimeType, webViewLink",
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveServiceError(f"Could not copy Google Drive file '{file_id}'.") from exc
        return DriveChild(
            file_id=copied["id"],
            name=copied["name"],
            mime_type=copied.get("mimeType", ""),
            web_view_url=copied.get("webViewLink") or self.file_view_url(copied["id"]),
        )

    def update_file_content(self, *, file_id: str, content: bytes, mime_type: str) -> None:
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        try:
            self._service.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveServiceError(f"Could not update Google Drive file '{file_id}'.") from exc

    @staticmethod
    def folder_url(folder_id: str) -> str:
        return f"https://drive.google.com/drive/folders/{folder_id}"

    @staticmethod
    def file_view_url(file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"

    @staticmethod
    def file_preview_url(file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/preview"

    @staticmethod
    def file_download_url(file_id: str) -> str:
        return f"https://drive.google.com/uc?id={file_id}&export=download"


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    return mime_type or "application/octet-stream"

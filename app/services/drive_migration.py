"""Copy an ICSARA Drive tree to a new parent folder and remap DB references.

Used by ``scripts/migrate_drive_parent.py``. Google Drive cannot *move* files
between shared drives unless the caller is a drive manager, so the migration
copies every file server-side (``files.copy``), producing new file ids, and then
rewrites every place those ids are stored:

* ``pregunta_media.drive_file_id`` / ``drive_preview_url`` / ``drive_web_view_url``
* ``job_artifacts.external_file_id`` / ``external_url`` (``storage_backend='drive'``)
* ``jobs.drive_folder_id`` / ``drive_folder_url``
* text inside the copied ``*.json`` / ``*.md`` artifacts (media URLs embedded in
  the reviewed JSON).

The mapping ``old_id -> new_id`` is persisted to a JSON file after every copied
item so the run can be resumed. Nothing in the source folder is ever deleted.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobArtifact, QuestionMedia
from app.services.google_drive_service import (
    DriveChild,
    DriveFolder,
    DriveServiceError,
    GoogleDriveService,
)

logger = logging.getLogger(__name__)

TEXT_MIME_TYPES = frozenset({"application/json", "text/markdown", "text/plain"})
RETRYABLE_ATTEMPTS = 5


class DriveClient(Protocol):
    def list_children(self, folder_id: str) -> list[DriveChild]: ...

    def ensure_folder(self, *, name: str, parent_id: str) -> DriveFolder: ...

    def copy_file(self, *, file_id: str, name: str, parent_id: str) -> DriveChild: ...

    def download_file_bytes(self, file_id: str) -> bytes: ...

    def update_file_content(self, *, file_id: str, content: bytes, mime_type: str) -> None: ...


@dataclass(slots=True)
class MigrationMapping:
    source_id: str
    target_id: str
    folders: dict[str, str] = field(default_factory=dict)
    files: dict[str, dict[str, str]] = field(default_factory=dict)
    rewritten: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, *, source_id: str, target_id: str) -> "MigrationMapping":
        if not path.exists():
            return cls(source_id=source_id, target_id=target_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("source_id") != source_id or raw.get("target_id") != target_id:
            raise ValueError(
                f"Mapping file {path} belongs to a different migration "
                f"({raw.get('source_id')} -> {raw.get('target_id')})."
            )
        return cls(
            source_id=source_id,
            target_id=target_id,
            folders=dict(raw.get("folders", {})),
            files={k: dict(v) for k, v in raw.get("files", {}).items()},
            rewritten=list(raw.get("rewritten", [])),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "folders": self.folders,
            "files": self.files,
            "rewritten": self.rewritten,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def id_replacements(self) -> dict[str, str]:
        replacements = dict(self.folders)
        replacements.update({old: new["id"] for old, new in self.files.items()})
        return replacements


@dataclass(slots=True)
class MigrationReport:
    folders_seen: int = 0
    folders_created: int = 0
    files_seen: int = 0
    files_copied: int = 0
    files_reused: int = 0
    text_files_rewritten: int = 0
    media_rows_updated: int = 0
    artifact_rows_updated: int = 0
    job_rows_updated: int = 0
    orphans: dict[str, list[str]] = field(default_factory=lambda: {"media": [], "artifacts": [], "jobs": []})

    def as_dict(self) -> dict[str, Any]:
        return {
            "folders_seen": self.folders_seen,
            "folders_created": self.folders_created,
            "files_seen": self.files_seen,
            "files_copied": self.files_copied,
            "files_reused": self.files_reused,
            "text_files_rewritten": self.text_files_rewritten,
            "media_rows_updated": self.media_rows_updated,
            "artifact_rows_updated": self.artifact_rows_updated,
            "job_rows_updated": self.job_rows_updated,
            "orphans": self.orphans,
        }


def _with_retry(action: Callable[[], Any], *, what: str) -> Any:
    delay = 1.0
    for attempt in range(1, RETRYABLE_ATTEMPTS + 1):
        try:
            return action()
        except DriveServiceError as exc:
            if attempt == RETRYABLE_ATTEMPTS:
                raise
            logger.warning("%s failed (attempt %s/%s): %s. Retrying in %.0fs", what, attempt, RETRYABLE_ATTEMPTS, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise AssertionError("unreachable")


def copy_tree(
    drive: DriveClient,
    *,
    mapping: MigrationMapping,
    mapping_path: Path | None,
    apply: bool,
    report: MigrationReport,
    log: Callable[[str], None] = logger.info,
) -> None:
    """Depth-first copy of ``mapping.source_id`` into ``mapping.target_id``."""
    stack: list[tuple[str, str]] = [(mapping.source_id, mapping.target_id)]
    while stack:
        source_folder_id, target_folder_id = stack.pop()
        children = _with_retry(lambda: drive.list_children(source_folder_id), what=f"list {source_folder_id}")
        existing_by_name: dict[str, DriveChild] = {}
        if apply:
            for child in _with_retry(lambda: drive.list_children(target_folder_id), what=f"list {target_folder_id}"):
                existing_by_name.setdefault(child.name, child)

        for child in sorted(children, key=lambda item: item.name):
            if child.is_folder:
                report.folders_seen += 1
                new_folder_id = mapping.folders.get(child.file_id)
                if new_folder_id is None:
                    if not apply:
                        log(f"[dry-run] folder {child.name} ({child.file_id}) -> would create under {target_folder_id}")
                        new_folder_id = f"dry-run:{child.file_id}"
                        mapping.folders[child.file_id] = new_folder_id
                    else:
                        existing = existing_by_name.get(child.name)
                        if existing is not None and existing.is_folder:
                            new_folder_id = existing.file_id
                        else:
                            folder = _with_retry(
                                lambda: drive.ensure_folder(name=child.name, parent_id=target_folder_id),
                                what=f"ensure folder {child.name}",
                            )
                            new_folder_id = folder.folder_id
                            report.folders_created += 1
                        mapping.folders[child.file_id] = new_folder_id
                        if mapping_path:
                            mapping.save(mapping_path)
                stack.append((child.file_id, new_folder_id))
                continue

            report.files_seen += 1
            if child.file_id in mapping.files:
                report.files_reused += 1
                continue
            if not apply:
                log(f"[dry-run] file {child.name} ({child.file_id}) -> would copy under {target_folder_id}")
                # In-memory placeholder so the DB pass can report what would change. Never persisted.
                mapping.files[child.file_id] = {
                    "id": f"dry-run:{child.file_id}",
                    "name": child.name,
                    "mime_type": child.mime_type,
                    "web_view_url": "",
                }
                continue

            existing = existing_by_name.get(child.name)
            if existing is not None and not existing.is_folder:
                copied = existing
                report.files_reused += 1
            else:
                copied = _with_retry(
                    lambda: drive.copy_file(file_id=child.file_id, name=child.name, parent_id=target_folder_id),
                    what=f"copy {child.name}",
                )
                report.files_copied += 1
                log(f"copied {child.name}: {child.file_id} -> {copied.file_id}")
            mapping.files[child.file_id] = {
                "id": copied.file_id,
                "name": child.name,
                "mime_type": child.mime_type or copied.mime_type,
                "web_view_url": copied.web_view_url,
            }
            if mapping_path:
                mapping.save(mapping_path)


def rewrite_text_artifacts(
    drive: DriveClient,
    *,
    mapping: MigrationMapping,
    mapping_path: Path | None,
    report: MigrationReport,
    log: Callable[[str], None] = logger.info,
) -> None:
    """Replace old Drive ids inside copied JSON/Markdown artifacts."""
    replacements = mapping.id_replacements()
    for old_id, info in mapping.files.items():
        new_id = info["id"]
        if info.get("mime_type") not in TEXT_MIME_TYPES or new_id in mapping.rewritten:
            continue
        raw = _with_retry(lambda: drive.download_file_bytes(new_id), what=f"download {info['name']}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping non-UTF8 artifact %s (%s)", info["name"], new_id)
            mapping.rewritten.append(new_id)
            continue
        updated = text
        for source, target in replacements.items():
            if source in updated:
                updated = updated.replace(source, target)
        if updated != text:
            _with_retry(
                lambda: drive.update_file_content(
                    file_id=new_id,
                    content=updated.encode("utf-8"),
                    mime_type=info.get("mime_type") or "text/plain",
                ),
                what=f"update {info['name']}",
            )
            report.text_files_rewritten += 1
            log(f"rewrote ids inside {info['name']} ({old_id} -> {new_id})")
        mapping.rewritten.append(new_id)
        if mapping_path:
            mapping.save(mapping_path)


def remap_database(
    db: Session,
    *,
    mapping: MigrationMapping,
    apply: bool,
    report: MigrationReport,
    log: Callable[[str], None] = logger.info,
) -> None:
    """Point DB rows at the copied files/folders. Commits only when ``apply``."""
    file_map = mapping.files
    folder_map = mapping.folders
    new_file_ids = {info["id"] for info in file_map.values()}
    new_folder_ids = set(folder_map.values())

    for media in db.scalars(select(QuestionMedia)).all():
        old_id = media.drive_file_id
        if old_id in new_file_ids:
            continue
        info = file_map.get(old_id)
        if info is None:
            report.orphans["media"].append(f"{media.id}:{old_id}")
            continue
        media.drive_file_id = info["id"]
        media.drive_preview_url = GoogleDriveService.file_preview_url(info["id"])
        media.drive_web_view_url = info.get("web_view_url") or GoogleDriveService.file_view_url(info["id"])
        report.media_rows_updated += 1

    for artifact in db.scalars(select(JobArtifact).where(JobArtifact.storage_backend == "drive")).all():
        old_id = artifact.external_file_id or ""
        if not old_id or old_id in new_file_ids:
            continue
        info = file_map.get(old_id)
        if info is None:
            report.orphans["artifacts"].append(f"{artifact.id}:{old_id}")
            continue
        artifact.external_file_id = info["id"]
        artifact.external_url = GoogleDriveService.file_download_url(info["id"])
        report.artifact_rows_updated += 1

    for job in db.scalars(select(Job).where(Job.drive_folder_id.is_not(None))).all():
        old_id = job.drive_folder_id or ""
        if not old_id or old_id in new_folder_ids:
            continue
        new_id = folder_map.get(old_id)
        if new_id is None:
            report.orphans["jobs"].append(f"{job.id}:{old_id}")
            continue
        job.drive_folder_id = new_id
        job.drive_folder_url = GoogleDriveService.folder_url(new_id)
        report.job_rows_updated += 1

    if apply:
        db.commit()
        log(
            "DB updated: media=%s artifacts=%s jobs=%s"
            % (report.media_rows_updated, report.artifact_rows_updated, report.job_rows_updated)
        )
    else:
        db.rollback()
        log(
            "[dry-run] DB would update: media=%s artifacts=%s jobs=%s"
            % (report.media_rows_updated, report.artifact_rows_updated, report.job_rows_updated)
        )


def run_migration(
    drive: DriveClient,
    db: Session,
    *,
    source_id: str,
    target_id: str,
    mapping_path: Path | None,
    apply: bool,
    log: Callable[[str], None] = logger.info,
) -> MigrationReport:
    if source_id == target_id:
        raise ValueError("Source and target folders must differ.")
    mapping = (
        MigrationMapping.load(mapping_path, source_id=source_id, target_id=target_id)
        if mapping_path
        else MigrationMapping(source_id=source_id, target_id=target_id)
    )
    report = MigrationReport()
    copy_tree(drive, mapping=mapping, mapping_path=mapping_path, apply=apply, report=report, log=log)
    if apply:
        rewrite_text_artifacts(drive, mapping=mapping, mapping_path=mapping_path, report=report, log=log)
    remap_database(db, mapping=mapping, apply=apply, report=report, log=log)
    return report


def verify_tree_counts(drive: DriveClient, folder_id: str) -> dict[str, int]:
    """Recursive count of folders/files under ``folder_id`` (read-only)."""
    counts = {"folders": 0, "files": 0}
    stack = [folder_id]
    while stack:
        current = stack.pop()
        for child in _with_retry(lambda: drive.list_children(current), what=f"list {current}"):
            if child.is_folder:
                counts["folders"] += 1
                stack.append(child.file_id)
            else:
                counts["files"] += 1
    return counts

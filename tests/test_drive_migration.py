from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Job, JobArtifact, Question, QuestionMedia
from app.db.session import SessionLocal
from app.services.drive_migration import MigrationMapping, run_migration, verify_tree_counts
from app.services.google_drive_service import DRIVE_FOLDER_MIME_TYPE, DriveChild, DriveFolder

OLD_ROOT = "old-root"
NEW_ROOT = "new-root"


class _FakeDrive:
    """In-memory Drive: nodes keyed by id with parent, name, mime and bytes."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.copies = 0
        self.updates: list[str] = []
        self._counter = 0
        self.add_folder(OLD_ROOT, "Proyectos Adendas", parent=None)
        self.add_folder(NEW_ROOT, "Adendas", parent=None)

    def add_folder(self, file_id: str, name: str, *, parent: str | None) -> str:
        self.nodes[file_id] = {"name": name, "mime": DRIVE_FOLDER_MIME_TYPE, "parent": parent, "data": b""}
        return file_id

    def add_file(self, file_id: str, name: str, *, parent: str, mime: str, data: bytes) -> str:
        self.nodes[file_id] = {"name": name, "mime": mime, "parent": parent, "data": data}
        return file_id

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _child(self, file_id: str) -> DriveChild:
        node = self.nodes[file_id]
        return DriveChild(
            file_id=file_id,
            name=node["name"],
            mime_type=node["mime"],
            web_view_url=f"https://drive.google.com/file/d/{file_id}/view?usp=drivesdk",
        )

    def list_children(self, folder_id: str) -> list[DriveChild]:
        return [self._child(fid) for fid, node in self.nodes.items() if node["parent"] == folder_id]

    def ensure_folder(self, *, name: str, parent_id: str) -> DriveFolder:
        for fid, node in self.nodes.items():
            if node["parent"] == parent_id and node["name"] == name and node["mime"] == DRIVE_FOLDER_MIME_TYPE:
                return DriveFolder(folder_id=fid, name=name, web_view_url="")
        fid = self.add_folder(self._new_id("nf"), name, parent=parent_id)
        return DriveFolder(folder_id=fid, name=name, web_view_url="")

    def copy_file(self, *, file_id: str, name: str, parent_id: str) -> DriveChild:
        src = self.nodes[file_id]
        self.copies += 1
        new_id = self.add_file(self._new_id("cp"), name, parent=parent_id, mime=src["mime"], data=src["data"])
        return self._child(new_id)

    def download_file_bytes(self, file_id: str) -> bytes:
        return self.nodes[file_id]["data"]

    def update_file_content(self, *, file_id: str, content: bytes, mime_type: str) -> None:
        self.nodes[file_id]["data"] = content
        self.updates.append(file_id)


def _seed_drive(drive: _FakeDrive) -> dict[str, str]:
    adenda = drive.add_folder("f-adenda", "adenda_7", parent=OLD_ROOT)
    run = drive.add_folder("f-run", "run_abc", parent=adenda)
    media = drive.add_folder("f-media", "media", parent=run)
    tables = drive.add_folder("f-tables", "tables", parent=media)
    images = drive.add_folder("f-images", "images", parent=media)
    table_png = drive.add_file("png-table", "table_001.png", parent=tables, mime="image/png", data=b"\x89PNGtable")
    image_png = drive.add_file("png-image", "image_001.png", parent=images, mime="image/png", data=b"\x89PNGimage")
    reviewed = {
        "observaciones": [
            {
                "tables": [{"table_file": "https://drive.google.com/file/d/png-table/view?usp=drivesdk"}],
                "images": [{"image_file": "https://drive.google.com/file/d/png-image/view?usp=drivesdk"}],
            }
        ]
    }
    reviewed_json = drive.add_file(
        "json-reviewed", "input_revisado.json", parent=run, mime="application/json",
        data=json.dumps(reviewed).encode("utf-8"),
    )
    report_md = drive.add_file("md-report", "verificacion_icsara.md", parent=run, mime="text/markdown", data=b"# ok\n")
    return {
        "run": run, "table_png": table_png, "image_png": image_png,
        "reviewed_json": reviewed_json, "report_md": report_md,
    }


def _seed_db(ids: dict[str, str]) -> dict[str, int | str]:
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        job = Job(
            id=uuid4(), adenda_id=7, status="done", stage="done", progress=100,
            original_filename="input.pdf", content_type="application/pdf", file_size_bytes=10,
            storage_path="/tmp/input.pdf", expires_at=now + timedelta(days=7),
            drive_folder_id=ids["run"], drive_folder_url=f"https://drive.google.com/drive/folders/{ids['run']}",
        )
        db.add(job)
        db.flush()
        artifact = JobArtifact(
            job_id=job.id, name="input_revisado.json", path=None, size_bytes=10, storage_backend="drive",
            mime_type="application/json", external_file_id=ids["reviewed_json"],
            external_url=f"https://drive.google.com/uc?id={ids['reviewed_json']}&export=download",
        )
        question = Question(job_id=job.id, adenda_id=7, numero=1, capitulo="", texto="Obs 1")
        db.add_all([artifact, question])
        db.flush()
        media_table = QuestionMedia(
            question_id=question.id, filename="table_001.png", tipo="tabla", parte=1, mime_type="image/png",
            drive_file_id=ids["table_png"],
            drive_preview_url=f"https://drive.google.com/file/d/{ids['table_png']}/preview",
            drive_web_view_url=f"https://drive.google.com/file/d/{ids['table_png']}/view",
        )
        media_orphan = QuestionMedia(
            question_id=question.id, filename="missing.png", tipo="figura", parte=1, mime_type="image/png",
            drive_file_id="gone-from-drive",
            drive_preview_url="https://drive.google.com/file/d/gone-from-drive/preview",
            drive_web_view_url="https://drive.google.com/file/d/gone-from-drive/view",
        )
        db.add_all([media_table, media_orphan])
        db.commit()
        return {"job_id": str(job.id), "artifact_id": artifact.id, "media_id": media_table.id, "orphan_id": media_orphan.id}
    finally:
        db.close()


def test_dry_run_copies_nothing_and_changes_no_rows(tmp_path: Path) -> None:
    drive = _FakeDrive()
    ids = _seed_drive(drive)
    seeded = _seed_db(ids)
    mapping_path = tmp_path / "mapping.json"

    db = SessionLocal()
    try:
        report = run_migration(drive, db, source_id=OLD_ROOT, target_id=NEW_ROOT, mapping_path=mapping_path, apply=False, log=lambda _m: None)
    finally:
        db.close()

    assert drive.copies == 0
    assert drive.list_children(NEW_ROOT) == []
    assert not mapping_path.exists()
    assert report.folders_seen == 5
    assert report.files_seen == 4
    assert report.media_rows_updated == 1
    assert report.artifact_rows_updated == 1
    assert report.job_rows_updated == 1
    assert report.orphans["media"] == [f"{seeded['orphan_id']}:gone-from-drive"]

    db = SessionLocal()
    try:
        media = db.get(QuestionMedia, seeded["media_id"])
        assert media.drive_file_id == ids["table_png"]
    finally:
        db.close()


def test_apply_copies_tree_rewrites_json_and_remaps_db(tmp_path: Path) -> None:
    drive = _FakeDrive()
    ids = _seed_drive(drive)
    seeded = _seed_db(ids)
    mapping_path = tmp_path / "mapping.json"

    db = SessionLocal()
    try:
        report = run_migration(drive, db, source_id=OLD_ROOT, target_id=NEW_ROOT, mapping_path=mapping_path, apply=True, log=lambda _m: None)
    finally:
        db.close()

    assert report.files_copied == 4
    assert report.folders_created == 5
    assert report.text_files_rewritten == 1  # markdown had no ids, only JSON changed
    assert verify_tree_counts(drive, NEW_ROOT) == verify_tree_counts(drive, OLD_ROOT) == {"folders": 5, "files": 4}

    mapping = MigrationMapping.load(mapping_path, source_id=OLD_ROOT, target_id=NEW_ROOT)
    new_table_id = mapping.files[ids["table_png"]]["id"]
    new_image_id = mapping.files[ids["image_png"]]["id"]
    new_json_id = mapping.files[ids["reviewed_json"]]["id"]
    new_run_id = mapping.folders[ids["run"]]

    # copied tree keeps names/structure under the new root
    new_adenda = drive.ensure_folder(name="adenda_7", parent_id=NEW_ROOT)
    assert drive.nodes[new_run_id]["parent"] == new_adenda.folder_id
    assert drive.nodes[new_table_id]["data"] == b"\x89PNGtable"

    # reviewed JSON copy now points at the new media ids; source JSON untouched
    rewritten = json.loads(drive.download_file_bytes(new_json_id))
    assert rewritten["observaciones"][0]["tables"][0]["table_file"].endswith(f"/d/{new_table_id}/view?usp=drivesdk")
    assert rewritten["observaciones"][0]["images"][0]["image_file"].endswith(f"/d/{new_image_id}/view?usp=drivesdk")
    assert b"png-table" in drive.download_file_bytes(ids["reviewed_json"])

    db = SessionLocal()
    try:
        media = db.get(QuestionMedia, seeded["media_id"])
        assert media.drive_file_id == new_table_id
        assert media.drive_preview_url == f"https://drive.google.com/file/d/{new_table_id}/preview"
        assert media.drive_web_view_url == f"https://drive.google.com/file/d/{new_table_id}/view?usp=drivesdk"
        orphan = db.get(QuestionMedia, seeded["orphan_id"])
        assert orphan.drive_file_id == "gone-from-drive"
        artifact = db.get(JobArtifact, seeded["artifact_id"])
        assert artifact.external_file_id == new_json_id
        assert artifact.external_url == f"https://drive.google.com/uc?id={new_json_id}&export=download"
        job = db.scalars(select(Job)).one()
        assert job.drive_folder_id == new_run_id
        assert job.drive_folder_url == f"https://drive.google.com/drive/folders/{new_run_id}"
    finally:
        db.close()


def test_apply_is_idempotent_and_resumes_from_mapping(tmp_path: Path) -> None:
    drive = _FakeDrive()
    ids = _seed_drive(drive)
    _seed_db(ids)
    mapping_path = tmp_path / "mapping.json"

    for _ in range(2):
        db = SessionLocal()
        try:
            run_migration(drive, db, source_id=OLD_ROOT, target_id=NEW_ROOT, mapping_path=mapping_path, apply=True, log=lambda _m: None)
        finally:
            db.close()

    assert drive.copies == 4
    assert verify_tree_counts(drive, NEW_ROOT) == {"folders": 5, "files": 4}
    assert len(drive.updates) == 1

    # A partially-lost mapping must reuse files already present in the target instead of duplicating them.
    mapping = MigrationMapping.load(mapping_path, source_id=OLD_ROOT, target_id=NEW_ROOT)
    mapping.files.pop(ids["table_png"])
    mapping.save(mapping_path)
    db = SessionLocal()
    try:
        report = run_migration(drive, db, source_id=OLD_ROOT, target_id=NEW_ROOT, mapping_path=mapping_path, apply=True, log=lambda _m: None)
    finally:
        db.close()
    assert drive.copies == 4
    assert report.files_reused == 4
    assert verify_tree_counts(drive, NEW_ROOT) == {"folders": 5, "files": 4}

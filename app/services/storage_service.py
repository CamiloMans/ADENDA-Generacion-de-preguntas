from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile, status

ALLOWED_PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
    "applications/vnd.pdf",
    "text/pdf",
    "text/x-pdf",
}

ALLOWED_ARTIFACT_NAMES = {
    "preguntas.json",
    "preguntas.txt",
    "chapters_hinges.json",
    "preguntas_clasificadas.json",
    "preguntas_clasificadas_detalle.json",
    "outputs_png.zip",
    "texto_total.txt",
}


def job_dir(base_dir: Path, job_id: UUID) -> Path:
    return base_dir / str(job_id)


def input_pdf_path(base_dir: Path, job_id: UUID) -> Path:
    return job_dir(base_dir, job_id) / "input.pdf"


def output_dir(base_dir: Path, job_id: UUID) -> Path:
    return job_dir(base_dir, job_id) / "outputs"


def ensure_job_dirs(base_dir: Path, job_id: UUID) -> tuple[Path, Path]:
    jdir = job_dir(base_dir, job_id)
    odir = output_dir(base_dir, job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    odir.mkdir(parents=True, exist_ok=True)
    return jdir, odir


def _check_pdf_upload(file: UploadFile, max_pdf_bytes: int) -> int:
    ctype = (file.content_type or "").lower().strip()
    if ctype not in ALLOWED_PDF_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF uploads are allowed.",
        )

    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > max_pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"PDF exceeds maximum size of {max_pdf_bytes} bytes.",
        )
    return size


def save_upload_as_pdf(file: UploadFile, *, base_dir: Path, job_id: UUID, max_pdf_bytes: int) -> tuple[Path, int]:
    size = _check_pdf_upload(file, max_pdf_bytes=max_pdf_bytes)
    jdir, _ = ensure_job_dirs(base_dir, job_id)
    destination = jdir / "input.pdf"

    with destination.open("wb") as out_f:
        shutil.copyfileobj(file.file, out_f)
    file.file.seek(0)
    return destination, size


def make_outputs_zip(outputs_dir: Path) -> Path | None:
    png_dir = outputs_dir / "outputs_png"
    if not png_dir.exists():
        return None
    has_png = any(p.suffix.lower() == ".png" for p in png_dir.glob("*.png"))
    if not has_png:
        return None

    zip_path = outputs_dir / "outputs_png.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=png_dir)
    return zip_path


def remove_job_dir(base_dir: Path, job_id: UUID) -> None:
    jdir = job_dir(base_dir, job_id)
    if jdir.exists():
        shutil.rmtree(jdir, ignore_errors=True)


def validate_artifact_name(filename: str) -> None:
    if filename not in ALLOWED_ARTIFACT_NAMES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found.",
        )


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

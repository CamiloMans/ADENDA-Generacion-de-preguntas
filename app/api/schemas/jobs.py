from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class JobCreateResponse(BaseModel):
    job_id: UUID
    adenda_id: int
    status: str
    created_at: datetime


class JobStatusResponse(BaseModel):
    job_id: UUID
    adenda_id: int
    status: str
    progress: int
    stage: str
    drive_folder_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime


class JobResultSummary(BaseModel):
    pages: int | None = None
    observaciones: int | None = None
    tablas: int | None = None
    imagenes: int | None = None
    clasificadas: int | None = None
    sin_clasificar: int | None = None
    ids_faltantes_detectados: int | None = None
    ids_extraidas_desde_pdf: int | None = None
    ids_no_localizadas: int | None = None
    correcciones_revision: int | None = None


class JobResultResponse(BaseModel):
    job_id: UUID
    adenda_id: int
    status: str
    drive_folder_url: str | None = None
    artifacts: dict[str, str]
    summary: JobResultSummary | dict[str, Any] | None = None

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class JobCreateResponse(BaseModel):
    job_id: UUID
    status: str
    created_at: datetime


class JobStatusResponse(BaseModel):
    job_id: UUID
    status: str
    progress: int
    stage: str
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime


class JobResultSummary(BaseModel):
    pages: int | None = None
    preguntas: int | None = None
    tablas: int | None = None
    figuras: int | None = None
    capitulos: int | None = None
    bisagras: int | None = None
    classified: int | None = None
    unclassified: int | None = None


class JobResultResponse(BaseModel):
    job_id: UUID
    status: str
    artifacts: dict[str, str]
    summary: JobResultSummary | dict[str, Any] | None = None

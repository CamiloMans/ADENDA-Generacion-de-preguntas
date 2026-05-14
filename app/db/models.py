from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base


def _json_type() -> Any:
    return JSON().with_variant(JSONB(), "postgresql")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    adenda_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    progress: Mapped[int] = mapped_column(nullable=False, default=0)
    original_filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(_json_type(), nullable=True)
    drive_folder_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drive_folder_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    artifacts: Mapped[list["JobArtifact"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    questions: Mapped[list["Question"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_jobs_created_at_desc", "created_at"),
    )


class JobArtifact(Base):
    __tablename__ = "job_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    external_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    job: Mapped[Job] = relationship(back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("job_id", "name", name="uq_job_artifacts_job_id_name"),
    )


class Question(Base):
    __tablename__ = "preguntas"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    adenda_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    numero: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    orden: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capitulo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    bisagra: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_1: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_2: Mapped[str | None] = mapped_column(Text, nullable=True)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_types: Mapped[list[str] | None] = mapped_column(_json_type(), nullable=True)
    tema_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    tema_principal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    clasificacion_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type(), nullable=True)
    temas_principales: Mapped[list[str] | None] = mapped_column(_json_type(), nullable=True)
    temas_principales_id: Mapped[list[str] | None] = mapped_column(_json_type(), nullable=True)
    temas_principales_json: Mapped[list[dict[str, Any]] | None] = mapped_column(_json_type(), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    temas_secundarios: Mapped[list[dict[str, Any]] | None] = mapped_column(_json_type(), nullable=True)
    keywords_match: Mapped[list[str] | None] = mapped_column(_json_type(), nullable=True)
    raw_question_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    job: Mapped[Job] = relationship(back_populates="questions")
    media: Mapped[list["QuestionMedia"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("job_id", "numero", name="uq_preguntas_job_id_numero"),
        UniqueConstraint("job_id", "observation_id", name="uq_preguntas_job_id_observation_id"),
        Index("ix_preguntas_adenda_id_numero", "adenda_id", "numero"),
        Index("ix_preguntas_adenda_id_observation_id", "adenda_id", "observation_id"),
        Index("ix_preguntas_adenda_id_orden", "adenda_id", "orden"),
    )


class QuestionMedia(Base):
    __tablename__ = "pregunta_media"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("preguntas.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[str] = mapped_column(String(32), nullable=False)
    parte: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_rows: Mapped[list[Any] | None] = mapped_column(_json_type(), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drive_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    drive_preview_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    drive_web_view_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    question: Mapped[Question] = relationship(back_populates="media")

    __table_args__ = (
        UniqueConstraint("question_id", "filename", name="uq_pregunta_media_question_id_filename"),
    )

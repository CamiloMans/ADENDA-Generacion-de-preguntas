"""align preguntas schema with reviewed ICSARA payload

Revision ID: 0003_reviewed_questions_schema
Revises: 0002_adenda_drive_questions
Create Date: 2026-04-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_reviewed_questions_schema"
down_revision: Union[str, None] = "0002_adenda_drive_questions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        constraint["name"] == constraint_name
        for constraint in inspector.get_unique_constraints(table_name)
    )


def upgrade() -> None:
    with op.batch_alter_table("preguntas") as batch_op:
        if not _has_column("preguntas", "observation_id"):
            batch_op.add_column(sa.Column("observation_id", sa.Text(), nullable=True))
        if not _has_column("preguntas", "orden"):
            batch_op.add_column(sa.Column("orden", sa.Integer(), nullable=True))
        if not _has_column("preguntas", "section_1"):
            batch_op.add_column(sa.Column("section_1", sa.Text(), nullable=True))
        if not _has_column("preguntas", "section_2"):
            batch_op.add_column(sa.Column("section_2", sa.Text(), nullable=True))
        if not _has_column("preguntas", "requirement_types"):
            batch_op.add_column(sa.Column("requirement_types", sa.JSON(), nullable=True))
        if not _has_column("preguntas", "clasificacion_json"):
            batch_op.add_column(sa.Column("clasificacion_json", sa.JSON(), nullable=True))
        if not _has_column("preguntas", "temas_principales_json"):
            batch_op.add_column(sa.Column("temas_principales_json", sa.JSON(), nullable=True))
        if not _has_column("preguntas", "raw_question_json"):
            batch_op.add_column(sa.Column("raw_question_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("pregunta_media") as batch_op:
        if not _has_column("pregunta_media", "caption"):
            batch_op.add_column(sa.Column("caption", sa.Text(), nullable=True))
        if not _has_column("pregunta_media", "table_rows"):
            batch_op.add_column(sa.Column("table_rows", sa.JSON(), nullable=True))

    with op.batch_alter_table("preguntas") as batch_op:
        if not _has_unique_constraint("preguntas", "uq_preguntas_job_id_observation_id"):
            batch_op.create_unique_constraint(
                "uq_preguntas_job_id_observation_id",
                ["job_id", "observation_id"],
            )

    if not _has_index("preguntas", "ix_preguntas_adenda_id_observation_id"):
        op.create_index(
            "ix_preguntas_adenda_id_observation_id",
            "preguntas",
            ["adenda_id", "observation_id"],
            unique=False,
        )

    if not _has_index("preguntas", "ix_preguntas_adenda_id_orden"):
        op.create_index(
            "ix_preguntas_adenda_id_orden",
            "preguntas",
            ["adenda_id", "orden"],
            unique=False,
        )


def downgrade() -> None:
    if _has_index("preguntas", "ix_preguntas_adenda_id_orden"):
        op.drop_index("ix_preguntas_adenda_id_orden", table_name="preguntas")

    if _has_index("preguntas", "ix_preguntas_adenda_id_observation_id"):
        op.drop_index("ix_preguntas_adenda_id_observation_id", table_name="preguntas")

    with op.batch_alter_table("preguntas") as batch_op:
        if _has_unique_constraint("preguntas", "uq_preguntas_job_id_observation_id"):
            batch_op.drop_constraint("uq_preguntas_job_id_observation_id", type_="unique")

    with op.batch_alter_table("pregunta_media") as batch_op:
        if _has_column("pregunta_media", "table_rows"):
            batch_op.drop_column("table_rows")
        if _has_column("pregunta_media", "caption"):
            batch_op.drop_column("caption")

    with op.batch_alter_table("preguntas") as batch_op:
        if _has_column("preguntas", "raw_question_json"):
            batch_op.drop_column("raw_question_json")
        if _has_column("preguntas", "temas_principales_json"):
            batch_op.drop_column("temas_principales_json")
        if _has_column("preguntas", "clasificacion_json"):
            batch_op.drop_column("clasificacion_json")
        if _has_column("preguntas", "requirement_types"):
            batch_op.drop_column("requirement_types")
        if _has_column("preguntas", "section_2"):
            batch_op.drop_column("section_2")
        if _has_column("preguntas", "section_1"):
            batch_op.drop_column("section_1")
        if _has_column("preguntas", "orden"):
            batch_op.drop_column("orden")
        if _has_column("preguntas", "observation_id"):
            batch_op.drop_column("observation_id")

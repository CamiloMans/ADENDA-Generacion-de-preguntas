"""add adenda, drive metadata, and question tables

Revision ID: 0002_adenda_drive_questions
Revises: 0001_initial
Create Date: 2026-03-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_adenda_drive_questions"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("adenda_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("drive_folder_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("drive_folder_url", sa.String(length=2048), nullable=True))

    op.execute("UPDATE jobs SET adenda_id = 0 WHERE adenda_id IS NULL")

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column("adenda_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_index("ix_jobs_adenda_id", ["adenda_id"], unique=False)

    with op.batch_alter_table("job_artifacts") as batch_op:
        batch_op.add_column(sa.Column("storage_backend", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("mime_type", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("external_url", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("external_file_id", sa.String(length=255), nullable=True))
        batch_op.alter_column("path", existing_type=sa.String(length=2048), nullable=True)

    op.execute("UPDATE job_artifacts SET storage_backend = 'local' WHERE storage_backend IS NULL")

    with op.batch_alter_table("job_artifacts") as batch_op:
        batch_op.alter_column("storage_backend", existing_type=sa.String(length=32), nullable=False)

    op.create_table(
        "preguntas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("adenda_id", sa.Integer(), nullable=False),
        sa.Column("numero", sa.Integer(), nullable=False),
        sa.Column("capitulo", sa.Text(), nullable=False),
        sa.Column("bisagra", sa.Text(), nullable=True),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("tema_principal", sa.Text(), nullable=True),
        sa.Column("tema_principal_id", sa.String(length=255), nullable=True),
        sa.Column("temas_principales", sa.JSON(), nullable=True),
        sa.Column("temas_principales_id", sa.JSON(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("temas_secundarios", sa.JSON(), nullable=True),
        sa.Column("keywords_match", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "numero", name="uq_preguntas_job_id_numero"),
    )
    op.create_index("ix_preguntas_job_id", "preguntas", ["job_id"], unique=False)
    op.create_index("ix_preguntas_adenda_id", "preguntas", ["adenda_id"], unique=False)
    op.create_index("ix_preguntas_adenda_id_numero", "preguntas", ["adenda_id", "numero"], unique=False)

    op.create_table(
        "pregunta_media",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("tipo", sa.String(length=32), nullable=False),
        sa.Column("parte", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("drive_file_id", sa.String(length=255), nullable=False),
        sa.Column("drive_preview_url", sa.String(length=2048), nullable=False),
        sa.Column("drive_web_view_url", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["preguntas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_id", "filename", name="uq_pregunta_media_question_id_filename"),
    )
    op.create_index("ix_pregunta_media_question_id", "pregunta_media", ["question_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_pregunta_media_question_id", table_name="pregunta_media")
    op.drop_table("pregunta_media")
    op.drop_index("ix_preguntas_adenda_id_numero", table_name="preguntas")
    op.drop_index("ix_preguntas_adenda_id", table_name="preguntas")
    op.drop_index("ix_preguntas_job_id", table_name="preguntas")
    op.drop_table("preguntas")

    with op.batch_alter_table("job_artifacts") as batch_op:
        batch_op.alter_column("path", existing_type=sa.String(length=2048), nullable=False)
        batch_op.drop_column("external_file_id")
        batch_op.drop_column("external_url")
        batch_op.drop_column("mime_type")
        batch_op.drop_column("storage_backend")

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index("ix_jobs_adenda_id")
        batch_op.drop_column("drive_folder_url")
        batch_op.drop_column("drive_folder_id")
        batch_op.drop_column("adenda_id")

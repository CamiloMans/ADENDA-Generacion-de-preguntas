"""Use BIGINT for packed question numbers.

Revision ID: 0004_bigint_preguntas_numero
Revises: 0003_reviewed_questions_schema
Create Date: 2026-05-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004_bigint_preguntas_numero"
down_revision: Union[str, None] = "0003_reviewed_questions_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "preguntas",
        "numero",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "preguntas",
        "numero",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )

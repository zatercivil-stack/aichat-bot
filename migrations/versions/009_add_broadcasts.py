"""add broadcasts table

Revision ID: 009
Revises: 008
Create Date: 2026-05-05 17:40:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    if "broadcasts" not in tables:
        op.create_table(
            "broadcasts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("promo_code", sa.String(length=64), nullable=False),
            sa.Column("total_recipients", sa.Integer(), nullable=False),
            sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
            sa.Column("log_file", sa.String(length=256), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_broadcasts_promo_code", "broadcasts", ["promo_code"], unique=False)
        op.create_index("ix_broadcasts_source", "broadcasts", ["source"], unique=False)
        op.create_index("ix_broadcasts_status", "broadcasts", ["status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    if "broadcasts" in tables:
        op.drop_index("ix_broadcasts_status", table_name="broadcasts")
        op.drop_index("ix_broadcasts_source", table_name="broadcasts")
        op.drop_index("ix_broadcasts_promo_code", table_name="broadcasts")
        op.drop_table("broadcasts")

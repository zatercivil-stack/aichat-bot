"""custom_presets — пресеты, добавляемые через дашборд (без правки кода)

Revision ID: 021
Revises: 020
Create Date: 2026-05-22 15:30:00.000000

В рантайме мёржатся с хардкод-пресетами (EDIT_PRESETS). Превью лежат в
preset_previews по тому же key (position 0=жен, 1=муж).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("male_prompt", sa.Text(), nullable=True),
        sa.Column("quality_mode", sa.String(length=16), nullable=False, server_default="quality"),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("female_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_custom_preset_key", "custom_presets", ["key"])
    op.create_index("ix_custom_presets_key", "custom_presets", ["key"])


def downgrade() -> None:
    op.drop_index("ix_custom_presets_key", table_name="custom_presets")
    op.drop_constraint("uq_custom_preset_key", "custom_presets", type_="unique")
    op.drop_table("custom_presets")

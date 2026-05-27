"""bot_config.value: VARCHAR(512) -> TEXT (для длинных welcome/help)

Revision ID: 010
Revises: 009
Create Date: 2026-05-10 07:20:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        'bot_config', 'value',
        existing_type=sa.String(512),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'bot_config', 'value',
        existing_type=sa.Text(),
        type_=sa.String(512),
        existing_nullable=False,
    )

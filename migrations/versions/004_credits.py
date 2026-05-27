"""Add credits system

Revision ID: 004
Revises: 003
Create Date: 2026-05-01

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '004'
down_revision: str | None = '003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('credits', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('daily_credits_granted_at', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'daily_credits_granted_at')
    op.drop_column('users', 'credits')

"""add max plan, streak, trial counters

Revision ID: 007
Revises: 006
Create Date: 2026-05-05

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '007'
down_revision: str | None = '006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ADD VALUE must run outside a transaction in some cases.
    # Use COMMIT/begin around it for safety.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE plan ADD VALUE IF NOT EXISTS 'max'")

    op.add_column(
        'users',
        sa.Column('streak_days', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column(
        'users',
        sa.Column('streak_last_date', sa.Date(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('trial_pro_messages_left', sa.Integer(), server_default='3', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'trial_pro_messages_left')
    op.drop_column('users', 'streak_last_date')
    op.drop_column('users', 'streak_days')
    # enum value 'max' cannot be removed in postgres — leave as-is

"""trial end tracking — for delayed conversion push

Revision ID: 012
Revises: 011
Create Date: 2026-05-11 13:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '012'
down_revision = '011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('trial_ended_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('trial_end_push_sent', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'trial_end_push_sent')
    op.drop_column('users', 'trial_ended_at')

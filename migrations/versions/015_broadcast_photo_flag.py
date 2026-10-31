"""one-shot bonus flag for photo-features broadcast (May 2026)

Revision ID: 015
Revises: 014
Create Date: 2026-05-12 09:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '015'
down_revision = '014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('broadcast_photo_2026_05', sa.Boolean(),
                  server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'broadcast_photo_2026_05')

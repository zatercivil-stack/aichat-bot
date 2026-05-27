"""add bot_events and bot_config tables

Revision ID: 008
Revises: 007
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'bot_events',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('event_type', sa.String(32), nullable=False, index=True),
        sa.Column('model_key', sa.String(16), nullable=True),
        sa.Column('credits_spent', sa.Integer, default=0, nullable=False, server_default='0'),
        sa.Column('extra', sa.JSON, nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )

    op.create_table(
        'bot_config',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('value', sa.String(512), nullable=False),
        sa.Column('description', sa.String(256), nullable=False, server_default=''),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('bot_events')
    op.drop_table('bot_config')

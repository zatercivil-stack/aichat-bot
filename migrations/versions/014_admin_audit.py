"""admin audit log

Revision ID: 014
Revises: 013
Create Date: 2026-05-12 09:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '014'
down_revision = '013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'admin_actions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target_user_id', sa.Integer(), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['target_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_admin_actions_admin_id', 'admin_actions', ['admin_id'])
    op.create_index('ix_admin_actions_action', 'admin_actions', ['action'])
    op.create_index('ix_admin_actions_target_user_id', 'admin_actions', ['target_user_id'])
    op.create_index('ix_admin_actions_created_at', 'admin_actions', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_admin_actions_created_at', table_name='admin_actions')
    op.drop_index('ix_admin_actions_target_user_id', table_name='admin_actions')
    op.drop_index('ix_admin_actions_action', table_name='admin_actions')
    op.drop_index('ix_admin_actions_admin_id', table_name='admin_actions')
    op.drop_table('admin_actions')

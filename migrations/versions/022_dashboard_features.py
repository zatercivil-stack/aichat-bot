"""featured presets + scheduled broadcasts + click tracking

Revision ID: 022
Revises: 021
Create Date: 2026-05-22 16:00:00.000000

Три добавления для дашборда:
1) custom_presets.featured — закрепить пресет вверху каталога ("хит недели").
2) broadcasts.click_count — отслеживать клики по CTA-кнопке в рассылке
   (через уникальный callback bc_open:<broadcast_id> → инкремент).
3) scheduled_broadcasts — очередь отложенных пресет-рассылок (cron-job
   каждую минуту берёт due и запускает).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '022'
down_revision = '021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('custom_presets',
        sa.Column('featured', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('broadcasts',
        sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'))
    op.create_table(
        'scheduled_broadcasts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('preset_id', sa.Integer(),
                  sa.ForeignKey('custom_presets.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('audience', sa.String(length=16), nullable=False, server_default='all'),
        sa.Column('caption', sa.Text(), nullable=False, server_default=''),
        sa.Column('send_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending', index=True),
        sa.Column('broadcast_id', sa.Integer(),
                  sa.ForeignKey('broadcasts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('scheduled_broadcasts')
    op.drop_column('broadcasts', 'click_count')
    op.drop_column('custom_presets', 'featured')

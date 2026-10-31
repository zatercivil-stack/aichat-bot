"""preset previews — кеш TG file_id для готовых примеров каждого пресета

Revision ID: 018
Revises: 017
Create Date: 2026-05-15 22:00:00.000000

Используется для inline-превью перед списанием кредитов и для каталога /gallery.
Каждая запись = один demo-кадр для конкретного preset_key. Хранится TG file_id,
получаемый после первой отправки фото в админский чат (он же кеш).

Если хотим несколько вариантов на пресет — добавляем строки с разным position.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '018'
down_revision = '017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'preset_previews',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),
        sa.Column('preset_key', sa.String(length=64), nullable=False),
        sa.Column('file_id', sa.String(length=256), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('source_file', sa.String(length=256), nullable=True),  # имя исходного файла на диске (для отладки)
        sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('preset_key', 'position', name='uq_preset_preview_pos'),
    )
    op.create_index('ix_preset_previews_preset_key', 'preset_previews', ['preset_key'])


def downgrade() -> None:
    op.drop_index('ix_preset_previews_preset_key', table_name='preset_previews')
    op.drop_table('preset_previews')

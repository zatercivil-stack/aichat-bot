"""preset_launch — единый реестр даты запуска пресетов (код + DB)

Revision ID: 024
Revises: 023
Create Date: 2026-06-02 11:00:00.000000

Развязывает «новизну»/graduation раздела «🆕 Новинки» от
preset_previews.generated_at (сбивается при массовой регенерации превью).
Свежесть теперь считается по launched_at.

Бэкофилл (порядок важен — ON CONFLICT DO NOTHING оставляет первую вставку):
  1) DB-пресеты → custom_presets.created_at (точная дата создания в дашборде,
     НЕ зависит от регенерации превью);
  2) остальные (хардкод) → MIN(generated_at) превью как лучший доступный прокси
     (фолбэк на 2026-05-01 для легаси-строк без даты).
Дальше новые пресеты регистрируются автоматически в reload_custom_presets.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '024'
down_revision = '023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'preset_launch',
        sa.Column('preset_key', sa.String(length=64), primary_key=True),
        sa.Column('launched_at', sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    # 1) Точные даты DB-пресетов.
    op.execute("""
        INSERT INTO preset_launch (preset_key, launched_at)
        SELECT key, COALESCE(created_at, now()) FROM custom_presets
        ON CONFLICT (preset_key) DO NOTHING
    """)
    # 2) Хардкод-пресеты — прокси по дате первого превью.
    op.execute("""
        INSERT INTO preset_launch (preset_key, launched_at)
        SELECT preset_key,
               COALESCE(MIN(generated_at), TIMESTAMPTZ '2026-05-01 00:00:00+00')
        FROM preset_previews
        GROUP BY preset_key
        ON CONFLICT (preset_key) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table('preset_launch')

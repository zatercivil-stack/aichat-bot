"""user gender — для гендерно-осознанного каталога и мужского режима промптов

Revision ID: 020
Revises: 019
Create Date: 2026-05-21 18:30:00.000000

Юзер указывает пол один раз (при первом входе в каталог). По нему:
  - показываем подходящие пресеты (мужчинам прячем жёстко-женские),
  - подставляем мужское/женское превью,
  - добавляем «masculine styling» суффикс в промпт (без глянца губ, серёг, платьев).

Значения: 'male' / 'female' / 'any' (не важно). NULL = ещё не спросили.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("gender", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "gender")

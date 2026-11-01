"""burn credits — time-limited pack credits that expire (boost revenue via burn-rate)

Revision ID: 019
Revises: 018
Create Date: 2026-05-16 09:00:00.000000

Юзеры теперь могут покупать «горящие» паки — больше кредитов за ту же цену, но
со сроком. Логика расхода: сначала тратятся burn-кредиты (если есть и не сгорели),
потом обычные. После expires_at burn_credits обнуляются.

Это копия conkurент-механики: 99₽ → 1500 кр на 7 дней (×3 от вечного 99₽/500 кр).
Если юзер не использует — деньги остаются нам (burn-rate revenue).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("burn_credits", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("users", sa.Column("burn_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("burn_reminded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_burn_expires_at", "users", ["burn_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_users_burn_expires_at", table_name="users")
    op.drop_column("users", "burn_reminded_at")
    op.drop_column("users", "burn_expires_at")
    op.drop_column("users", "burn_credits")

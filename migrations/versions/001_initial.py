"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("full_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("language_code", sa.String(8), nullable=False, server_default="ru"),
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("referrer_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_active_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "plan",
            sa.Enum("free", "basic", "pro", name="plan"),
            nullable=False,
            server_default="free",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="RUB"),
        sa.Column("payment_method", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("plan", sa.Enum("free", "basic", "pro", name="plan"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "succeeded", "failed", "canceled", name="paymentstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("extra", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_payments_external_id", "payments", ["external_id"])

    op.create_table(
        "daily_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("model_key", sa.String(16), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "usage_date", "model_key", name="uq_daily_usage"),
    )
    op.create_index("ix_daily_usage_user_id", "daily_usage", ["user_id"])


def downgrade() -> None:
    op.drop_table("daily_usage")
    op.drop_table("payments")
    op.drop_table("subscriptions")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS plan")
    op.execute("DROP TYPE IF EXISTS paymentstatus")

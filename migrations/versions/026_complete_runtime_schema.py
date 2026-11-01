"""Add schema used by promo automation and channel publishing.

Revision ID: 026
Revises: 025

Existing installations may already have these objects from manual provisioning.
Only add missing objects; do not rewrite existing data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "promo_activated_at" not in columns:
        op.add_column(
            "users", sa.Column("promo_activated_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "time_bonus_granted" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "time_bonus_granted", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    if not inspector.has_table("channel_posts"):
        op.create_table(
            "channel_posts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("topic", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("cta_type", sa.String(16), nullable=False, server_default="none"),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_channel_posts_status", "channel_posts", ["status"])


def downgrade() -> None:
    # These fields/table may predate Alembic on existing installations. A downgrade
    # must not delete potentially pre-existing user data or scheduled posts.
    pass

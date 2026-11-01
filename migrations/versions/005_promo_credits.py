"""Migrate promo codes from plan/days to credits system

Revision ID: 005
Revises: 004
Create Date: 2026-05-01
"""

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promo_codes", sa.Column("credits", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("promo_codes", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("promo_codes", "plan")
    op.drop_column("promo_codes", "days")


def downgrade() -> None:
    op.add_column(
        "promo_codes", sa.Column("days", sa.Integer(), nullable=False, server_default="30")
    )
    op.add_column(
        "promo_codes", sa.Column("plan", sa.String(16), nullable=False, server_default="free")
    )
    op.drop_column("promo_codes", "expires_at")
    op.drop_column("promo_codes", "credits")

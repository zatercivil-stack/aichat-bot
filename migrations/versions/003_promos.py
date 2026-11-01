"""Add promo codes

Revision ID: 003
Revises: 002
Create Date: 2026-05-01

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE promo_codes (
            id SERIAL PRIMARY KEY,
            code VARCHAR(64) UNIQUE NOT NULL,
            plan plan NOT NULL,
            days INTEGER NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 0,
            used_count INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE promo_redemptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            promo_code_id INTEGER NOT NULL REFERENCES promo_codes(id),
            redeemed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_promo_redemption UNIQUE (user_id, promo_code_id)
        )
    """)
    op.execute("CREATE INDEX ix_promo_redemptions_user_id ON promo_redemptions(user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE promo_redemptions")
    op.execute("DROP TABLE promo_codes")

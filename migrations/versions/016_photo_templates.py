"""photo templates — viral 'try with your face' broadcasts

Revision ID: 016
Revises: 015
Create Date: 2026-05-12 13:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "photo_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.String(length=256), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_used", sa.Text(), nullable=False, server_default=""),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("click_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("photo_templates")

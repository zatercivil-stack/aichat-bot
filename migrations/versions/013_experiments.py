"""A/B experiments + assignments

Revision ID: 013
Revises: 012
Create Date: 2026-05-11 13:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("variants", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "experiment_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(length=32), nullable=False),
        sa.Column(
            "assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conversion_event", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "experiment_id", name="uq_exp_assignment"),
    )
    op.create_index("ix_experiment_assignments_user_id", "experiment_assignments", ["user_id"])
    op.create_index(
        "ix_experiment_assignments_experiment_id", "experiment_assignments", ["experiment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_experiment_assignments_experiment_id", table_name="experiment_assignments")
    op.drop_index("ix_experiment_assignments_user_id", table_name="experiment_assignments")
    op.drop_table("experiment_assignments")
    op.drop_table("experiments")

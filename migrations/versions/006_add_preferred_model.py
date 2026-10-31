"""add preferred_model to users

Revision ID: 006
Revises: 005
Create Date: 2026-05-02 19:00:00

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('preferred_model', sa.String(16), nullable=True, server_default='free'))
    op.execute("UPDATE users SET preferred_model = 'free' WHERE preferred_model IS NULL")
    op.alter_column('users', 'preferred_model', nullable=False)


def downgrade() -> None:
    op.drop_column('users', 'preferred_model')

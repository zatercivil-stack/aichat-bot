"""channel_traffic — лог джойнов/левов в @conturD с invite-link attribution.

Дашборд /admin/channel-traffic группирует события по invite_link_url
(или 'organic' если пришёл по @username) и считает retention.

Чтобы это начало работать — бот должен слушать chat_member updates
(включается авто через resolve_used_update_types после регистрации
@router.chat_member, см. app/handlers/channel_traffic.py).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '023'
down_revision = '022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'channel_traffic',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('user_tg_id', sa.BigInteger(), nullable=False, index=True),
        sa.Column('event', sa.String(length=8), nullable=False),  # join | leave
        sa.Column('invite_link_url', sa.String(length=256), nullable=True),
        sa.Column('invite_link_name', sa.String(length=128), nullable=True),
        sa.Column('via_chat_folder', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('old_status', sa.String(length=24), nullable=True),
        sa.Column('new_status', sa.String(length=24), nullable=True),
        sa.Column('ts', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False, index=True),
    )
    op.create_index('ix_channel_traffic_link_ts', 'channel_traffic',
                    ['invite_link_url', 'ts'])


def downgrade() -> None:
    op.drop_index('ix_channel_traffic_link_ts', table_name='channel_traffic')
    op.drop_table('channel_traffic')

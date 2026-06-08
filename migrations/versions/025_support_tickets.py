"""support_tickets — система тикетов поддержки (тикет + тред сообщений)

Revision ID: 025
Revises: 024
Create Date: 2026-06-07 12:00:00.000000

Две таблицы:
  • support_tickets — одно обращение (один открытый на юзера, анти-спам);
  • support_messages — тред (user/admin), опционально TG-вложение.
Ответы шлёт только веб-панель; бот лишь принимает обращения и доставляет
ответ юзеру с inline-кнопками «дописать»/«вопрос решён».
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '025'
down_revision = '024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'support_tickets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('last_user_msg_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_support_tickets_user_id', 'support_tickets', ['user_id'])

    op.create_table(
        'support_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('support_tickets.id'), nullable=False),
        sa.Column('sender', sa.String(length=8), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('attachment_file_id', sa.String(length=256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_support_messages_ticket_id', 'support_messages', ['ticket_id'])


def downgrade() -> None:
    op.drop_index('ix_support_messages_ticket_id', table_name='support_messages')
    op.drop_table('support_messages')
    op.drop_index('ix_support_tickets_user_id', table_name='support_tickets')
    op.drop_table('support_tickets')

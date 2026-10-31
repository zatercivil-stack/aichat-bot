"""subscription auto-renew — saved YooKassa payment method + opt-in flag

Revision ID: 017
Revises: 016
Create Date: 2026-05-15 14:00:00.000000

Добавляет в subscriptions поля для рекуррентного списания через YooKassa:
  - auto_renew (BOOLEAN): юзер согласился на автопродление (по умолчанию TRUE при первом
    платеже, можно выключить кнопкой «Отключить автопродление» в /sub)
  - payment_method_id (VARCHAR(64)): id сохранённого способа оплаты от YooKassa
    (приходит в webhook после успешной оплаты с save_payment_method=true)
  - renewal_reminded_at (TIMESTAMP): дата отправки pre-charge уведомления —
    чтобы не спамить дважды
  - last_renewal_attempt_at (TIMESTAMP): последняя попытка автоплатежа (для retry-логики)
  - renewal_failures (INT): сколько подряд неудач (после 3 — отключаем auto_renew)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '017'
down_revision = '016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('subscriptions',
        sa.Column('auto_renew', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('subscriptions',
        sa.Column('payment_method_id', sa.String(length=64), nullable=True))
    op.add_column('subscriptions',
        sa.Column('renewal_reminded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions',
        sa.Column('last_renewal_attempt_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions',
        sa.Column('renewal_failures', sa.Integer(), nullable=False, server_default='0'))
    # Индекс для быстрого поиска подписок к продлению
    op.create_index(
        'ix_subscriptions_expires_at',
        'subscriptions', ['expires_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_subscriptions_expires_at', table_name='subscriptions')
    op.drop_column('subscriptions', 'renewal_failures')
    op.drop_column('subscriptions', 'last_renewal_attempt_at')
    op.drop_column('subscriptions', 'renewal_reminded_at')
    op.drop_column('subscriptions', 'payment_method_id')
    op.drop_column('subscriptions', 'auto_renew')

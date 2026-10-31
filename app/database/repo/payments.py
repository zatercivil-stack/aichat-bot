from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Payment, PaymentStatus, Plan


async def create_payment(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    currency: str,
    payment_method: str,
    plan: Plan,
    extra: dict | None = None,
) -> Payment:
    payment = Payment(
        user_id=user_id,
        amount=amount,
        currency=currency,
        payment_method=payment_method,
        plan=plan,
        status=PaymentStatus.PENDING,
        extra=extra or {},
    )
    session.add(payment)
    await session.flush()
    await session.refresh(payment)
    return payment


async def get_payment(session: AsyncSession, payment_id: int) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.id == payment_id))
    return result.scalar_one_or_none()


async def get_payment_by_external_id(session: AsyncSession, external_id: str) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.external_id == external_id))
    return result.scalar_one_or_none()


async def update_payment(
    session: AsyncSession,
    payment_id: int,
    status: PaymentStatus,
    external_id: str | None = None,
) -> Payment | None:
    payment = await get_payment(session, payment_id)
    if not payment:
        return None
    payment.status = status
    payment.updated_at = datetime.now(timezone.utc)
    if external_id:
        payment.external_id = external_id
    await session.flush()
    return payment


async def get_revenue_today(session: AsyncSession) -> Decimal:
    from datetime import date

    from sqlalchemy import func
    today = date.today()
    result = await session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.status == PaymentStatus.SUCCEEDED)
        .where(func.date(Payment.created_at) == today)
        .where(Payment.currency == 'RUB')
    )
    return result.scalar_one()


async def get_pending_payments(session: AsyncSession):
    """Получить все висящие платежи"""
    from sqlalchemy import select
    result = await session.execute(
        select(Payment).where(Payment.status == PaymentStatus.PENDING)
    )
    return result.scalars().all()


def complete_pending_payments(session: AsyncSession):
    """Завершить все висящие платежи (устарело, используйте get_pending_payments + update)"""
    pass


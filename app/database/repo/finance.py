from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BotEvent, Payment, PaymentStatus, User


async def get_payment_funnel(session: AsyncSession, days: int = 30) -> dict:
    """Payment funnel breakdown: created → pending → succeeded/failed/canceled.

    Bonus: recovery hit-rate (succeeded after recovery_sent_at).
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    total = await session.scalar(
        select(func.count()).select_from(Payment).where(Payment.created_at >= since)
    ) or 0
    succeeded = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.created_at >= since,
            Payment.status == PaymentStatus.SUCCEEDED,
        )
    ) or 0
    pending = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.created_at >= since,
            Payment.status == PaymentStatus.PENDING,
        )
    ) or 0
    failed = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.created_at >= since,
            Payment.status == PaymentStatus.FAILED,
        )
    ) or 0
    canceled = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.created_at >= since,
            Payment.status == PaymentStatus.CANCELED,
        )
    ) or 0

    # Recovery: payments where recovery_sent_at was set; of those — how many succeeded
    # extra is JSON (not JSONB) — use ->> operator directly
    recovery_filter = text("(extra->>'recovery_sent_at') IS NOT NULL")
    recovery_sent = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.created_at >= since,
            recovery_filter,
        )
    ) or 0
    recovery_recovered = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.created_at >= since,
            recovery_filter,
            Payment.status == PaymentStatus.SUCCEEDED,
        )
    ) or 0

    conv_rate = (succeeded / total * 100) if total else 0.0
    abandonment = (1 - succeeded / total) * 100 if total else 0.0
    recovery_rate = (recovery_recovered / recovery_sent * 100) if recovery_sent else 0.0

    return {
        'total': total,
        'succeeded': succeeded,
        'pending': pending,
        'failed': failed,
        'canceled': canceled,
        'conv_rate': round(conv_rate, 1),
        'abandonment_rate': round(abandonment, 1),
        'recovery_sent': recovery_sent,
        'recovery_recovered': recovery_recovered,
        'recovery_rate': round(recovery_rate, 1),
    }


async def get_revenue_summary(session: AsyncSession) -> dict:
    """Aggregated revenue: today / 7d / 30d / all-time + paying users count."""
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    base = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.status == PaymentStatus.SUCCEEDED,
        Payment.currency == 'RUB',
    )
    revenue_today = await session.scalar(base.where(Payment.created_at >= today)) or Decimal(0)
    revenue_week = await session.scalar(base.where(Payment.created_at >= week_ago)) or Decimal(0)
    revenue_month = await session.scalar(base.where(Payment.created_at >= month_ago)) or Decimal(0)
    revenue_total = await session.scalar(base) or Decimal(0)

    paying_users = await session.scalar(
        select(func.count(func.distinct(Payment.user_id))).where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.currency == 'RUB',
        )
    ) or 0

    paid_count = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.currency == 'RUB',
        )
    ) or 0

    arpu = (float(revenue_total) / paying_users) if paying_users else 0.0
    avg_check = (float(revenue_total) / paid_count) if paid_count else 0.0

    return {
        'revenue_today': float(revenue_today),
        'revenue_week': float(revenue_week),
        'revenue_month': float(revenue_month),
        'revenue_total': float(revenue_total),
        'paying_users': paying_users,
        'paid_count': paid_count,
        'arpu': round(arpu, 2),
        'avg_check': round(avg_check, 2),
    }


async def get_daily_revenue(session: AsyncSession, days: int = 30) -> list[dict]:
    """Daily revenue (RUB, succeeded only) for the last N days."""
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = today - timedelta(days=days - 1)

    rows = await session.execute(
        select(
            func.date_trunc('day', Payment.created_at).label('day'),
            func.coalesce(func.sum(Payment.amount), 0).label('amount'),
            func.count().label('cnt'),
        )
        .where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.currency == 'RUB',
            Payment.created_at >= since,
        )
        .group_by(text('1'))
        .order_by(text('1'))
    )
    return [
        {'day': str(r.day)[:10], 'amount': float(r.amount or 0), 'cnt': int(r.cnt)}
        for r in rows
    ]


async def get_revenue_by_method(session: AsyncSession, days: int = 30) -> list[dict]:
    """Revenue split by payment_method over last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await session.execute(
        select(
            Payment.payment_method,
            func.coalesce(func.sum(Payment.amount), 0).label('amount'),
            func.count().label('cnt'),
        )
        .where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.created_at >= since,
        )
        .group_by(Payment.payment_method)
        .order_by(func.sum(Payment.amount).desc())
    )
    return [
        {'method': r.payment_method, 'amount': float(r.amount or 0), 'cnt': int(r.cnt)}
        for r in rows
    ]


async def get_top_spenders(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Top users by total RUB spent (succeeded payments only)."""
    rows = await session.execute(
        select(
            User.id,
            User.telegram_id,
            User.username,
            User.full_name,
            User.credits,
            func.sum(Payment.amount).label('total'),
            func.count(Payment.id).label('cnt'),
            func.max(Payment.created_at).label('last_paid'),
        )
        .join(Payment, Payment.user_id == User.id)
        .where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.currency == 'RUB',
        )
        .group_by(User.id)
        .order_by(func.sum(Payment.amount).desc())
        .limit(limit)
    )
    return [
        {
            'user_id': r.id,
            'telegram_id': r.telegram_id,
            'username': r.username,
            'full_name': r.full_name,
            'credits': r.credits,
            'total': float(r.total or 0),
            'cnt': int(r.cnt),
            'last_paid': r.last_paid,
        }
        for r in rows
    ]


async def get_payment_status_breakdown(session: AsyncSession, days: int = 30) -> dict:
    """Counts of payments by status over last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await session.execute(
        select(Payment.status, func.count().label('cnt'))
        .where(Payment.created_at >= since)
        .group_by(Payment.status)
    )
    return {str(r.status.value if hasattr(r.status, 'value') else r.status): int(r.cnt) for r in rows}


async def get_conversion_metrics(session: AsyncSession) -> dict:
    """Funnel: total users → users who sent first message → users who paid."""
    total_users = await session.scalar(select(func.count()).select_from(User)) or 0

    msg_users = await session.scalar(
        select(func.count(func.distinct(BotEvent.user_id)))
        .where(BotEvent.event_type == 'message_sent')
    ) or 0

    paying_users = await session.scalar(
        select(func.count(func.distinct(Payment.user_id))).where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.currency == 'RUB',
        )
    ) or 0

    activation_rate = (msg_users / total_users * 100) if total_users else 0
    paid_rate = (paying_users / total_users * 100) if total_users else 0
    paid_from_active = (paying_users / msg_users * 100) if msg_users else 0

    return {
        'total_users': total_users,
        'activated_users': msg_users,
        'paying_users': paying_users,
        'activation_rate': round(activation_rate, 1),
        'paid_rate': round(paid_rate, 2),
        'paid_from_active': round(paid_from_active, 2),
    }

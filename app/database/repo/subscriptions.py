from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Plan, Subscription


async def get_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def get_user_plan(session: AsyncSession, user_id: int) -> Plan:
    sub = await get_subscription(session, user_id)
    if not sub:
        return Plan.FREE
    now = datetime.now(timezone.utc)
    if sub.plan == Plan.FREE:
        return Plan.FREE
    if sub.expires_at and sub.expires_at < now:
        sub.is_active = False
        await session.flush()
        return Plan.FREE
    if sub.is_active:
        return sub.plan
    return Plan.FREE


async def activate_subscription(
    session: AsyncSession,
    user_id: int,
    plan: Plan,
    expires_at: datetime,
) -> Subscription:
    sub = await get_subscription(session, user_id)
    if sub:
        sub.plan = plan
        sub.expires_at = expires_at
        sub.is_active = True
        sub.updated_at = datetime.now(timezone.utc)
    else:
        sub = Subscription(user_id=user_id, plan=plan, expires_at=expires_at, is_active=True)
        session.add(sub)
    await session.flush()
    await session.refresh(sub)
    return sub


async def extend_subscription(
    session: AsyncSession,
    user_id: int,
    plan_str: str,
    days: int = 30,
) -> Subscription:
    """Активирует/продлевает подписку. Если уже активна — добавляет days к expires_at."""
    sub = await get_subscription(session, user_id)
    now = datetime.utcnow()
    plan_enum = Plan(plan_str)

    if sub and sub.is_active and sub.expires_at:
        # Нормализация tz: получаем naive datetime для сравнения
        sub_expires = sub.expires_at
        if sub_expires.tzinfo is not None:
            sub_expires_cmp = sub_expires.replace(tzinfo=None)
        else:
            sub_expires_cmp = sub_expires

        if sub_expires_cmp > now:
            # Активна — продлеваем от текущего expires_at
            sub.expires_at = sub_expires + timedelta(days=days)
            sub.plan = plan_enum
        else:
            # Истекла
            sub.plan = plan_enum
            sub.expires_at = now + timedelta(days=days)
            sub.is_active = True
        sub.updated_at = now
    elif sub:
        sub.plan = plan_enum
        sub.expires_at = now + timedelta(days=days)
        sub.is_active = True
        sub.updated_at = now
    else:
        sub = Subscription(
            user_id=user_id,
            plan=plan_enum,
            expires_at=now + timedelta(days=days),
            is_active=True,
        )
        session.add(sub)
    await session.flush()
    return sub


async def count_by_plan(session: AsyncSession) -> dict[str, int]:
    from sqlalchemy import func
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Subscription.plan, func.count())
        .where(Subscription.is_active == True)
        .where((Subscription.expires_at == None) | (Subscription.expires_at > now))
        .group_by(Subscription.plan)
    )
    return {row[0].value: row[1] for row in result}

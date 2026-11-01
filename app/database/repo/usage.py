from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DailyUsage, Plan

# Дневные лимиты по model_key для каждого плана
PLAN_LIMITS: dict[Plan, dict[str, int]] = {
    Plan.FREE: {"free": 20, "basic": 0, "pro": 0, "image": 0},
    Plan.BASIC: {"free": 9999, "basic": 300, "pro": 0, "image": 5},
    Plan.PRO: {"free": 9999, "basic": 9999, "pro": 100, "image": 20},
}

# Лучшая модель, доступная плану
PLAN_BEST_MODEL: dict[Plan, str] = {
    Plan.FREE: "free",
    Plan.BASIC: "basic",
    Plan.PRO: "pro",
}


async def get_daily_count(
    session: AsyncSession, user_id: int, usage_date: date, model_key: str
) -> int:
    result = await session.execute(
        select(DailyUsage.message_count).where(
            DailyUsage.user_id == user_id,
            DailyUsage.usage_date == usage_date,
            DailyUsage.model_key == model_key,
        )
    )
    row = result.scalar_one_or_none()
    return row or 0


async def increment_daily_count(
    session: AsyncSession, user_id: int, usage_date: date, model_key: str
) -> int:
    stmt = (
        insert(DailyUsage)
        .values(user_id=user_id, usage_date=usage_date, model_key=model_key, message_count=1)
        .on_conflict_do_update(
            index_elements=["user_id", "usage_date", "model_key"],
            set_={"message_count": DailyUsage.message_count + 1},
        )
        .returning(DailyUsage.message_count)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


async def check_limit(
    session: AsyncSession, user_id: int, plan: Plan, model_key: str, bonus: int = 0
) -> tuple[bool, int, int]:
    """Returns (allowed, used, limit)."""
    limit = PLAN_LIMITS[plan].get(model_key, 0)
    if plan == Plan.FREE and model_key == "free" and bonus > 0:
        limit += bonus
    if limit == 0:
        return False, 0, 0
    if limit >= 9999:
        return True, 0, 9999

    from app.utils import today_msk

    today = today_msk()  # МСК для лимита «за сегодня» — иначе ночью съезжает
    used = await get_daily_count(session, user_id, today, model_key)
    return used < limit, used, limit


async def get_messages_today_total(session: AsyncSession) -> int:
    from sqlalchemy import func

    from app.utils import today_msk

    today = today_msk()
    result = await session.execute(
        select(func.coalesce(func.sum(DailyUsage.message_count), 0)).where(
            DailyUsage.usage_date == today
        )
    )
    return result.scalar_one()

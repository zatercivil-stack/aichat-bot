from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Payment, PaymentStatus, Subscription, User


# Сколько дней после последнего успешного платежа юзер «свежеплативший»
# (без watermark). Через PAID_PERIOD_DAYS возвращается watermark, если нет
# активной подписки. Раньше было «один платёж = навсегда» — слишком либерально,
# не стимулирует регулярный LTV.
PAID_PERIOD_DAYS = 30


async def is_paid_user(session: AsyncSession, user_db_id: int) -> bool:
    """True если юзер сейчас «платный» — без watermark.

    Условия (OR):
      1. Успешный платёж в последние PAID_PERIOD_DAYS (30 дней)
      2. Активная подписка (expires_at > now)
    Иначе → watermark."""
    cutoff = datetime.utcnow() - timedelta(days=PAID_PERIOD_DAYS)
    recent_paid = await session.scalar(
        select(func.count(Payment.id))
        .where(
            Payment.user_id == user_db_id,
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.created_at >= cutoff,
        )
    )
    if (recent_paid or 0) > 0:
        return True
    sub = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_db_id)
    )
    if sub and sub.is_active and sub.expires_at:
        expires = sub.expires_at
        if expires.tzinfo is not None:
            expires = expires.replace(tzinfo=None)
        if expires > datetime.utcnow():
            return True
    return False


async def get_all_telegram_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(User.telegram_id).where(User.is_banned == False)
    )
    return list(result.scalars().all())


async def add_referral_bonus(session: AsyncSession, telegram_id: int, amount: int = 150) -> None:
    """Начисляет реферальный бонус + инкрементит счётчик приглашённых. Идёт
    через add_credits (а не direct UPDATE), чтобы корректно отработать
    _clean_expired_burn и единый путь начисления."""
    from app.database.repo.credits import add_credits
    user = await session.scalar(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    )
    if not user:
        return
    await add_credits(session, user, amount)
    user.referral_bonus = (user.referral_bonus or 0) + 1
    await session.flush()


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def set_preferred_model(session: AsyncSession, telegram_id: int, model: str) -> bool:
    """Установить предпочтительную модель пользователя."""
    user = await get_user(session, telegram_id)
    if not user:
        return False
    user.preferred_model = model
    await session.flush()
    return True


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    full_name: str,
    language_code: str = 'ru',
    referrer_id: int | None = None,
) -> tuple[User, bool]:
    user = await get_user(session, telegram_id)
    if user:
        return user, False

    user = User(
        telegram_id=telegram_id,
        username=username,
        full_name=full_name,
        language_code=language_code,
        referrer_id=referrer_id,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user, True


async def update_last_active(session: AsyncSession, telegram_id: int) -> None:
    await session.execute(
        update(User)
        .where(User.telegram_id == telegram_id)
        .values(last_active_at=datetime.now(timezone.utc))
    )


async def get_total_users_count(session: AsyncSession) -> int:
    from sqlalchemy import func, select
    result = await session.execute(select(func.count()).select_from(User))
    return result.scalar_one()

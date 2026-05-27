from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PromoCode, PromoRedemption


async def get_promo(session: AsyncSession, code: str) -> PromoCode | None:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(PromoCode).where(
            PromoCode.code == code.upper(),
            PromoCode.is_active == True,
            (PromoCode.expires_at == None) | (PromoCode.expires_at > now),
        )
    )
    return result.scalar_one_or_none()


async def has_redeemed(session: AsyncSession, user_id: int, promo_id: int) -> bool:
    result = await session.execute(
        select(PromoRedemption).where(
            PromoRedemption.user_id == user_id,
            PromoRedemption.promo_code_id == promo_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def redeem_promo(session: AsyncSession, user_id: int, promo: PromoCode) -> None:
    redemption = PromoRedemption(user_id=user_id, promo_code_id=promo.id)
    session.add(redemption)
    new_count = promo.used_count + 1
    values: dict = {'used_count': new_count}
    if promo.max_uses > 0 and new_count >= promo.max_uses:
        values['is_active'] = False
    await session.execute(
        update(PromoCode).where(PromoCode.id == promo.id).values(**values)
    )
    await session.flush()


async def create_promo(
    session: AsyncSession,
    code: str,
    credits: int,
    max_uses: int = 0,
    expires_at: datetime | None = None,
) -> PromoCode:
    promo = PromoCode(
        code=code.upper(),
        credits=credits,
        max_uses=max_uses,
        expires_at=expires_at,
    )
    session.add(promo)
    await session.flush()
    await session.refresh(promo)
    return promo


async def deactivate_promo(session: AsyncSession, code: str) -> bool:
    result = await session.execute(
        update(PromoCode)
        .where(PromoCode.code == code.upper())
        .values(is_active=False)
        .returning(PromoCode.id)
    )
    return result.scalar_one_or_none() is not None


async def list_promos(session: AsyncSession) -> list[PromoCode]:
    result = await session.execute(
        select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)
    )
    return list(result.scalars().all())

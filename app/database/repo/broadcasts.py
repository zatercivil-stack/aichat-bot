from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Broadcast, PromoCode


async def get_broadcasts(session: AsyncSession, limit: int = 20) -> List[Broadcast]:
    """Fetch latest broadcasts ordered by started_at DESC."""
    result = await session.execute(
        select(Broadcast).order_by(Broadcast.started_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_broadcast_by_id(session: AsyncSession, broadcast_id: int) -> Broadcast | None:
    result = await session.execute(select(Broadcast).where(Broadcast.id == broadcast_id))
    return result.scalar_one_or_none()


async def get_promo_analytics(session: AsyncSession) -> List[dict]:
    """
    Returns promo analytics: for each promo code, counts redemptions and related broadcasts.
    """
    from app.database.models import PromoRedemption

    stmt = select(
        PromoCode.id,
        PromoCode.code,
        PromoCode.credits,
        PromoCode.max_uses,
        PromoCode.used_count,
        PromoCode.is_active,
        PromoCode.expires_at,
        PromoCode.created_at,
        func.count(PromoRedemption.id).label('redemptions'),
        func.min(PromoRedemption.redeemed_at).label('first_used'),
        func.max(PromoRedemption.redeemed_at).label('last_used'),
    ).outerjoin(PromoRedemption).group_by(PromoCode.id).order_by(PromoCode.created_at.desc())

    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings().all()]


# Alias for backwards compatibility
get_promo_stats = get_promo_analytics


async def get_promo_conversion_funnel(session: AsyncSession, promo_code: str) -> dict:
    """
    Returns conversion funnel for a specific promo code:
    - total_sent: from broadcasts where this promo was used
    - delivered: sum of sent_count (successful)
    - clicked: number of redemptions (actual activations)
    """
    from app.database.models import PromoRedemption

    # Get broadcast stats for this promo
    bc_stmt = select(
        func.sum(Broadcast.total_recipients).label('total_sent'),
        func.sum(Broadcast.sent_count).label('delivered'),
    ).where(Broadcast.promo_code == promo_code)
    bc_res = await session.execute(bc_stmt)
    bc_row = bc_res.mappings().one()

    # Count redemptions
    redemption_count = await session.scalar(
        select(func.count(PromoRedemption.id)).join(PromoCode).where(PromoCode.code == promo_code)
    ) or 0

    return {
        'promo_code': promo_code,
        'total_sent': bc_row['total_sent'] or 0,
        'delivered': bc_row['delivered'] or 0,
        'clicked': redemption_count,
    }

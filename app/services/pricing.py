"""Pricing helpers — applies launch-price promo to image features.

During the promo window (defined in BotConfig), `image_edit` and `face_swap`
ops are discounted by N кр. After promo ends — base price.

Used by chat handlers (when charging), by UI (button labels), and by
admin pricing screen (to show base vs effective).
"""

from __future__ import annotations

from datetime import date as _date

import structlog

from app.database.base import AsyncSessionFactory
from app.database.repo.bot_config import get_config_value
from app.database.repo.credits import MODEL_COSTS

logger = structlog.get_logger(__name__)


# Какие операции попадают под promo. Остальные считаются по базовой цене.
PROMO_OPERATIONS = ("image_edit", "image_edit_precise", "face_swap")


async def _promo_active(session) -> tuple[bool, str, int]:
    """Returns (is_active, until_iso, discount). If disabled — (False, '', 0)."""
    until = (await get_config_value(session, "image_promo_until") or "").strip()
    if not until:
        return False, "", 0
    try:
        end = _date.fromisoformat(until)
    except ValueError:
        return False, "", 0
    from app.utils import today_msk

    # Промо живёт по МСК (юзеры в России) — иначе акция «истекает в полночь»
    # на 3 часа раньше для пользователей.
    if today_msk() > end:
        return False, until, 0
    discount_raw = (await get_config_value(session, "image_promo_discount") or "0").strip()
    try:
        discount = int(discount_raw)
    except ValueError:
        discount = 0
    if discount <= 0:
        return False, until, 0
    return True, until, discount


async def effective_cost(operation: str, session=None) -> int:
    """Цена операции с учётом promo."""
    base = MODEL_COSTS.get(operation, 0)
    if operation not in PROMO_OPERATIONS:
        return base
    if session is None:
        async with AsyncSessionFactory() as s:
            active, _, discount = await _promo_active(s)
    else:
        active, _, discount = await _promo_active(session)
    if active and discount > 0:
        return max(0, base - discount)
    return base


async def get_promo_info() -> dict:
    """Возвращает {active, until, discount, base_costs, effective_costs}."""
    async with AsyncSessionFactory() as session:
        active, until, discount = await _promo_active(session)
        eff = {}
        for op in PROMO_OPERATIONS:
            base = MODEL_COSTS.get(op, 0)
            eff[op] = max(0, base - discount) if (active and discount > 0) else base
    return {
        "active": active,
        "until": until,
        "discount": discount,
        "base_costs": {op: MODEL_COSTS.get(op, 0) for op in PROMO_OPERATIONS},
        "effective_costs": eff,
    }

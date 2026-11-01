"""Реф-кэшбэк (вариант B) — монетизирующая ветка рефералки.

Идея: когда ПРИГЛАШЁННЫЙ впервые ОПЛАЧИВАЕТ, пригласившему капает доля платежа
как СКИДКА на его СОБСТВЕННУЮ покупку пака (не бесплатные кредиты — только в
зачёт оплаты). Это тянет самого реферера в платящие и математически не уводит
в минус: кэшбэк < платежа друга, а на чекауте скидка ограничена долей цены.

Настройки реферальной программы:
- CASHBACK_RATE 30% — доля платежа друга → в кэшбэк-баланс реферера.
- MAX_DISCOUNT_RATE 30% — потолок скидки на ОДНУ покупку (маржа-гард).
- TTL 14 дней — окно срочности; продлевается при каждом новом платеже друга.
- Скидка применяется ТОЛЬКО к обычным пакам (290/690/1790). Подписки и 99₽
  burn-стартер исключены — у них тонкая маржа/они loss-leader.

Баланс — в Redis (AOF-персистентный), ключ ref_cashback:{tg}, в рублях (int).
Все операции defensive: при сбое Redis возвращаем «нет скидки» (полная цена),
никогда не ломаем оплату и не списываем лишнего.
"""

from __future__ import annotations

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

CASHBACK_RATE = 0.30
MAX_DISCOUNT_RATE = 0.30
CASHBACK_TTL_DAYS = 14
# Паки, к которым применяется скидка (маржа положительна даже при −30%).
ELIGIBLE_PACK_IDS = {"pack_2000", "pack_6000", "pack_20000"}

_KEY = "ref_cashback:{tg}"


def _redis():
    from redis.asyncio import Redis

    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def grant(referrer_tg: int, friend_paid_rub: float) -> int:
    """Начисляет рефереру кэшбэк (30% платежа друга) в ₽. Продлевает TTL.
    Возвращает начисленную сумму (₽) или 0."""
    try:
        amount = int(float(friend_paid_rub) * CASHBACK_RATE)
    except (TypeError, ValueError):
        return 0
    if amount <= 0:
        return 0
    r = _redis()
    try:
        key = _KEY.format(tg=referrer_tg)
        await r.incrby(key, amount)
        await r.expire(key, CASHBACK_TTL_DAYS * 24 * 3600)
        return amount
    except Exception as exc:
        logger.warning("cashback grant failed", referrer=referrer_tg, error=str(exc)[:150])
        return 0
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


async def get_balance(user_tg: int) -> int:
    """Текущий кэшбэк-баланс юзера (₽), 0 если нет/сгорел/сбой."""
    r = _redis()
    try:
        v = await r.get(_KEY.format(tg=user_tg))
        return max(0, int(v)) if v else 0
    except Exception:
        return 0
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


def discount_for(balance_rub: int, price_rub: float) -> int:
    """Сколько ₽ скидки применится к покупке price_rub при балансе balance_rub.
    Чистая функция (для тестов): min(баланс, 30% цены)."""
    try:
        cap = int(float(price_rub) * MAX_DISCOUNT_RATE)
    except (TypeError, ValueError):
        return 0
    return max(0, min(int(balance_rub), cap))


async def preview_discount(user_tg: int, price_rub: float) -> int:
    """Скидка к покупке за price_rub без списания (для UI/чекаута)."""
    return discount_for(await get_balance(user_tg), price_rub)


async def redeem(user_tg: int, used_rub: int) -> None:
    """Списывает использованную часть кэшбэка после успешной оплаты."""
    try:
        used = int(used_rub)
    except (TypeError, ValueError):
        return
    if used <= 0:
        return
    r = _redis()
    try:
        key = _KEY.format(tg=user_tg)
        newbal = await r.incrby(key, -used)
        if newbal is not None and int(newbal) <= 0:
            await r.delete(key)
    except Exception as exc:
        logger.warning("cashback redeem failed", user=user_tg, error=str(exc)[:150])
    finally:
        try:
            await r.aclose()
        except Exception:
            pass

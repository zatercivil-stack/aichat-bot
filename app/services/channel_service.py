from __future__ import annotations

import structlog
from aiogram import Bot

logger = structlog.get_logger(__name__)

CHANNEL = "@conturD"
_CACHE_TTL = 4 * 3600  # 4 часа


def _sub_key(user_tg_id: int) -> str:
    return f"chan_sub:{user_tg_id}"


def _free_key(user_tg_id: int, today: str) -> str:
    return f"daily_free:{user_tg_id}:{today}"


async def check(bot: Bot, redis, user_tg_id: int) -> bool:
    """Проверяет подписку на канал. Результат кешируется в Redis на 4 часа."""
    key = _sub_key(user_tg_id)
    cached = await redis.get(key)
    if cached is not None:
        return cached == "1"
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL, user_id=user_tg_id)
        ok = member.status in ("member", "administrator", "creator")
    except Exception as exc:
        err = str(exc)
        if "inaccessible" in err or "not enough rights" in err.lower() or "Forbidden" in err:
            logger.error(
                "channel_check_bot_not_admin",
                hint="Add @NeyraChatBot as admin to the channel",
                error=err[:300],
            )
        else:
            logger.warning("channel_sub_check_failed", error=err[:200])
        ok = False
    await redis.set(key, "1" if ok else "0", ex=_CACHE_TTL)
    return ok


async def invalidate(redis, user_tg_id: int) -> None:
    await redis.delete(_sub_key(user_tg_id))


async def is_subscribed(bot: Bot, user_tg_id: int) -> bool:
    """Подписан ли юзер на канал (с 4ч-кэшем). Тонкая обёртка над check() с
    собственным singleton-redis — удобно дёргать из любого хендлера, где нет
    инжектнутого redis. Никогда не падает: при ошибке возвращает False."""
    try:
        return await check(bot, _get_redis(), user_tg_id)
    except Exception as exc:
        logger.warning("is_subscribed failed", error=str(exc)[:200])
        return False


async def mark_free_tier(redis, user_tg_id: int, today: str) -> None:
    """Помечает, что пользователь сегодня получил free-tier ежедневный бонус."""
    await redis.set(_free_key(user_tg_id, today), "1", ex=86400)


async def consume_free_tier(redis, user_tg_id: int, today: str) -> bool:
    """Атомарно: если флаг есть — удаляет его и возвращает True (юзер ИМЕЕТ
    право на top-up за подписку). Если нет — False. Защита от race: двойной
    клик «Я подписался» не даст двойной начисление, потому что GETDEL
    атомарен внутри Redis. Заменяет was_free_tier + clear_free_tier."""
    val = await redis.getdel(_free_key(user_tg_id, today))
    return val == "1"


# ── Channel-aware ежедневный грант ──────────────────────────────────────────
# Один lazy-singleton redis-клиент: фото-хендлеры дёргают грант на каждое
# действие, плодить соединения через Redis.from_url не нужно.
_redis_singleton = None


def _get_redis():
    global _redis_singleton
    if _redis_singleton is None:
        from redis.asyncio import Redis

        from app.config import settings

        _redis_singleton = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_singleton


async def grant_daily_aware(bot: Bot, session, user) -> int:
    """Channel-aware обёртка над grant_daily_if_needed.

    Сам резолвит подписку на @conturD (с 4ч-кэшем) и передаёт is_channel_sub
    в грант — иначе подписчики канала молча получали бы free-tier (15) вместо
    обещанных 40. Если выдан именно free-tier — ставит redis-флаг, чтобы
    cb_check_channel_sub мог доначислить разницу при поздней подписке в тот же
    день.

    Возвращает сумму начисленных кредитов (0 если уже начислено сегодня).
    """
    from app.database.repo.credits import DAILY_FREE_CREDITS, grant_daily_if_needed
    from app.utils import today_msk

    today = today_msk()
    # Уже начислено сегодня — не дёргаем Telegram API ради channel-check.
    if user.daily_credits_granted_at == today:
        return 0

    redis = _get_redis()
    is_channel = False
    try:
        is_channel = await check(bot, redis, user.telegram_id)
    except Exception as exc:
        logger.warning("grant_daily_aware channel check failed", error=str(exc)[:200])
        is_channel = False

    granted = await grant_daily_if_needed(session, user, is_channel_sub=is_channel)

    # free-tier (15) ⟺ нет подписки И не в канале. Помечаем для top-up.
    if granted == DAILY_FREE_CREDITS and not is_channel:
        try:
            await mark_free_tier(redis, user.telegram_id, today.isoformat())
        except Exception as exc:
            logger.warning("mark_free_tier failed", error=str(exc)[:200])

    return granted

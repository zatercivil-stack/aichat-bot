"""Rate-limiting middleware: per-user sliding window using Redis.

Pre-AI message rate-limit:
- 5 messages per 10s — soft cap (warns then ignores)
- 30 messages per 60s — hard cap (silently drops)
- 200 messages per 1h — abuse cap (silently drops + flags admin)

Admins are exempt.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.config import settings

logger = structlog.get_logger(__name__)

# (limit, window_seconds, kind)
RULES = [
    (5,   10,   'burst'),    # short burst
    (30,  60,   'minute'),   # per-minute
    (200, 3600, 'hour'),     # hourly abuse
]


class RateLimitMiddleware(BaseMiddleware):
    """Drops messages from a user that exceed our rate caps."""

    def __init__(self, redis_factory):
        super().__init__()
        self._redis_factory = redis_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Only rate-limit Message events (skip callbacks, inline queries, etc.)
        if not isinstance(event, Message):
            return await handler(event, data)

        tg_user = event.from_user
        if not tg_user or tg_user.is_bot:
            return await handler(event, data)

        # Admins always pass
        admin_ids = settings.admin_ids_list
        if admin_ids and tg_user.id in admin_ids:
            return await handler(event, data)

        redis = self._redis_factory()
        if redis is None:
            return await handler(event, data)

        now = int(time.time())
        breached = None
        try:
            for limit, window, kind in RULES:
                key = f'ratelimit:{kind}:{tg_user.id}'
                # Sliding window via sorted set: add timestamp, drop old, count
                pipe = redis.pipeline()
                pipe.zadd(key, {str(now): now})
                pipe.zremrangebyscore(key, 0, now - window)
                pipe.zcard(key)
                pipe.expire(key, window + 5)
                _, _, count, _ = await pipe.execute()
                if count > limit:
                    breached = (limit, window, kind, count)
                    break
        except Exception as exc:
            logger.warning('rate-limit check failed', error=str(exc)[:200])
            return await handler(event, data)

        if breached:
            limit, window, kind, count = breached
            logger.warning('rate-limit hit', telegram_id=tg_user.id, kind=kind, count=count, limit=limit)
            try:
                # Only warn on burst breaches — silent on the rest to avoid spamming
                if kind == 'burst':
                    await event.answer(
                        '⚡ Слишком быстро. Подожди пару секунд и попробуй снова.',
                    )
                # For hour-window: notify admin once per user/day
                if kind == 'hour':
                    admin_alert_key = f'admin_alert:abuse:{tg_user.id}:{now // 86400}'
                    if await redis.set(admin_alert_key, '1', nx=True, ex=86400):
                        from app.services.admin_notify import alert_admins
                        await alert_admins(
                            f'🚨 <b>Подозрение на абуз</b>\n\n'
                            f'TG: <code>{tg_user.id}</code> @{tg_user.username or "—"}\n'
                            f'Превышен лимит {limit}/час (текущее значение: {count})',
                        )
            except Exception as exc:
                logger.warning('rate-limit notify failed', error=str(exc)[:200])
            return  # drop the event

        return await handler(event, data)

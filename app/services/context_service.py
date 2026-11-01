from __future__ import annotations

import json

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

MAX_MESSAGES = 14  # 7 пар user/assistant — баланс памяти и стоимости pro-тира
CTX_TTL = 60 * 60 * 24  # 24 часа без активности — контекст сбрасывается


def _key(user_id: int) -> str:
    return f"aichat:ctx:{user_id}"


async def get_context(redis: Redis, user_id: int) -> list[dict]:
    raw = await redis.get(_key(user_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


async def add_message(redis: Redis, user_id: int, role: str, content: str) -> None:
    messages = await get_context(redis, user_id)
    messages.append({"role": role, "content": content})
    if len(messages) > MAX_MESSAGES:
        messages = messages[-MAX_MESSAGES:]
    await redis.set(_key(user_id), json.dumps(messages, ensure_ascii=False), ex=CTX_TTL)


async def clear_context(redis: Redis, user_id: int) -> None:
    await redis.delete(_key(user_id))


async def get_context_info(redis: Redis, user_id: int) -> tuple[int, int]:
    """Возвращает (кол-во сообщений в контексте, секунд до истечения).
    Если контекста нет — (0, 0)."""
    key = _key(user_id)
    raw = await redis.get(key)
    if not raw:
        return 0, 0
    try:
        messages = json.loads(raw)
    except Exception:
        return 0, 0
    ttl = await redis.ttl(key)
    return len(messages), max(0, ttl)

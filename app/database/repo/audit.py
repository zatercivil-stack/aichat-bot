"""Admin audit log — append-only, never raises."""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.database.base import AsyncSessionFactory
from app.database.models import AdminAction

logger = structlog.get_logger(__name__)


async def log_action(
    admin_id: str, action: str,
    target_user_id: int | None = None,
    ip: str | None = None,
    **payload: Any,
) -> None:
    """Persist an admin action. Best-effort, swallows all errors.

    `admin_id`: 'token' for shared-token auth (current model), or TG id as string.
    `action`: short verb, e.g. 'user.ban', 'user.credits', 'promo.create'.
    `target_user_id`: optional User.id this action affects.
    `payload`: any JSON-serializable kwargs for context.
    """
    try:
        async with AsyncSessionFactory() as session:
            entry = AdminAction(
                admin_id=admin_id[:64],
                action=action[:64],
                target_user_id=target_user_id,
                ip=ip[:64] if ip else None,
                payload=payload,
            )
            session.add(entry)
            await session.commit()
    except Exception as exc:
        logger.warning('audit log_action failed', action=action, error=str(exc)[:200])


def log_action_bg(
    admin_id: str, action: str,
    target_user_id: int | None = None,
    ip: str | None = None,
    **payload: Any,
) -> None:
    """Fire-and-forget — usable from sync flows."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(log_action(admin_id, action, target_user_id, ip, **payload))
    except RuntimeError:
        pass

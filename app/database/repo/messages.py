from __future__ import annotations

import asyncio

import structlog

from app.database.base import AsyncSessionFactory
from app.database.models import ChatMessage

logger = structlog.get_logger(__name__)


async def save_message(
    user_id: int, role: str, content: str,
    model_key: str | None = None, credits_spent: int = 0,
) -> None:
    """Persist a single chat message. Truncates very long content."""
    try:
        async with AsyncSessionFactory() as session:
            msg = ChatMessage(
                user_id=user_id,
                role=role,
                content=content[:20000],  # cap at 20KB per message
                model_key=model_key,
                credits_spent=credits_spent,
            )
            session.add(msg)
            await session.commit()
    except Exception as exc:
        logger.warning('save_message failed', user_id=user_id, role=role, error=str(exc)[:200])


def save_message_bg(user_id: int, role: str, content: str,
                    model_key: str | None = None, credits_spent: int = 0) -> None:
    """Fire-and-forget version usable from sync contexts."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(save_message(user_id, role, content, model_key, credits_spent))
    except RuntimeError:
        logger.warning('save_message_bg: no running loop')

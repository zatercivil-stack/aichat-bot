from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.database.base import AsyncSessionFactory
from app.database.repo.users import get_or_create_user, update_last_active

logger = structlog.get_logger(__name__)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update: Update = data.get('event_update') or data.get('update')

        tg_user = None
        if hasattr(event, 'from_user') and event.from_user:
            tg_user = event.from_user
        elif hasattr(event, 'message') and event.message and event.message.from_user:
            tg_user = event.message.from_user
        elif hasattr(event, 'callback_query') and event.callback_query and event.callback_query.from_user:
            tg_user = event.callback_query.from_user

        if tg_user and not tg_user.is_bot:
            async with AsyncSessionFactory() as session:
                user, created = await get_or_create_user(
                    session,
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    full_name=tg_user.full_name or '',
                    language_code=tg_user.language_code or 'ru',
                )
                await update_last_active(session, tg_user.id)
                await session.commit()
                data['db_user'] = user
                data['is_new_user'] = created
                if created:
                    logger.info('New user registered', telegram_id=tg_user.id, username=tg_user.username)

        return await handler(event, data)

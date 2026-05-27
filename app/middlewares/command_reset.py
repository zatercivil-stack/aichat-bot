"""Outer-middleware: при получении /команды чистим текущий FSM-state.

Иначе FSM-хендлеры (ImageState.waiting_prompt, PromoState.waiting_code и т.п.)
перехватывают любое сообщение и обрабатывают /start как «промпт для картинки»
или «текст промокода». Юзер тыкает «Меню → /start» и попадает не туда.

Outer-middleware вызывается до фильтров — успеваем сбросить state, и Command-фильтр
матчится корректно.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog
from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

logger = structlog.get_logger(__name__)


class CommandResetStateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text and event.text.startswith('/'):
            state: FSMContext | None = data.get('state')
            if state is not None:
                try:
                    if await state.get_state() is not None:
                        await state.clear()
                except Exception as exc:
                    logger.warning('command-reset state clear failed', error=str(exc)[:200])
        return await handler(event, data)

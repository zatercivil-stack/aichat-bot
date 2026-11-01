"""Outer-middleware: при получении /команды чистим текущий FSM-state.

Иначе FSM-хендлеры (ImageState.waiting_prompt, PromoState.waiting_code и т.п.)
перехватывают любое сообщение и обрабатывают /start как «промпт для картинки»
или «текст промокода». Юзер тыкает «Меню → /start» и попадает не туда.

Outer-middleware вызывается до фильтров — успеваем сбросить state, и Command-фильтр
матчится корректно.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

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
        if isinstance(event, Message) and event.text and event.text.startswith("/"):
            state: FSMContext | None = data.get("state")
            if state is not None:
                try:
                    from app.states import SupportState

                    cur = await state.get_state()
                    # Режим тикета — НЕ сбрасываем: пользователь в «опиши проблему»,
                    # любой текст (в т.ч. начинающийся с /) = тело обращения; выход —
                    # кнопкой «Отмена». Иначе такое сообщение терялось бы молча.
                    if cur is not None and cur != SupportState.waiting_message.state:
                        await state.clear()
                except Exception as exc:
                    logger.warning("command-reset state clear failed", error=str(exc)[:200])
        return await handler(event, data)

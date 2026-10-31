from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import update_streak
from app.database.repo.users import get_or_create_user, get_user, update_last_active
from app.utils import premium_text

logger = structlog.get_logger(__name__)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.get('event_update') or data.get('update')

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

            # Стрик — отдельной сессией ПОСЛЕ коммита upsert'а: сбой серии
            # (DB-ошибка, отравление транзакции) не должен ронять апдейт и
            # критичный upsert юзера. Отмечаем на ЛЮБОЙ заход (раньше серия
            # двигалась только в /start, /status, каталоге, генерации, чате →
            # кто ходил через тапы по пушам/навигацию, для серии «не заходил» →
            # вечный streak=1). Идемпотентно за МСК-сутки + SELECT FOR UPDATE.
            await self._mark_streak(tg_user.id, data.get('bot'))

        return await handler(event, data)

    async def _mark_streak(self, tg_id: int, bot: Any) -> None:
        """Отмечает заход в серию и празднует milestone. Полностью изолирован:
        любой сбой логируется и проглатывается, основной flow не страдает."""
        try:
            async with AsyncSessionFactory() as session:
                user = await get_user(session, tg_id)
                if user is None:
                    return
                days, bonus = await update_streak(session, user)
                await session.commit()
        except Exception as exc:
            logger.warning('streak update failed', error=str(exc)[:200])
            return

        # Milestone-бонус уже начислен в update_streak (закоммичен) — здесь
        # только празднование. В обработчиках update_streak станет идемпотентным
        # (last_date==today) → bonus=0, повтора не будет.
        if bonus > 0 and bot is not None:
            try:
                await bot.send_message(
                    tg_id,
                    premium_text(
                        f'🔥 <b>{days} дней подряд!</b> Держи '
                        f'<b>+{bonus} кр</b> в подарок за серию.'
                    ),
                    parse_mode='HTML',
                )
            except Exception as exc:
                logger.warning('streak celebration failed', error=str(exc)[:200])

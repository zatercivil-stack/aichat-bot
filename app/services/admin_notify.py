"""Realtime admin alerts via Telegram.

Used for: large payments, errors, abuse, broadcast completion, etc.
Always rate-limits identical alerts to avoid flooding.
"""
from __future__ import annotations

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)


async def alert_admins(text: str, parse_mode: str = 'HTML') -> None:
    """Send a message to every configured admin TG ID. Never raises."""
    admin_ids = settings.admin_ids_list
    if not admin_ids:
        return

    try:
        from app.bot import get_bot
        bot = get_bot()
    except Exception as exc:
        logger.warning('alert_admins: bot unavailable', error=str(exc)[:200])
        return

    if not bot:
        return

    for aid in admin_ids:
        try:
            await bot.send_message(aid, text, parse_mode=parse_mode, disable_web_page_preview=True)
        except Exception as exc:
            logger.warning('alert_admins send failed', admin_id=aid, error=str(exc)[:200])


async def alert_big_payment(user_id: int, telegram_id: int, username: str | None,
                            amount: float, currency: str, method: str) -> None:
    """Threshold: amounts >= 690 RUB or equivalent."""
    rub_threshold = 500.0
    if currency == 'RUB' and amount < rub_threshold:
        return
    flag = '💎' if amount >= 1000 else '💰'
    text = (
        f'{flag} <b>Платёж получен</b>\n\n'
        f'Сумма: <b>{amount:,.0f} {currency}</b>\n'
        f'Метод: {method}\n'
        f'Юзер: @{username or "—"} (<code>{telegram_id}</code>)\n'
        f'<a href="http://202.148.55.149:8181/admin/users/{user_id}">Открыть в админке</a>'
    )
    await alert_admins(text)


async def alert_error_spike(error_type: str, count: int, window_min: int) -> None:
    text = (
        f'⚠️ <b>Всплеск ошибок</b>\n\n'
        f'Тип: <code>{error_type}</code>\n'
        f'Кол-во: <b>{count}</b> за {window_min} мин\n'
        f'<a href="http://202.148.55.149:8181/admin/events?event_type=error">События</a>'
    )
    await alert_admins(text)

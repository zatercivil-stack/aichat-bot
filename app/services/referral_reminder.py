"""Реф-напоминание: пингует реферера, чей приглашённый зашёл по ссылке, но ещё
НЕ сделал ни одной генерации (бонус срабатывает на первом фото друга). Один пинг
на реферал (кулдаун в Redis), ночью (23-09 МСК) молчим. Цель — дожать тёплый
реферал до выплаты (= рост базы бесплатно)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import structlog
from sqlalchemy import text as sa_text

from app.config import settings
from app.constants import REFERRAL_BONUS
from app.database.base import AsyncSessionFactory
from app.utils import premium_text

logger = structlog.get_logger(__name__)

_COOLDOWN_DAYS = 5  # один пинг на реферал


def _redis():
    from redis.asyncio import Redis
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def _current_reward() -> tuple[int, datetime | None]:
    """Награда за друга с учётом спарка. Возвращает (сумма, дедлайн_акции|None).
    Дедлайн не-None только пока спарк активен — тогда в тексте подсветим срок."""
    from app.database.repo.bot_config import get_config_value
    amount = REFERRAL_BONUS
    until: datetime | None = None
    try:
        async with AsyncSessionFactory() as s:
            bu_raw = (await get_config_value(s, 'referral_boost_until') or '').strip()
            if bu_raw:
                bu = datetime.fromisoformat(bu_raw)
                if bu.tzinfo is None:
                    bu = bu.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) <= bu:
                    amt = (await get_config_value(s, 'referral_boost_amount') or '500').strip()
                    amount = max(REFERRAL_BONUS, int(amt))
                    until = bu
    except Exception:
        pass
    return amount, until


def _kb_for(referrer_tg: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from app.keyboards.main import PE_HANDSHAKE
    bu = settings.BOT_USERNAME or 'NeyraChatBot'
    link = f'https://t.me/{bu}?start=ref_{referrer_tg}'
    txt = ('Я в Нейре — кидаешь селфи, получаешь себя в любой сцене. '
           'Попробуй, тебе 500 кр на старт.')
    share = f'https://t.me/share/url?url={quote(link, safe="")}&text={quote(txt, safe="")}'
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton.model_construct(
            text='Напомнить другу', url=share,
            icon_custom_emoji_id=PE_HANDSHAKE, style='success')]])


def _text(amount: int, until: datetime | None = None) -> str:
    if until and amount > REFERRAL_BONUS:
        # дедлайн акции в МСК (UTC+3)
        d = until.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m')
        return premium_text(
            '🎁 <b>Твой друг уже в Нейре!</b>\n\n'
            'Чтобы тебе прилетел бонус, нужно, чтобы он сделал хотя бы одно фото.\n\n'
            f'🔥 <b>Сейчас по акции — целых +{amount} кр за друга</b> '
            f'(вместо {REFERRAL_BONUS}), но только <b>до {d} включительно</b> — потом обычные '
            f'+{REFERRAL_BONUS}. Напомни ему: пусть пришлёт селфи, минута 👇'
        )
    return premium_text(
        '🎁 <b>Твой друг уже в Нейре!</b>\n\n'
        f'Чтобы тебе прилетел бонус — <b>+{amount} кр на неделю</b> — нужно, '
        'чтобы он сделал хотя бы одно фото.\n\n'
        'Напомни ему: пусть пришлёт селфи — минута, и кредиты твои 👇'
    )


async def send_referral_stall_reminders(bot) -> None:
    msk_hour = (datetime.now(timezone.utc) + timedelta(hours=3)).hour
    if msk_hour < 9 or msk_hour >= 23:
        return
    try:
        async with AsyncSessionFactory() as session:
            rows = (await session.execute(sa_text("""
                SELECT referee.id, referee.referrer_id
                FROM users referee
                WHERE referee.referrer_id IS NOT NULL
                  AND referee.is_banned = false
                  AND referee.created_at < now() - interval '12 hours'
                  AND referee.created_at > now() - interval '7 days'
                  AND referee.id NOT IN (
                      SELECT DISTINCT user_id FROM bot_events WHERE event_type='image_edited')
                ORDER BY referee.created_at DESC
                LIMIT 500
            """))).all()
    except Exception as exc:
        logger.warning('referral stall query failed', error=str(exc)[:200])
        return
    if not rows:
        return

    amount, until = await _current_reward()
    redis = _redis()
    sent = 0
    seen_referrers: set[int] = set()
    try:
        for referee_id, referrer_tg in rows:
            referrer_tg = int(referrer_tg)
            if referrer_tg in seen_referrers:
                continue  # 1 пинг на реферера за прогон
            # кулдаун — один пинг на конкретный реферал
            try:
                fresh = await redis.set(f'ref_stall_remind:{referee_id}', '1',
                                        nx=True, ex=_COOLDOWN_DAYS * 86400)
            except Exception:
                fresh = True
            if not fresh:
                continue
            seen_referrers.add(referrer_tg)
            try:
                await bot.send_message(referrer_tg, _text(amount, until),
                                       parse_mode='HTML', reply_markup=_kb_for(referrer_tg))
                sent += 1
            except Exception as exc:
                err = str(exc).lower()
                if not any(s in err for s in ('forbidden', 'blocked', 'deactivated')):
                    logger.warning('referral reminder send failed',
                                   referrer=referrer_tg, error=str(exc)[:150])
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass
    if sent:
        logger.info('referral stall reminders sent', sent=sent)

"""One-off рассылка АКТИВНЫМ за 30 дней — анонс двойной реф-выгоды (Tier1 +250кр
за первое фото друга, Tier2 скидка 30% когда друг платит). Цель — запустить
реф-петлю. Стоимость 0₽ (текст). Идемпотентно (Broadcast с PROMO_CODE).

Кнопка — ПЕРСОНАЛЬНАЯ share-ссылка получателя (1 тап → переслать другу).

Запуск:
  python -m scripts.referral_announce_2026_06_07 --dry-run   # пример админам
  python -m scripts.referral_announce_2026_06_07 --send      # реальная отправка
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from urllib.parse import quote

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text as sa_text

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Broadcast
from app.database.repo.events import log_event
from app.utils import premium_text

logger = structlog.get_logger(__name__)

SOURCE = 'neyra'
PROMO_CODE = 'REFERRAL_ANNOUNCE_2026_06_07'


def _text() -> str:
    return premium_text(
        '🤝 <b>Новое: зови друзей — выгода вдвойне</b>\n\n'
        '• Друг сделал первое фото → <b>+250 кр</b> тебе\n'
        '• Друг купил пак или подписку → <b>скидка 30%</b> на твою покупку (14 дней)\n\n'
        'Чем больше друзей платят — тем дешевле кредиты тебе. '
        'Жми кнопку — перешли ссылку другу 👇'
    )


def _kb(referrer_tg: int) -> InlineKeyboardMarkup:
    from app.keyboards.main import PE_HANDSHAKE
    bu = settings.BOT_USERNAME or 'NeyraChatBot'
    link = f'https://t.me/{bu}?start=ref_{referrer_tg}'
    share_text = ('Смотри, что я нашёл — Нейра делает фото с твоим лицом в любой '
                  'сцене. Шлёшь селфи → за минуту получаешь себя в Париже, на обложке '
                  'глянца, в неоне. 500 кр в подарок новичку.')
    share = f'https://t.me/share/url?url={quote(link, safe="")}&text={quote(share_text, safe="")}'
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton.model_construct(
            text='Поделиться с друзьями', url=share,
            icon_custom_emoji_id=PE_HANDSHAKE, style='success')]])


async def _fetch_recipients() -> list[tuple[int, int]]:
    """(user_id, telegram_id) — активные за 30 дней, не баннутые, без админ-тестов."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text("""
            SELECT u.id, u.telegram_id FROM users u
            WHERE u.is_banned = false
              AND u.last_active_at >= now() - interval '30 days'
              AND u.id NOT IN (1, 2, 3, 6)
            ORDER BY u.id
        """))).all()
    return [(uid, tg) for uid, tg in rows]


async def _send_one(bot: Bot, tg_id: int) -> str:
    try:
        await bot.send_message(tg_id, _text(), parse_mode='HTML', reply_markup=_kb(tg_id))
        return 'sent'
    except Exception as exc:
        err = str(exc).lower()
        if any(s in err for s in ('forbidden', 'blocked', 'deactivated')):
            return 'blocked'
        logger.warning('send failed', tg_id=tg_id, error=str(exc)[:200])
        return 'error'


async def _already_sent() -> bool:
    async with AsyncSessionFactory() as session:
        b = await session.scalar(
            select(Broadcast).where(Broadcast.promo_code == PROMO_CODE).limit(1))
        return b is not None


async def _create_broadcast_row(total: int) -> int:
    async with AsyncSessionFactory() as session:
        b = Broadcast(source=SOURCE, promo_code=PROMO_CODE, total_recipients=total,
                      started_at=datetime.now(timezone.utc), status='running')
        session.add(b)
        await session.commit()
        return b.id


async def _finalize(bid: int, sent: int, blocked: int, errors: int) -> None:
    async with AsyncSessionFactory() as session:
        b = await session.scalar(select(Broadcast).where(Broadcast.id == bid))
        if not b:
            return
        b.sent_count, b.blocked_count, b.error_count = sent, blocked, errors
        b.completed_at = datetime.now(timezone.utc)
        if b.started_at:
            st = b.started_at
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            b.duration_seconds = int((b.completed_at - st).total_seconds())
        b.status = 'completed'
        await session.commit()


async def run(dry_run: bool) -> None:
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        recipients = await _fetch_recipients()
        total = len(recipients)
        print('\n=== REFERRAL ANNOUNCE ===')
        print(f'Mode:       {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients: {total} (активные 30д)')
        print('=========================\n')
        if dry_run:
            for aid in settings.admin_ids_list:
                await bot.send_message(aid, '🧪 <i>ТЕСТ: анонс реф-выгоды (НЕ опубликовано).</i>', parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid)
            print('✓ Dry-run complete. Проверь Telegram.')
            return
        if await _already_sent():
            print('⏭  Уже отправлено ранее — пропуск.')
            return
        bid = await _create_broadcast_row(total)
        print(f'Started broadcast #{bid}')
        sent = blocked = errors = 0
        for uid, tg in recipients:
            st = await _send_one(bot, tg)
            if st == 'sent':
                sent += 1
                try:
                    await log_event(uid, 'referral_announce_push', source=SOURCE)
                except Exception:
                    pass
            elif st == 'blocked':
                blocked += 1
            else:
                errors += 1
            await asyncio.sleep(0.06)
        await _finalize(bid, sent, blocked, errors)
        print(f'✓ Done. sent={sent} blocked={blocked} errors={errors}')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--send', action='store_true')
    args = ap.parse_args()
    asyncio.run(run(dry_run=args.dry_run))

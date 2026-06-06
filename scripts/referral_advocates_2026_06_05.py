"""One-off рассылка по тем, кому продукт зашёл (сделали ≥1 фото И активны ≤14д) —
приглашение звать друзей по реф-ссылке. Реферал теперь реально платит (burn-250
на неделю + перк «без водяного знака» на 5 друзей), плюс СПАРК: до вс 07.06
двойной бонус (500 за друга вместо 250) — окно в bot_config.referral_boost_until.

Цель: бесплатное расширение базы тёплым трафиком без рекламы. Кредиты в рассылке
НЕ начисляем; награда — burn, и только когда друг реально сделает 1-е фото.
Картинка-герой: welcome-коллаж (file_id) → стоимость 0₽ (нет fal).

Запуск из контейнера (нужен mount scripts или rebuild — файл новый):
  python -m scripts.referral_advocates_2026_06_05 --dry-run   # пример админам
  python -m scripts.referral_advocates_2026_06_05 --send      # реальная отправка
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
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text as sa_text

from app.config import settings
from app.constants import REFERRAL_BONUS
from app.database.base import AsyncSessionFactory
from app.database.models import Broadcast
from app.database.repo.events import log_event
from app.keyboards.main import PE_HANDSHAKE
from app.utils import premium_text

logger = structlog.get_logger(__name__)

SOURCE = 'neyra'
PROMO_CODE = 'REF_ADVOCATE_2026_06_05'
BOOST_AMOUNT = 500          # совпадает с bot_config.referral_boost_amount
BOOST_UNTIL_HUMAN = 'вс 7 июня'


def _render_text() -> str:
    return premium_text(
        f'🔥 <b>Только до {BOOST_UNTIL_HUMAN}: друг приносит тебе +{BOOST_AMOUNT} кр.</b>\n\n'
        'Кидаешь ссылку другу. Как только он сделает первое фото —\n'
        f'🤝 тебе <b>+{BOOST_AMOUNT} кр на неделю</b> (обычно {REFERRAL_BONUS}), ему 500 на старт.\n'
        '👥 5 друзей — неделя без водяного знака.\n\n'
        'Ссылка уже готова — жми «Позвать друга» 👇'
    )


def _build_kb(tg_id: int) -> InlineKeyboardMarkup:
    bu = settings.BOT_USERNAME or 'NeyraChatBot'
    ref_link = f'https://t.me/{bu}?start=ref_{tg_id}'
    share_text = ('Заценил Нейру — кидаешь селфи, получаешь себя в любой сцене. '
                  'Балет у статуи, Париж в неоне. Лицо в точности твоё. 500 кр в подарок новичку.')
    share_url = (f'https://t.me/share/url?url={quote(ref_link, safe="")}'
                 f'&text={quote(share_text, safe="")}')
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Позвать друга', url=share_url,
            icon_custom_emoji_id=PE_HANDSHAKE, style='success')],
    ])


async def _fetch_recipients() -> list[tuple[int, int]]:
    """(user_id, telegram_id) — сделали ≥1 фото И активны ≤14д, не забанены."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text("""
            SELECT u.id, u.telegram_id
            FROM users u
            WHERE u.is_banned = false
              AND u.id NOT IN (1, 2, 3, 6)
              AND u.id IN (SELECT DISTINCT user_id FROM bot_events
                           WHERE event_type = 'image_edited')
              AND u.last_active_at > now() - interval '14 days'
            ORDER BY u.id
        """))).all()
    return [(uid, tg_id) for uid, tg_id in rows]


async def _get_collage_fid(bot: Bot) -> str | None:
    try:
        from app.services.welcome_collage import get_or_build_welcome_collage_fid
        return await get_or_build_welcome_collage_fid(bot)
    except Exception as exc:
        logger.warning('collage fetch failed', error=str(exc)[:200])
        return None


async def _send_one(bot: Bot, tg_id: int, collage_fid: str | None) -> str:
    text, kb = _render_text(), _build_kb(tg_id)
    try:
        if collage_fid:
            await bot.send_photo(tg_id, photo=collage_fid, caption=text,
                                 parse_mode='HTML', reply_markup=kb)
        else:
            await bot.send_message(tg_id, text, parse_mode='HTML',
                                   reply_markup=kb, disable_web_page_preview=True)
        return 'sent'
    except Exception as exc:
        err = str(exc).lower()
        if any(s in err for s in ('forbidden', 'blocked', 'deactivated')):
            return 'blocked'
        logger.warning('send failed', tg_id=tg_id, error=str(exc)[:200])
        return 'error'


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
        collage = await _get_collage_fid(bot)
        total = len(recipients)
        print('\n=== REFERRAL ADVOCATES PLAN ===')
        print(f'Mode:        {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients:  {total} (≥1 фото, активны ≤14д)')
        print(f'Collage:     {"OK" if collage else "NONE (текстом)"}')
        print(f'Спарк:       до {BOOST_UNTIL_HUMAN} награда {BOOST_AMOUNT} (база {REFERRAL_BONUS})')
        print(f'Стоимость:   0₽ (file_id reuse, кредиты не начисляем)')
        print('===============================\n')
        if dry_run:
            admins = settings.admin_ids_list
            print(f'Sample → admins: {admins}')
            for aid in admins:
                await bot.send_message(aid, '🧪 <i>ТЕСТ: рассылка-приглашение звать друзей (спарк ×2). НЕ опубликовано.</i>', parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid, collage)
            print('✓ Dry-run complete. Проверь Telegram.')
            return
        bid = await _create_broadcast_row(total)
        print(f'Started broadcast #{bid}')
        sent = blocked = errors = 0
        for uid, tg_id in recipients:
            st = await _send_one(bot, tg_id, collage)
            if st == 'sent':
                sent += 1
                try:
                    await log_event(uid, 'referral_advocate_push', source=SOURCE)
                except Exception:
                    pass
            elif st == 'blocked':
                blocked += 1
            else:
                errors += 1
            await asyncio.sleep(0.06)
        await _finalize(bid, sent, blocked, errors)
        print(f'✓ Done. sent={sent} blocked={blocked} errors={errors} (reach={sent})')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--send', action='store_true')
    args = ap.parse_args()
    asyncio.run(run(dry_run=args.dry_run))

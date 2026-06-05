"""One-off кампания по «горячей упёршейся» когорте B: юзеры, которые СДЕЛАЛИ
≥1 фото (знают продукт) и у которых total_balance (burn+обычные) в 1–199 кр —
не хватает даже на одно фото (200 кр). Самая ценная и точная цель: вовлечены,
но упёрлись в кредитную стену.

НЕ начисляем новых кредитов (борьба с глутом). Даём три пути продолжить:
  • 🎁 Подарок дня — одно фото в день бесплатно (genuinely free, не впариваем);
  • 🤝 Позови друга — реф теперь РАБОТАЕТ (250 burn/нед, другу 500 welcome);
  • 🔥 Пак 99₽ — для тех, кто готов платить сразу.

Картинка-герой: welcome-коллаж (file_id) → стоимость рассылки 0₽ (нет fal).
Аудитория мала и точна (~11 на 2026-06-04). Reach считаем по факту (blocked не
ловится в is_banned). Событие 'referral_lowcredit_push' → меряем конверсию.

Запуск из контейнера:
  python -m scripts.referral_lowcredit_2026_06_04 --dry-run   # пример админам
  python -m scripts.referral_lowcredit_2026_06_04 --send      # реальная отправка
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
from app.constants import REFERRAL_BONUS
from app.database.base import AsyncSessionFactory
from app.database.models import Broadcast
from app.database.repo.events import log_event
from app.keyboards.main import PE_GIFT, PE_HANDSHAKE, PE_FIRE

logger = structlog.get_logger(__name__)

SOURCE = 'neyra'
PROMO_CODE = 'REF_LOWCR_2026_06_04'    # метка аналитики (НЕ реальный промокод)
EDIT_COST = 200


def _render_text(credits: int) -> str:
    return (
        '⚡ <b>Кредиты на исходе — но Нейра не кончается.</b>\n\n'
        f'У тебя осталось <b>{credits} кр</b>. Три способа продолжить:\n\n'
        '🎁 <b>Подарок дня</b> — одно фото каждый день бесплатно, без списания.\n'
        f'🤝 <b>Позови друга</b> — когда он сделает первое фото, тебе '
        f'<b>+{REFERRAL_BONUS} кр на неделю</b> (а ему 500 на старт). '
        '5 друзей — неделя без водяного знака.\n'
        '🔥 <b>Пак 99₽</b> — 1 500 кр, хватит на 7 топ-сцен.'
    )


def _build_kb(tg_id: int) -> InlineKeyboardMarkup:
    bu = settings.BOT_USERNAME or 'NeyraChatBot'
    ref_link = f'https://t.me/{bu}?start=ref_{tg_id}'
    share_text = ('Заценил Нейру — кидаешь селфи, получаешь себя в любой сцене. '
                  'Лицо в точности твоё.')
    share_url = (f'https://t.me/share/url?url={quote(ref_link, safe="")}'
                 f'&text={quote(share_text, safe="")}')
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Забрать подарок дня', callback_data='mystery_today',
            icon_custom_emoji_id=PE_GIFT, style='success')],
        [InlineKeyboardButton.model_construct(
            text=f'Позвать друга → +{REFERRAL_BONUS}', url=share_url,
            icon_custom_emoji_id=PE_HANDSHAKE, style='primary')],
        [InlineKeyboardButton.model_construct(
            text='Пополнить — 99₽', callback_data='shop',
            icon_custom_emoji_id=PE_FIRE)],
    ])


async def _fetch_recipients() -> list[tuple[int, int, int]]:
    """(user_id, telegram_id, total_balance) для горячей когорты B:
    сделали ≥1 фото И total_balance (burn если не сгорел + обычные) в 1..199."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text("""
            SELECT u.id, u.telegram_id,
                   (COALESCE(CASE WHEN u.burn_expires_at IS NULL
                                  OR u.burn_expires_at > now()
                             THEN u.burn_credits ELSE 0 END, 0) + u.credits) AS bal
            FROM users u
            WHERE u.is_banned = false
              AND u.id NOT IN (1, 2, 3, 6)
              AND u.id IN (SELECT DISTINCT user_id FROM bot_events
                           WHERE event_type = 'image_edited')
              AND (COALESCE(CASE WHEN u.burn_expires_at IS NULL
                                 OR u.burn_expires_at > now()
                            THEN u.burn_credits ELSE 0 END, 0) + u.credits)
                  BETWEEN 1 AND 199
            ORDER BY u.id
        """))).all()
    return [(uid, tg_id, bal or 0) for uid, tg_id, bal in rows]


async def _get_collage_fid(bot: Bot) -> str | None:
    try:
        from app.services.welcome_collage import get_or_build_welcome_collage_fid
        return await get_or_build_welcome_collage_fid(bot)
    except Exception as exc:
        logger.warning('collage fetch failed', error=str(exc)[:200])
        return None


async def _send_one(bot: Bot, tg_id: int, credits: int,
                    collage_fid: str | None) -> str:
    """'sent' | 'blocked' | 'error'."""
    text = _render_text(credits)
    kb = _build_kb(tg_id)
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
        b = Broadcast(source=SOURCE, promo_code=PROMO_CODE,
                      total_recipients=total,
                      started_at=datetime.now(timezone.utc), status='running')
        session.add(b)
        await session.commit()
        return b.id


async def _finalize(broadcast_id: int, sent: int, blocked: int, errors: int) -> None:
    async with AsyncSessionFactory() as session:
        b = await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
        if not b:
            return
        b.sent_count, b.blocked_count, b.error_count = sent, blocked, errors
        b.completed_at = datetime.now(timezone.utc)
        if b.started_at:
            started = b.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            b.duration_seconds = int((b.completed_at - started).total_seconds())
        b.status = 'completed'
        await session.commit()


async def run(dry_run: bool) -> None:
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        recipients = await _fetch_recipients()
        collage_fid = await _get_collage_fid(bot)
        total = len(recipients)
        avg_cr = round(sum(c for _, _, c in recipients) / total) if total else 0

        print('\n=== REFERRAL LOW-CREDIT (cohort B) PLAN ===')
        print(f'Mode:            {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients:      {total} (сделали ≥1 фото, баланс 1–199)')
        print(f'Средний баланс:  {avg_cr} кр')
        print(f'Collage fid:     {"OK" if collage_fid else "NONE (текстом)"}')
        print(f'Стоимость fal:   0₽ (file_id reuse, кредиты не начисляем)')
        print('==========================================\n')

        if dry_run:
            admin_ids = settings.admin_ids_list
            print(f'Sending sample to admins: {admin_ids}')
            for aid in admin_ids:
                await bot.send_message(aid, '<i>📋 DRY-RUN — кампания «мало кредитов»:</i>',
                                       parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid, 150, collage_fid)
            print('✓ Dry-run complete. Проверь Telegram.')
            return

        broadcast_id = await _create_broadcast_row(total)
        print(f'Started broadcast #{broadcast_id}')
        sent = blocked = errors = 0
        for uid, tg_id, credits in recipients:
            status = await _send_one(bot, tg_id, credits, collage_fid)
            if status == 'sent':
                sent += 1
                try:
                    await log_event(uid, 'referral_lowcredit_push', source=SOURCE)
                except Exception:
                    pass
            elif status == 'blocked':
                blocked += 1
            else:
                errors += 1
            await asyncio.sleep(0.06)
        await _finalize(broadcast_id, sent, blocked, errors)
        print(f'✓ Done. sent={sent} blocked={blocked} errors={errors} (reach={sent})')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='пример админам, без рассылки')
    g.add_argument('--send', action='store_true', help='реальная отправка')
    args = ap.parse_args()
    asyncio.run(run(dry_run=args.dry_run))

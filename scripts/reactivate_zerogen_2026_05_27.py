"""One-off реактивация: пуш по zero-gen когорте (зашли в бот, получили кредиты,
не сделали НИ ОДНОГО фото). Вариант A — давим на неиспользованный личный
баланс + Подарок дня, БЕЗ начисления новых кредитов.

Аудитория: users без события image_edited, не забанены, исключая тест-id 1,2,3,6.
На 27.05 это ~83 человека (81 с балансом, в среднем 418 кр; только 2 выставили
пол — отвалились на первом экране).

Картинка-герой: welcome-коллаж результатов (file_id, генерации fal.ai НЕТ →
стоимость рассылки 0₽). Баланс в тексте — личный у каждого.

Запуск из контейнера:
  python -m scripts.reactivate_zerogen_2026_05_27 --dry-run   # 2 примера админам
  python -m scripts.reactivate_zerogen_2026_05_27 --send      # реальная отправка
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Broadcast
from app.database.repo.events import log_event

logger = structlog.get_logger(__name__)

PE_SPARKLES = '5325547803936572038'     # ✨
EDIT_COST = 200                          # кр за одно quality-фото (image_edit.EDIT_COST)
SOURCE = 'neyra'
PROMO_CODE = 'REACT_0GEN_2026_05_27'     # метка для аналитики (не реальный промокод)


def _photos_word(n: int) -> str:
    """Русское склонение «кадр/кадра/кадров»."""
    if n % 100 in (11, 12, 13, 14):
        return 'кадров'
    last = n % 10
    if last == 1:
        return 'кадр'
    if last in (2, 3, 4):
        return 'кадра'
    return 'кадров'


def _render_text(credits: int) -> str:
    """Вариант A, персонализированный по балансу. Для <200 кр (фото не набегает)
    — мягкий вариант без числа."""
    photos = credits // EDIT_COST
    if photos >= 1:
        word = _photos_word(photos)
        head = (
            f'✨ <b>У тебя {credits} кредитов — а первого фото так и нет.</b>\n\n'
            f'Этого хватит на {photos} {word} с твоим лицом: пришли селфи, '
            f'выбери сцену — через минуту получишь фото, которое не отличить '
            f'от студийной съёмки.'
        )
    else:
        head = (
            '✨ <b>Ты заглянул в Нейру, но первого фото так и нет.</b>\n\n'
            'А это всего минута: пришли селфи, выбери сцену — получишь фото '
            'с твоим лицом, которое не отличить от студийной съёмки.'
        )
    return (
        f'{head}\n\n'
        '🎁 А сегодня ещё и Подарок дня — одну сцену можно забрать вообще '
        'без списания.\n\n'
        'Жми и пришли селфи 👇'
    )


def _build_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Сделать первое фото →',
            callback_data='gallery_open',
            icon_custom_emoji_id=PE_SPARKLES, style='success',
        )],
    ])


async def _fetch_recipients() -> list[tuple[int, int, int]]:
    """(user_id, telegram_id, credits) для zero-gen когорты."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text("""
            SELECT id, telegram_id, credits
            FROM users
            WHERE is_banned = false
              AND id NOT IN (1, 2, 3, 6)
              AND id NOT IN (
                  SELECT DISTINCT user_id FROM bot_events
                  WHERE event_type = 'image_edited'
              )
            ORDER BY id
        """))).all()
    return [(uid, tg_id, credits or 0) for uid, tg_id, credits in rows]


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
    kb = _build_kb()
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
                      started_at=datetime.now(UTC), status='running')
        session.add(b)
        await session.commit()
        return b.id


async def _finalize(broadcast_id: int, sent: int, blocked: int, errors: int) -> None:
    async with AsyncSessionFactory() as session:
        b = await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
        if not b:
            return
        b.sent_count, b.blocked_count, b.error_count = sent, blocked, errors
        b.completed_at = datetime.now(UTC)
        if b.started_at:
            started = b.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
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
        with_balance = sum(1 for _, _, c in recipients if c >= EDIT_COST)
        avg_cr = round(sum(c for _, _, c in recipients) / total) if total else 0

        print('\n=== REACTIVATION (zero-gen) PLAN ===')
        print(f'Mode:            {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients:      {total}')
        print(f'С балансом ≥{EDIT_COST}: {with_balance}')
        print(f'Средний баланс:  {avg_cr} кр')
        print(f'Collage fid:     {"OK" if collage_fid else "NONE (текстом)"}')
        print('====================================\n')

        if dry_run:
            admin_ids = settings.admin_ids_list
            print(f'Sending samples to admins: {admin_ids}')
            for aid in admin_ids:
                await bot.send_message(aid, '<i>📋 DRY-RUN — баланс 418 кр (типичный):</i>',
                                       parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid, 418, collage_fid)
                await asyncio.sleep(0.4)
                await bot.send_message(aid, '<i>📋 DRY-RUN — баланс &lt;200 кр (мягкий вариант):</i>',
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
                    await log_event(user_id=uid, event_type='reactivation_push',
                                    model_key=None, credits_spent=0,
                                    source=SOURCE, code=PROMO_CODE)
                except Exception as exc:
                    logger.warning('event log failed', error=str(exc)[:200])
            elif status == 'blocked':
                blocked += 1
            else:
                errors += 1
            await asyncio.sleep(0.06)  # ~16 msg/sec, в пределах лимита TG

        await _finalize(broadcast_id, sent, blocked, errors)
        print('\n=== DONE ===')
        print(f'Sent:    {sent}')
        print(f'Blocked: {blocked}')
        print(f'Errors:  {errors}')
        print('============\n')
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='2 примера админам')
    g.add_argument('--send', action='store_true', help='Реальная отправка по zero-gen')
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == '__main__':
    main()

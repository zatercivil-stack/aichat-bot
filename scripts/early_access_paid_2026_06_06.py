"""One-off рассылка ПЛАТЯЩИМ (активная подписка ИЛИ покупка пака за 30 дней) —
подсветка ценности: у них ранний доступ к новым сценам. Гонит к использованию
новинок (retention/value-reinforcement). Кредиты НЕ начисляем; кнопка ведёт в
раздел «Новинки», где платящий видит 🔓 перк-бейдж. Стоимость 0₽ (file_id).

Запуск из контейнера (mount scripts или rebuild — файл новый):
  python -m scripts.early_access_paid_2026_06_06 --dry-run   # пример админам
  python -m scripts.early_access_paid_2026_06_06 --send      # реальная отправка
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

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
from app.keyboards.main import PE_UNLOCK
from app.utils import premium_text

logger = structlog.get_logger(__name__)

SOURCE = 'neyra'
PROMO_CODE = 'EARLY_ACCESS_PAID_2026_06_06'
HERO_PRESET = 'pink_lux'  # свежая сцена для героя


def _text() -> str:
    return premium_text(
        '🔓 <b>У тебя ранний доступ к новым сценам.</b>\n\n'
        'Свежие образы в Нейре открываются тебе раньше всех — пока остальные ждут. '
        'Уже добавили несколько новинок: примерь первым.\n\n'
        'Спасибо, что ты с нами 💜'
    )


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton.model_construct(
            text='Смотреть новинки', callback_data='gcat:new:0:f',
            icon_custom_emoji_id=PE_UNLOCK, style='success')]])


async def _fetch_recipients() -> list[tuple[int, int]]:
    """(user_id, telegram_id) — активная подписка ИЛИ успешная покупка за 30д."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text("""
            SELECT u.id, u.telegram_id
            FROM users u
            WHERE u.is_banned = false
              AND u.id NOT IN (1, 2, 3, 6)
              AND (
                u.id IN (SELECT user_id FROM subscriptions WHERE is_active = true)
                OR u.id IN (SELECT user_id FROM payments
                            WHERE status='succeeded' AND created_at > now() - interval '30 days')
              )
            ORDER BY u.id
        """))).all()
    return [(uid, tg) for uid, tg in rows]


async def _hero_fid() -> str | None:
    try:
        from app.database.repo.preset_previews import get_preview_file_id
        async with AsyncSessionFactory() as s:
            return await get_preview_file_id(s, HERO_PRESET, position=0)
    except Exception as exc:
        logger.warning('hero preview fetch failed', error=str(exc)[:200])
        return None


async def _send_one(bot: Bot, tg_id: int, hero: str | None) -> str:
    try:
        if hero:
            await bot.send_photo(tg_id, photo=hero, caption=_text(),
                                 parse_mode='HTML', reply_markup=_kb())
        else:
            await bot.send_message(tg_id, _text(), parse_mode='HTML', reply_markup=_kb())
        return 'sent'
    except Exception as exc:
        err = str(exc).lower()
        if any(s in err for s in ('forbidden', 'blocked', 'deactivated')):
            return 'blocked'
        logger.warning('send failed', tg_id=tg_id, error=str(exc)[:200])
        return 'error'


async def _already_sent() -> bool:
    """True, если рассылка с этим PROMO_CODE уже стартовала/завершилась —
    защита от повторной отправки при ретрае cron."""
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
        hero = await _hero_fid()
        total = len(recipients)
        print('\n=== EARLY-ACCESS PAID PLAN ===')
        print(f'Mode:        {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients:  {total} (подписка/пак за 30д)')
        print(f'Hero:        {HERO_PRESET} {"OK" if hero else "NONE"}')
        print(f'Стоимость:   0₽ (file_id, кредиты не начисляем)')
        print('==============================\n')
        if dry_run:
            for aid in settings.admin_ids_list:
                await bot.send_message(aid, '🧪 <i>ТЕСТ: рассылка платящим про ранний доступ. НЕ опубликовано.</i>', parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid, hero)
            print('✓ Dry-run complete. Проверь Telegram.')
            return
        if await _already_sent():
            print('⏭  Уже отправлено ранее (есть Broadcast с этим promo_code) — пропуск.')
            return
        bid = await _create_broadcast_row(total)
        print(f'Started broadcast #{bid}')
        sent = blocked = errors = 0
        for uid, tg in recipients:
            st = await _send_one(bot, tg, hero)
            if st == 'sent':
                sent += 1
                try:
                    await log_event(uid, 'early_access_paid_push', source=SOURCE)
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

"""One-off broadcast: announce photo features (face swap, fast/quality edit).

Two modes:
  --dry-run          : send only to ADMIN_IDS, log expected recipients & bonus
  --send             : real broadcast to all eligible users

Idempotent bonus: tracked via `users.broadcast_photo_2026_05` boolean column
(see migration 015). Bonus is granted exactly once per user.

Usage from container:
  python -m scripts.broadcast_photo_features --dry-run
  python -m scripts.broadcast_photo_features --send
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text as sa_text

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Broadcast, User
from app.database.repo.events import log_event

logger = structlog.get_logger(__name__)


# ─── Premium emoji IDs (from app/keyboards/main.py) ──────────────────────────
PE_LIGHTNING = '5258203794772085854'    # ⚡
PE_GIFT = '5440502878491930005'         # 🎁
PE_ROCKET = '5368654333397199941'       # 🚀  (for 💎/quality)
PE_ROBOT = '6147670072706995009'        # 🤖  (face swap)
PE_CARD = '5213403875670765022'         # 💳
PE_SPARKLES = '5325547803936572038'     # ✨

BONUS_THRESHOLD = 100   # bonus for users with < this credits
BONUS_AMOUNT = 150
BROADCAST_FLAG_COL = 'broadcast_photo_2026_05'
SOURCE = 'neyra'
PROMO_CODE = 'NEW_PHOTO_05_2026'


def _emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _render_text(credits: int, get_bonus: bool) -> str:
    """Render personalized message text."""
    bonus_line = (
        f'{_emoji(PE_GIFT, "🎁")} <b>+{BONUS_AMOUNT} кр от меня</b> — хватит на 1 face swap или 3 быстрых правки\n'
        if get_bonus else ''
    )
    return (
        f'🪞 <b>Покажу, как ты выглядишь...</b>\n\n'
        f'...как герой аниме\n'
        f'...в эпохе Ренессанса\n'
        f'...на обложке Time\n'
        f'...в костюме викинга\n'
        f'...на Татоине из Star Wars\n\n'
        f'Все варианты — реальны. Нужно только селфи.\n\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'{_emoji(PE_ROBOT, "🎭")} <b>Подставить лицо — 100 кр</b>\n'
        f'твоё лицо в любую сцену, любая эпоха, любой стиль\n\n'
        f'{_emoji(PE_LIGHTNING, "⚡")} <b>Поменять стиль или фон — 40 кр</b>\n'
        f'аниме · акварель · 3D · киберпанк · масло\n\n'
        f'{_emoji(PE_ROCKET, "💎")} <b>Сменить одежду или довести до HD — 100 кр</b>\n'
        f'с сохранением твоего лица и фигуры\n\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'{_emoji(PE_CARD, "💳")} Баланс: <b>{credits + (BONUS_AMOUNT if get_bonus else 0):,} кр</b>\n'
        f'{bonus_line}\n'
        f'Пришли любое селфи 👇'
    )


def _build_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(text='Подставить лицо', callback_data='face_swap_start', icon_custom_emoji_id=PE_ROBOT, style='primary')],
        [InlineKeyboardButton.model_construct(text='Изменить фото', callback_data='edit_start', icon_custom_emoji_id=PE_SPARKLES)],
    ])


async def _fetch_recipients() -> list[tuple[int, int, int, bool]]:
    """Returns list of (user_id, telegram_id, credits, is_eligible_for_bonus)."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text(f"""
            SELECT id, telegram_id, credits,
                   COALESCE({BROADCAST_FLAG_COL}, false) AS already_bonused
            FROM users
            WHERE is_banned = false
            ORDER BY id
        """))).all()
    out = []
    for uid, tg_id, credits, already in rows:
        get_bonus = (credits < BONUS_THRESHOLD) and (not already)
        out.append((uid, tg_id, credits, get_bonus))
    return out


async def _grant_bonus(user_id: int) -> bool:
    """Atomically grant bonus. Returns True if granted, False if already had it."""
    async with AsyncSessionFactory() as session:
        # Atomic: update only if flag is false; then increment credits in same tx
        result = await session.execute(sa_text(f"""
            UPDATE users
            SET credits = credits + :bonus,
                {BROADCAST_FLAG_COL} = true
            WHERE id = :uid AND COALESCE({BROADCAST_FLAG_COL}, false) = false
            RETURNING id
        """), {'uid': user_id, 'bonus': BONUS_AMOUNT})
        granted = result.first() is not None
        await session.commit()
        if granted:
            try:
                await log_event(user_id=user_id, event_type='broadcast_grant',
                                model_key=None, credits_spent=-BONUS_AMOUNT,
                                source=SOURCE, code=PROMO_CODE)
            except Exception as exc:
                logger.warning('event log failed', error=str(exc)[:200])
        return granted


async def _create_broadcast_row(total: int) -> int:
    async with AsyncSessionFactory() as session:
        b = Broadcast(
            source=SOURCE,
            promo_code=PROMO_CODE,
            total_recipients=total,
            started_at=datetime.now(timezone.utc),
            status='running',
        )
        session.add(b)
        await session.commit()
        return b.id


async def _finalize_broadcast(broadcast_id: int, sent: int, blocked: int,
                              errors: int, status: str) -> None:
    async with AsyncSessionFactory() as session:
        b = await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
        if not b:
            return
        b.sent_count = sent
        b.blocked_count = blocked
        b.error_count = errors
        b.completed_at = datetime.now(timezone.utc)
        if b.started_at:
            started = b.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            b.duration_seconds = int((b.completed_at - started).total_seconds())
        b.status = status
        await session.commit()


async def _send_one(bot: Bot, tg_id: int, credits: int, get_bonus: bool) -> str:
    """Send to one user. Returns 'sent' | 'blocked' | 'error'."""
    try:
        await bot.send_message(
            tg_id,
            _render_text(credits, get_bonus),
            parse_mode='HTML',
            reply_markup=_build_kb(),
            disable_web_page_preview=True,
        )
        return 'sent'
    except Exception as exc:
        err = str(exc).lower()
        if 'forbidden' in err or 'blocked' in err or 'deactivated' in err or 'user is deactivated' in err:
            return 'blocked'
        logger.warning('send failed', tg_id=tg_id, error=str(exc)[:200])
        return 'error'


async def run(dry_run: bool) -> None:
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        admin_ids = settings.admin_ids_list
        recipients = await _fetch_recipients()

        total = len(recipients)
        bonus_eligible = sum(1 for _, _, _, b in recipients if b)
        bonus_total_cost = bonus_eligible * BONUS_AMOUNT

        print(f'\n=== BROADCAST PLAN ===')
        print(f'Mode:                {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients (active): {total}')
        print(f'Bonus-eligible:      {bonus_eligible} (<{BONUS_THRESHOLD} кр)')
        print(f'Bonus total cost:    {bonus_total_cost} кр')
        print(f'Bonus per user:      {BONUS_AMOUNT} кр')
        print(f'====================\n')

        if dry_run:
            # Send sample to each admin only
            print(f'Sending sample to admins: {admin_ids}')
            for aid in admin_ids:
                # Show both variants (no-bonus and with-bonus) so admin can review
                await bot.send_message(aid, '<i>📋 DRY-RUN — Вариант БЕЗ бонуса:</i>',
                                       parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid, credits=520, get_bonus=False)
                await asyncio.sleep(0.5)
                await bot.send_message(aid, '<i>📋 DRY-RUN — Вариант С бонусом (для тех у кого &lt;100 кр):</i>',
                                       parse_mode='HTML')
                await asyncio.sleep(0.2)
                await _send_one(bot, aid, credits=5, get_bonus=True)
            print(f'✓ Dry-run complete. Check Telegram for samples.')
            return

        # REAL SEND
        broadcast_id = await _create_broadcast_row(total)
        print(f'Started broadcast #{broadcast_id}')

        sent = blocked = errors = granted = 0
        for uid, tg_id, credits, get_bonus in recipients:
            if get_bonus:
                ok = await _grant_bonus(uid)
                if ok:
                    granted += 1
                    credits = credits + BONUS_AMOUNT  # show new balance in message

            status = await _send_one(bot, tg_id, credits, get_bonus)
            if status == 'sent':
                sent += 1
            elif status == 'blocked':
                blocked += 1
            else:
                errors += 1
            await asyncio.sleep(0.05)  # ~20 msg/sec — within TG limit

        await _finalize_broadcast(broadcast_id, sent, blocked, errors, 'completed')

        print(f'\n=== BROADCAST DONE ===')
        print(f'Sent:    {sent}')
        print(f'Blocked: {blocked}')
        print(f'Errors:  {errors}')
        print(f'Bonus granted: {granted}')
        print(f'======================\n')
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='Sample to admins only')
    g.add_argument('--send', action='store_true', help='Send to all eligible users')
    args = parser.parse_args()

    asyncio.run(run(dry_run=args.dry_run))


if __name__ == '__main__':
    main()

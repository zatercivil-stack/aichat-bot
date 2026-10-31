"""Анонс новой фичи «Сцены с точным лицом» — медиагруппа + текст с кнопкой.

Использование:
    # Тест: один юзер grelok (telegram_id из bot_config.test_broadcast_username)
    docker compose exec bot python -m scripts.announce_scenes --test

    # Реальная рассылка по активным юзерам без бана
    docker compose exec bot python -m scripts.announce_scenes --audience active

Фото берёт из data/launch_scenes/ (имена: 01_ballet.jpg, 02_warrior.jpg, 03_tennis.jpg).
При отсутствии — падает с понятной ошибкой. Watermark не накладывает — это уже фото
с водяным знаком "Нейра · t.me/NeyraChatBot" (как присланы пользователем).
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC
from pathlib import Path

import structlog
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from sqlalchemy import select

from app.bot import create_bot, get_bot
from app.config import settings  # noqa: F401  (touch import to confirm app loads)
from app.database.base import AsyncSessionFactory
from app.database.models import User
from app.database.repo.bot_config import get_config_value

logger = structlog.get_logger(__name__)

PHOTOS_DIR = Path(__file__).parent.parent / 'data' / 'launch_scenes'
# Берём любые .jpg/.jpeg/.png/.webp из папки, сортируем по имени.
# Порядок имеет значение — первая в медиагруппе показывается крупнее.
SUPPORTED_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}

CAPTION = (
    '✨ <b>Одно селфи → ты на обложке</b>\n\n'
    'Лицо в кадре — <b>точно твоё</b>. Без «получилась чужая тётка».\n\n'
    'Готовые сцены:\n'
    '🩰 Балет у античного льва\n'
    '🛡 Валькирия в доспехах\n'
    '🎾 Теннис 70-х\n'
    '🌹 Полночь в Париже в неоне\n'
    '🎩 Леди в Bentley\n'
    '💋 Y2K у прилавков\n'
    '+ ещё 7 настроений внутри.\n\n'
    '🔥 До 19 мая — запуск-цена <b>200 кр</b> за сцену. Дальше дороже.\n\n'
    'Жми кнопку, пришли селфи — готово через минуту.'
)

CTA_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='📸 Попробовать сейчас', callback_data='edit_start')],
])


async def _load_photos() -> list[tuple[str, bytes]]:
    """Берёт все картинки из data/launch_scenes/, сортирует по имени, читает в память.
    Падает если папка пустая. Возвращает [(filename, bytes), ...]."""
    if not PHOTOS_DIR.exists():
        raise FileNotFoundError(f'Папка {PHOTOS_DIR} не существует')
    files = sorted(
        p for p in PHOTOS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )
    if not files:
        raise FileNotFoundError(f'В {PHOTOS_DIR} нет .jpg/.jpeg/.png/.webp')
    return [(p.name, p.read_bytes()) for p in files]


async def _resolve_audience(audience: str) -> list[int]:
    async with AsyncSessionFactory() as session:
        if audience == 'test':
            username = (await get_config_value(session, 'test_broadcast_username') or 'grelok').lstrip('@')
            user = await session.scalar(select(User).where(User.username.ilike(username)))
            if not user:
                logger.error('test user not found', username=username)
                return []
            return [user.telegram_id]
        elif audience == 'active':
            # Активные за последние 30 дней, не забанены
            from datetime import datetime, timedelta
            cutoff = datetime.now(UTC) - timedelta(days=30)
            rows = await session.execute(
                select(User.telegram_id)
                .where(not User.is_banned)
                .where(User.last_active_at >= cutoff)
            )
            return [r[0] for r in rows.all()]
        elif audience == 'all':
            rows = await session.execute(
                select(User.telegram_id).where(not User.is_banned)
            )
            return [r[0] for r in rows.all()]
        else:
            raise ValueError(f'Unknown audience: {audience}')


async def _send_to(bot, tg_id: int, photos: list[tuple[str, bytes]]) -> bool:
    """Отправляет одному юзеру: медиагруппа + follow-up текст с кнопкой.
    photos: [(filename, bytes), ...] — порядок задаётся сортировкой имён."""
    try:
        media = [
            InputMediaPhoto(media=BufferedInputFile(blob, filename=fname))
            for fname, blob in photos
        ]
        await bot.send_media_group(chat_id=tg_id, media=media)
        # Текст + кнопка отдельным сообщением (медиагруппы не поддерживают reply_markup)
        await bot.send_message(
            tg_id, CAPTION, parse_mode='HTML',
            reply_markup=CTA_KB, disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if 'blocked' in msg or 'deactivated' in msg or 'chat not found' in msg:
            logger.info('skip user (blocked/deleted)', tg_id=tg_id)
        else:
            logger.warning('send failed', tg_id=tg_id, error=str(exc)[:200])
        return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audience', choices=('test', 'active', 'all'), default='test')
    parser.add_argument('--test', action='store_true', help='shortcut for --audience test')
    parser.add_argument('--dry-run', action='store_true', help='print recipients, do not send')
    args = parser.parse_args()
    if args.test:
        args.audience = 'test'

    # Сначала аудиторию — чтобы --dry-run работал даже когда фото ещё не положены
    tg_ids = await _resolve_audience(args.audience)
    logger.info('audience resolved', audience=args.audience, count=len(tg_ids))

    if args.dry_run:
        for tid in tg_ids[:10]:
            print(tid)
        print(f'... total {len(tg_ids)} recipients')
        return

    photos = await _load_photos()
    logger.info('photos loaded', count=len(photos),
                sizes_kb=[len(blob) // 1024 for _, blob in photos],
                names=[fname for fname, _ in photos])

    bot = get_bot() or create_bot()

    sent = failed = 0
    for tg_id in tg_ids:
        ok = await _send_to(bot, tg_id, photos)
        if ok:
            sent += 1
        else:
            failed += 1
        # 30 msgs/sec safe rate (TG default: 30/sec total, 1/sec per chat)
        await asyncio.sleep(0.05)

    print(f'\n✓ Sent: {sent} · ✗ Failed: {failed} · Total: {len(tg_ids)}')


if __name__ == '__main__':
    asyncio.run(main())

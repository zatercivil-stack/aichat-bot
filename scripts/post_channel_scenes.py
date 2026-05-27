"""Одноразовый пост в @conturD про Нейру.

Структура:
    [1 фото-коллаж 3 кадров] + caption с текстом + 1 inline-кнопка
    Кнопка ведёт на deep-link t.me/NeyraChatBot?start=promo_SCENE200,
    который автоматически зачисляет +200 кр на счёт юзеру при первом /start.

Режимы:
    docker compose exec bot python -m scripts.post_channel_scenes --test
        → личка админу (id из ADMIN_IDS)
    docker compose exec bot python -m scripts.post_channel_scenes --publish
        → @conturD (боевой режим, ставится в cron на 15.05 12:00 МСК)
"""
from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

import structlog
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
)
from PIL import Image

from app.bot import create_bot, get_bot
from app.config import settings

logger = structlog.get_logger(__name__)

CHANNEL_ID = '@conturD'
PROMO_CODE = 'SCENE200'
DEEP_LINK = f'https://t.me/NeyraChatBot?start=promo_{PROMO_CODE}'
PHOTOS_DIR = Path('/app/data/launch_scenes')


# Caption на photo имеет лимит 1024 символа. Текущая длина ≈ 700 — fits.
# Стиль: разговорный, простой, без жаргона. Сначала объясняем что такое Нейра
# для тех, кто из канала видит её впервые.
POST_TEXT = (
    'Если вдруг непонятно, почему у знакомых в ленте начались фотосессии — '
    'это <b>Нейра</b>. Бот в Telegram, который превращает обычное селфи в '
    'кадр в любой сцене: балет у античной статуи, теннис 70-х, вечер в '
    'Bentley, ночь в Париже. Лицо в кадре <b>твоё</b>, не чужое.\n\n'
    'Загружаешь селфи → выбираешь настроение → через минуту готово. Без '
    'фотографа, без студии, без фотошопа.\n\n'
    'Для подписчиков канала — <b>200 кредитов</b> на счёт = одна сцена '
    'бесплатно. До 19 мая.\n\n'
    f'Промокод: <tg-spoiler>{PROMO_CODE}</tg-spoiler>\n'
    'Жми кнопку — кредиты начислятся автоматически.'
)

CTA_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='Забрать 200 кр и попробовать', url=DEEP_LINK)],
])


def _load_photo_blobs() -> list[bytes]:
    if not PHOTOS_DIR.exists():
        raise FileNotFoundError(f'Папка {PHOTOS_DIR} не существует')
    files = sorted(
        p for p in PHOTOS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
    )
    if not files:
        raise FileNotFoundError(f'В {PHOTOS_DIR} нет фото')
    return [p.read_bytes() for p in files]


def _build_collage(blobs: list[bytes], target_h: int = 1080) -> bytes:
    """Склеивает фото горизонтально в одно изображение с тонкими чёрными
    разделителями (2px). Высота фиксирована, ширина = сумма пропорций.
    Возвращает JPEG-байты."""
    imgs = [Image.open(io.BytesIO(b)).convert('RGB') for b in blobs]
    resized: list[Image.Image] = []
    for img in imgs:
        ratio = target_h / img.height
        new_w = max(1, int(img.width * ratio))
        resized.append(img.resize((new_w, target_h), Image.LANCZOS))

    gap = 2  # пикселей между фото
    total_w = sum(img.width for img in resized) + gap * (len(resized) - 1)
    canvas = Image.new('RGB', (total_w, target_h), (0, 0, 0))
    x = 0
    for img in resized:
        canvas.paste(img, (x, 0))
        x += img.width + gap

    out = io.BytesIO()
    canvas.save(out, 'JPEG', quality=88, optimize=True)
    return out.getvalue()


async def _send_post(chat_id: int | str) -> None:
    bot = get_bot() or create_bot()
    blobs = _load_photo_blobs()
    collage = _build_collage(blobs)
    logger.info('collage built',
                input_count=len(blobs), output_kb=len(collage) // 1024)

    photo_file = BufferedInputFile(collage, filename='neyra_scenes.jpg')
    await bot.send_photo(
        chat_id=chat_id, photo=photo_file,
        caption=POST_TEXT, parse_mode='HTML',
        reply_markup=CTA_KB,
    )


async def main():
    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument('--test', action='store_true',
                     help='отправить в личку первому ADMIN_IDS (для проверки)')
    grp.add_argument('--publish', action='store_true',
                     help='опубликовать в @conturD (боевой режим)')
    args = parser.parse_args()

    if args.test:
        admin_ids = settings.admin_ids_list
        if not admin_ids:
            print('ADMIN_IDS не настроен в .env')
            return
        target = admin_ids[0]
        logger.info('test send', target=target)
        await _send_post(target)
        print(f'✓ Тестовый пост отправлен в личку: {target}')
    else:
        logger.info('channel publish', channel=CHANNEL_ID)
        await _send_post(CHANNEL_ID)
        print(f'✓ Пост опубликован в {CHANNEL_ID}')


if __name__ == '__main__':
    asyncio.run(main())

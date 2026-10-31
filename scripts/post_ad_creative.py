"""Универсальный отправщик рекламного креатива в TG.

Поддерживает 3 формата:
  • --photo    → коллаж 3 фото + caption + кнопка (текущий вариант)
  • --video    → MP4 8s + caption + кнопка (РЕКОМЕНДУЕМЫЙ для посевов)
  • --round    → кружок (video_note) — потом ОТДЕЛЬНО шлём caption+кнопку

Креативы (--creative):
  fomo, wow, dare, saga, curi — каждый со своей копией и привязанным промокодом.

Цели:
  --test                  → отправить в личку ADMIN_IDS[0]
  --target @somechannel   → опубликовать в указанный канал (только бот должен быть admin)
  --target -1001234567    → можно числовой chat_id

Использование:
    docker compose exec bot python -m scripts.post_ad_creative \\
        --creative wow --video --test
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.bot import create_bot, get_bot
from app.config import settings

logger = structlog.get_logger(__name__)

PHOTOS_DIR = Path('/app/data/launch_scenes')

CREATIVES: dict[str, dict] = {
    'fomo': {
        'promo': 'SCENE_FOMO',
        # FOMO = «подружки уже делают» — премиум-аспирация: Bentley + Париж + Porsche
        'photos': ['demo_vintage_car.jpg', 'demo_paris_night.jpg', 'demo_v2_fisheye_porsche.jpg'],
        'text': (
            'Если ты ещё не видела это в чужих сторис — посмотри.\n\n'
            'Подружки массово начали выкладывать фотосессии: '
            'балет у античного льва, Bentley, Париж в неоне. Без студии, '
            'без фотографа.\n\n'
            'Это бот в Telegram. <b>Нейра</b>. Закидываешь селфи, выбираешь '
            'настроение — через минуту твоё лицо <b>1-в-1</b> в любой сцене.\n\n'
            '<b>250 кредитов в подарок подписчикам канала</b> = одна сцена '
            'бесплатно.'
        ),
        'cta': 'Забрать 250 кр →',
    },
    'wow': {
        'promo': 'SCENE_WOW',
        # WOW = «это не модели» — variety transformation (балет + воин + теннис)
        'photos': ['photo.jpg', 'photo2.jpg', 'photo3.jpg'],
        'text': (
            'На этом фото — <b>НЕ модели</b>. Это селфи трёх обычных девушек, '
            'прогнанные через бота.\n\n'
            '🔍 Никакой студии. Никакого фотографа. Никакого ретуша. '
            'Только AI, который сохранил лицо <b>1-в-1</b> и подставил его в '
            'любой кадр — балет у античной статуи, корт 70-х, Bentley, Париж.\n\n'
            'Тебе бы тоже хотелось такую фотосессию?\n\n'
            '🎁 <b>250 кредитов в подарок подписчикам</b> — хватит на пробу.'
        ),
        'cta': 'Сделать своё фото →',
    },
    'dare': {
        'promo': 'SCENE_DARE',
        # DARE = молодёжный challenge — берём bold-V2 серию (Y2K-фридж + macro + boxing-red)
        'photos': ['demo_v2_y2k_fridge.jpg', 'demo_v2_macro_watermelon.jpg', 'demo_v2_bold_red_studio.jpg'],
        'text': (
            'Закинула селфи в бота, получила обратно ВОТ ЭТО ↑\n\n'
            'Балет у льва. Y2K-кадр с фишай-объективом. Гулливер в Нью-Йорке. '
            'Полночь в Париже в неоне.\n\n'
            'Лицо в кадре — <b>твоё, не чужое</b>. '
            'Лучшие подружки уже спрашивают где ты так снималась.\n\n'
            'Подписчикам канала — <b>250 кр в подарок</b>. '
            'Хватит на одну сцену = 0 рублей.'
        ),
        'cta': 'Пробую →',
    },
    'saga': {
        'promo': 'SCENE_SAGA',
        # SAGA = mini-story «Париж/Galliano» → один cinematic кадр Парижа
        'photos': ['demo_paris_night.jpg'],
        'text': (
            'Я никогда не была в Париже.\n\n'
            'Но вчера выложила фото с улицы Сен-Жермен в неоне, в платье '
            'от Galliano, и три подруги написали: «а ты когда вернулась?»\n\n'
            'Это AI. Бот в Telegram — <b>Нейра</b>. Берёт твоё селфи и ставит '
            'тебя в любой кадр: Bentley, балет, теннис 70-х. '
            'Лицо настоящее, не похожее.\n\n'
            'Цена одной сцены — 200 кредитов. '
            'Подписчикам канала — <b>первые 250 кр в подарок</b>.'
        ),
        'cta': 'Получить 250 кр →',
    },
    'curi': {
        'promo': 'SCENE_CURI',
        # CURI = curiosity + spoiler — один мощный кадр-обложка журнала
        'photos': ['demo_v2_magazine_cover.jpg'],
        'text': (
            'Это не фото с реальной съёмки. '
            'Это AI взял её селфи и поставил в этот кадр.\n\n'
            'Лицо — точно её. Сцена — придумана за минуту.\n\n'
            'В <b>Нейре</b> большая коллекция готовых сцен: Балет, Валькирия, '
            'Корт 70-х, Bentley, Y2K, Париж и другие.\n\n'
            '🎁 Промокод для канала: <tg-spoiler>SCENE_CURI</tg-spoiler> '
            '= 250 кр на счёт.'
        ),
        'cta': 'Открыть бота →',
    },
}


def _build_kb(promo_code: str, cta_text: str) -> InlineKeyboardMarkup:
    deep_link = f'https://t.me/NeyraChatBot?start=promo_{promo_code}'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cta_text, url=deep_link)],
    ])


def _load_photo_blobs(filenames: list[str] | None = None) -> list[tuple[str, bytes]]:
    """Если filenames переданы — читает ИХ (в порядке списка).
    Иначе — все .jpg/.png/.webp из папки (для обратной совместимости)."""
    if filenames:
        results = []
        for fname in filenames:
            p = PHOTOS_DIR / fname
            if not p.exists():
                raise FileNotFoundError(f'Не найдено: {p}')
            results.append((p.name, p.read_bytes()))
        return results
    files = sorted(
        p for p in PHOTOS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
        and not p.name.startswith('ad_')
    )
    return [(p.name, p.read_bytes()) for p in files]


def _build_collage(blobs: list[bytes], cell_size: int = 900) -> bytes:
    """Склеивает фото в горизонтальный коллаж с одинаковыми КВАДРАТНЫМИ ячейками.
    Раньше при разных aspect-ratio исходников фото клеились криво.
    Сейчас: каждое фото cover-fit'ится в square cell_size×cell_size (center crop),
    потом коллаж = N квадратов в ряд. Чистые границы, единая высота."""
    import io

    from PIL import Image

    def _cover_fit(img: Image.Image, size: int) -> Image.Image:
        # Cover-fit: масштабируем чтобы покрыть size×size, лишнее обрезаем по центру
        src_ratio = img.width / img.height
        if src_ratio > 1.0:
            new_h = size
            new_w = int(size * src_ratio)
        else:
            new_w = size
            new_h = int(size / src_ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - size) // 2
        top = (new_h - size) // 2
        return img.crop((left, top, left + size, top + size))

    cells = [_cover_fit(Image.open(io.BytesIO(b)).convert('RGB'), cell_size) for b in blobs]
    gap = 4  # тонкая чёрная разделительная полоска
    total_w = cell_size * len(cells) + gap * (len(cells) - 1)
    canvas = Image.new('RGB', (total_w, cell_size), (0, 0, 0))
    x = 0
    for c in cells:
        canvas.paste(c, (x, 0))
        x += cell_size + gap
    out = io.BytesIO()
    canvas.save(out, 'JPEG', quality=88, optimize=True)
    return out.getvalue()


async def _send(bot, chat_id, media_kind: str, creative: dict) -> None:
    text = creative['text']
    kb = _build_kb(creative['promo'], creative['cta'])

    if media_kind == 'photo':
        # Берём конкретный набор фото под креатив (если задан в CREATIVES.photos),
        # иначе используем дефолтные 3 launch_scenes фото
        wanted = creative.get('photos')
        blobs = _load_photo_blobs(wanted)
        photos = [b for _, b in blobs]
        if len(photos) == 1:
            # Один кадр — отправляем как есть, без коллажа (хороший hero shot)
            await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(photos[0], filename='neyra_scene.jpg'),
                caption=text, parse_mode='HTML', reply_markup=kb,
            )
        else:
            # 2-3 кадра — клеим в горизонтальный коллаж
            collage = _build_collage(photos)
            await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(collage, filename='neyra_scenes.jpg'),
                caption=text, parse_mode='HTML', reply_markup=kb,
            )
    elif media_kind == 'video':
        mp4 = PHOTOS_DIR / 'ad_preview.mp4'
        if not mp4.exists():
            raise FileNotFoundError(
                f'{mp4} нет — сначала запусти `scripts.build_ad_visuals --video`'
            )
        await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(str(mp4), filename='neyra_scenes.mp4'),
            caption=text, parse_mode='HTML', reply_markup=kb,
            supports_streaming=True,
        )
    elif media_kind == 'round':
        # video_note не поддерживает caption/buttons — шлём отдельным шагом
        mp4 = PHOTOS_DIR / 'ad_round.mp4'
        if not mp4.exists():
            raise FileNotFoundError(
                f'{mp4} нет — сначала `scripts.build_ad_visuals --round`'
            )
        await bot.send_video_note(
            chat_id=chat_id,
            video_note=FSInputFile(str(mp4), filename='neyra_round.mp4'),
        )
        # Потом текст + кнопка отдельно
        await bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode='HTML', reply_markup=kb,
            disable_web_page_preview=True,
        )
    else:
        raise ValueError(f'Неизвестный формат: {media_kind}')


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--creative', required=True, choices=tuple(CREATIVES.keys()))
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument('--photo', action='store_true', help='коллаж 3 фото')
    grp.add_argument('--video', action='store_true', help='MP4 видео (РЕКОМЕНДУЮ)')
    grp.add_argument('--round', action='store_true', help='кружок + текст-фоллоу')
    tgt = parser.add_mutually_exclusive_group(required=True)
    tgt.add_argument('--test', action='store_true', help='в личку ADMIN_IDS[0]')
    tgt.add_argument('--target', help='@channel или числовой chat_id')
    args = parser.parse_args()

    media_kind = 'photo' if args.photo else 'video' if args.video else 'round'
    creative = CREATIVES[args.creative]

    bot = get_bot() or create_bot()

    if args.test:
        chat_id = settings.admin_ids_list[0]
    else:
        chat_id = args.target
        if chat_id.lstrip('-').isdigit():
            chat_id = int(chat_id)

    logger.info('ad send', creative=args.creative, kind=media_kind, chat_id=chat_id)
    await _send(bot, chat_id, media_kind, creative)
    print(f'✓ Креатив "{args.creative}" ({media_kind}) отправлен → {chat_id}')


if __name__ == '__main__':
    asyncio.run(main())

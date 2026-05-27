"""Создаёт preview-изображения для каждого пресета и кеширует TG file_id в БД.

Логика:
  1. Для пресетов где есть готовая картинка на диске (data/launch_scenes/*) —
     используем её. Маппинг см. EXISTING_DEMO_FILES.
  2. Для остальных — генерируем через fal.ai text-to-image (~$0.05 каждая).
  3. Загружаем каждое в TG (send_photo в админский чат), читаем file_id из ответа.
  4. Upsert в таблицу preset_previews(preset_key, file_id).

Запуск:
    docker compose exec bot python -m scripts.generate_preset_previews

Идемпотентный — повторный запуск пропускает уже сохранённые в БД пресеты
(удалить запись из preset_previews если хочешь регенерить).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from aiogram.types import BufferedInputFile, FSInputFile

from app.bot import create_bot, get_bot
from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.preset_previews import (
    get_preview_file_id, save_preview,
)
from app.handlers.image_edit import EDIT_PRESETS
from app.services.fal_image_service import generate_image, is_enabled

logger = structlog.get_logger(__name__)

PHOTOS_DIR = Path('/app/data/launch_scenes')

# Маппинг preset_key → имя файла в launch_scenes/ (если есть готовое демо)
EXISTING_DEMO_FILES: dict[str, str] = {
    # ── Из оригинальных юзерских фото ──
    # photo.jpg → теннис; photo2.jpg → валькирия; photo3.jpg → балет (фактический контент)
    'lion_steps':       'photo3.jpg',
    'warrior':          'photo2.jpg',
    'tennis_70s':       'photo.jpg',
    # ── Из demo_*.jpg (1-я партия generation) ──
    'paris_night':      'demo_paris_night.jpg',
    'vintage_car':      'demo_vintage_car.jpg',
    'y2k_attitude':     'demo_y2k.jpg',
    'giant_city':       'demo_giant_city.jpg',
    'subway_bouquet':   'demo_metro_bouquet.jpg',
    # ── Из demo_v2_*.jpg (2-я партия — Y2K/Bold серия) ──
    'fisheye_porsche':  'demo_v2_fisheye_porsche.jpg',
    'y2k_fridge':       'demo_v2_y2k_fridge.jpg',
    'macro_watermelon': 'demo_v2_macro_watermelon.jpg',
    'night_racing':     'demo_v2_night_racing.jpg',
    'bold_red_studio':  'demo_v2_bold_red_studio.jpg',
    'magazine_vibe':    'demo_v2_magazine_cover.jpg',
}

# Text-to-image промпты для пресетов без готового demo. Описываем «модель в этой
# сцене» — идеально для preview-каталога.
TEXT2IMG_PROMPTS: dict[str, tuple[str, str]] = {
    # (size, prompt) — image_size из fal.ai (portrait_4_3 default)
    'cinema': ('portrait_4_3',
        'Dark cinematic editorial close-up portrait using chiaroscuro lighting. A sharp '
        'diagonal beam of natural light cuts through deep blackness, illuminating only '
        'the eyes, cheekbones and glossy lips of a young woman while the rest of her '
        'face fades into rich black shadow. Hair partially covers one eye, chin '
        'slightly raised, piercing gaze upward. Background completely absorbed by deep '
        'darkness. 8k, ultrarealistic, 85mm lens at f/1.8, magazine cover aesthetic.'),
    'vogue': ('portrait_4_3',
        'High-fashion Vogue cover portrait. A young woman in luxurious haute couture '
        'clothing, flawless studio makeup, controlled lighting with a soft beauty dish. '
        'Pristine gradient background (soft cream). Sharp focus on face, magazine-'
        'quality retouching. Confident pose, slight chin elevation, direct gaze into '
        'camera. 8k, 85mm portrait lens, fashion editorial mood.'),
    'anime': ('portrait_4_3',
        'Hand-drawn anime portrait in the style of Studio Ghibli and modern Japanese '
        'animation. A young woman with soft watercolor textures, warm golden hour '
        'lighting, expressive large detailed eyes, delicate clean line work. Dreamy '
        'painterly background with gentle bokeh, subtle wind in hair. Studio Ghibli '
        'colour palette (warm honey + soft sky blue), Mamoru Hosoda film-still '
        'quality.'),
    'cyberpunk': ('portrait_4_3',
        'Cyberpunk neon-lit night portrait. A young woman bathed in vibrant pink and '
        'cyan neon glow from holographic city signs in the rain-slicked background. '
        'Wet asphalt reflections, atmospheric mist, futuristic edgy mood reminiscent '
        'of Blade Runner 2049. Sharp focus on face with dramatic neon rim lighting '
        'outlining cheekbones and hair. Subtle cyberpunk accessories. 8k, anamorphic '
        'lens flares, vibrant teal-magenta colour grading.'),
    'golden_hour': ('portrait_4_3',
        'Romantic golden hour outdoor portrait. A young woman backlit by warm setting '
        'sun, hair catching the light and glowing, soft natural lens flare. Warm '
        'amber and orange tones throughout, dreamy creamy bokeh of a wildflower '
        'meadow or wheat field in the background. 85mm portrait lens at f/1.4, '
        'magazine-quality natural light photography.'),
    'retro_90s': ('portrait_4_3',
        'Authentic 90s film photography portrait. Real 35mm film grain, warm faded '
        'colours, slight overexposure with light leaks. A young woman in casual '
        'candid pose with retro styling (denim, layered chains, slip dress). '
        'Background suggests vintage interior. Feels nostalgic, raw, unretouched. '
        'Kodak Portra 400 film emulation, on-camera flash.'),
    'rooftop_wine': ('portrait_4_3',
        'Full-body candid night photo on a rooftop terrace. A young woman sitting '
        'relaxed on a small wooden bench, leaning forward with elbows on knees, head '
        'tilted down toward a wine glass in her hand. Cream off-white wool overshirt '
        'jacket over a plain white tee, loose black trousers, white sneakers. On the '
        'bench beside her a bottle of wine and a second glass. Nighttime city '
        'rooftop with warm string lights, cinematic mood, photorealistic.'),
    'rainy_taxi': ('portrait_4_3',
        'Cinematic neon-noir night portrait inside a car. A young person in the back '
        'seat looking pensively out the rain-streaked side window. Dramatic cyberpunk '
        'neon glow from city signs outside — warm red on one side of the face, cool '
        'cyan on the other. Rain droplets create bokeh light flares. Blade Runner '
        'aesthetic, 35mm grain, photorealistic.'),
    'vintage_rain': ('portrait_4_3',
        'Vintage cinematic portrait inside a classic retro red car. A young woman '
        'seated in the back, head softly resting against the deep red leather '
        'upholstery, gazing out the rain-streaked window with calm pensive '
        'expression. Soft moody daylight, red leather glow on skin. Vintage 35mm '
        'film aesthetic, warm muted tones, melancholy mood, photorealistic.'),
    'berlin_metro': ('portrait_4_3',
        'Editorial street-style portrait at an underground subway station. A young '
        'woman standing on the platform, body angled toward camera, confident calm '
        'gaze. A bright yellow subway train passes in strong horizontal motion blur '
        'behind, filling the background with warm golden movement. Long oversized '
        'black wool coat. Tiled subway station with arched ceiling. 35mm photo '
        'aesthetic, sharp focus on subject, motion blur only on train.'),
    'fisheye_paris': ('portrait_4_3',
        'Surreal black-and-white fisheye street photo on a Paris intersection. A '
        'young woman standing on a cobblestone crosswalk under an extreme fisheye '
        'lens — wide barrel distortion making the Haussmannian buildings curve '
        'dramatically around the edges of the frame. Documentary street-photography '
        'aesthetic, fisheye 8mm lens look, grainy black-and-white film, candid '
        'editorial mood, photorealistic.'),
}


async def _upload_to_tg_get_file_id(bot, admin_chat_id: int,
                                     file_bytes: bytes | None = None,
                                     file_path: Path | None = None,
                                     caption: str = '') -> str:
    """Шлёт фото в админский чат, возвращает file_id из ответа TG.
    Поддерживает либо bytes (для свежесгенерированных), либо path (для существующих)."""
    if file_bytes is not None:
        photo = BufferedInputFile(file_bytes, filename='preview.jpg')
    elif file_path is not None:
        photo = FSInputFile(str(file_path), filename=file_path.name)
    else:
        raise ValueError('Need either file_bytes or file_path')

    msg = await bot.send_photo(chat_id=admin_chat_id, photo=photo, caption=caption)
    # photo — массив (разные размеры). Берём самый большой (последний).
    return msg.photo[-1].file_id


async def main():
    if not is_enabled():
        print('fal.ai не настроен — не сможем генерить недостающие demo')

    admin_chat_id = settings.admin_ids_list[0] if settings.admin_ids_list else None
    if not admin_chat_id:
        print('ADMIN_IDS не настроен — некому слать фото для file_id')
        return

    bot = get_bot() or create_bot()

    all_preset_keys = list(EDIT_PRESETS.keys())
    # Утилиты (change_clothes/bg, remove_bg) — без preview, юзер вводит свой запрос
    skip_keys = {'change_clothes', 'change_bg', 'remove_bg'}
    keys_to_process = [k for k in all_preset_keys if k not in skip_keys]

    print(f'Будет обработано {len(keys_to_process)} пресетов')
    print(f'  из них с готовыми файлами: {sum(1 for k in keys_to_process if k in EXISTING_DEMO_FILES)}')
    print(f'  для генерации через fal.ai: {sum(1 for k in keys_to_process if k not in EXISTING_DEMO_FILES and k in TEXT2IMG_PROMPTS)}')
    print(f'  без источника (пропустим): {sum(1 for k in keys_to_process if k not in EXISTING_DEMO_FILES and k not in TEXT2IMG_PROMPTS)}')
    print()

    saved = skipped = failed = 0
    for key in keys_to_process:
        label = EDIT_PRESETS[key][0]
        # Уже есть в БД? — пропускаем (идемпотентность)
        async with AsyncSessionFactory() as session:
            existing = await get_preview_file_id(session, key)
        if existing:
            print(f'⏭ {key:20s} {label:30s} — уже в БД, пропускаю')
            skipped += 1
            continue

        # 1. Сначала пробуем готовый файл
        if key in EXISTING_DEMO_FILES:
            src = PHOTOS_DIR / EXISTING_DEMO_FILES[key]
            if src.exists():
                try:
                    print(f'📸 {key:20s} {label:30s} — из {src.name}')
                    file_id = await _upload_to_tg_get_file_id(
                        bot, admin_chat_id, file_path=src,
                        caption=f'[preview] {label}',
                    )
                    async with AsyncSessionFactory() as session:
                        await save_preview(session, key, file_id, source_file=src.name)
                        await session.commit()
                    saved += 1
                    await asyncio.sleep(0.4)  # rate-limit
                    continue
                except Exception as exc:
                    print(f'   ✗ ошибка upload: {str(exc)[:120]}')

        # 2. Если нет файла — генерим через fal.ai
        if key in TEXT2IMG_PROMPTS:
            size, prompt = TEXT2IMG_PROMPTS[key]
            print(f'🎨 {key:20s} {label:30s} — генерю через fal.ai ({size})')
            try:
                img_bytes = await generate_image(prompt, image_size=size)
                if not img_bytes:
                    print(f'   ✗ fal.ai вернул пусто')
                    failed += 1
                    continue
                file_id = await _upload_to_tg_get_file_id(
                    bot, admin_chat_id, file_bytes=img_bytes,
                    caption=f'[preview generated] {label}',
                )
                async with AsyncSessionFactory() as session:
                    await save_preview(session, key, file_id, source_file=f'fal:{key}')
                    await session.commit()
                saved += 1
                await asyncio.sleep(0.4)
                continue
            except Exception as exc:
                print(f'   ✗ ошибка генерации: {str(exc)[:120]}')
                failed += 1
                continue

        print(f'⚠ {key:20s} — нет файла и нет промпта, пропускаю')

    print()
    print(f'=== Итого: сохранено {saved}, пропущено (уже было) {skipped}, ошибок {failed} ===')


if __name__ == '__main__':
    asyncio.run(main())

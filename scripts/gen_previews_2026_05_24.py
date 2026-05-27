"""Генерит превью (M+Ж) для заданных пресетов. Bot создаётся напрямую с токеном
(get_bot() в standalone возвращает None). Запуск:
    docker compose exec -T -e PYTHONPATH=/app bot python3 /app/scripts/gen_previews_2026_05_24.py key1 key2 ...
Каждое превью ~$0.12 (fal 2K). Шлёт превью админу + сохраняет в preset_previews.
"""
import asyncio
import os
import sys

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.preset_previews import save_preview
from app.services import fal_image_service as F
from app.handlers.image_edit import (
    EDIT_PRESETS, _MALE_PRESET_OVERRIDES, _MALE_STYLE_SUFFIX, _FEMALE_ONLY_PRESETS,
)

BASE_F = '/app/data/base_face.jpg'
BASE_M = '/app/data/base_face_male.jpg'


async def main(raw_keys):
    # Парсим 'key' или 'key:0'/'key:1' → словарь key→позиция (None = обе).
    want_pos = {}
    keys = []
    for rk in raw_keys:
        if ':' in rk:
            k, p = rk.split(':', 1)
            want_pos[k] = int(p)
            keys.append(k)
        else:
            want_pos[rk] = None
            keys.append(rk)

    # В standalone-процессе EDIT_PRESETS содержит только хардкод — подтягиваем
    # custom-пресеты из БД (как делает бот на старте).
    from app.handlers.image_edit import reload_custom_presets
    await reload_custom_presets()

    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    try:
        for key in keys:
            entry = EDIT_PRESETS.get(key)
            if not entry:
                print(f'NO PRESET: {key}')
                continue
            label, prompt = entry
            female_only = key in _FEMALE_ONLY_PRESETS
            # Прицельная генерация: 'key:0' только жен, 'key:1' только муж,
            # 'key' — обе. Защита от случайного перетирания не той позиции.
            only_pos = want_pos.get(key)
            jobs = []
            if only_pos in (None, 0):
                jobs.append((0, BASE_F, prompt))
            if only_pos in (None, 1) and not female_only:
                male_p = _MALE_PRESET_OVERRIDES.get(key) or (prompt + _MALE_STYLE_SUFFIX)
                jobs.append((1, BASE_M, male_p))
            for pos, base, p in jobs:
                if not os.path.exists(base):
                    print(f'NO BASE FACE: {base}')
                    continue
                with open(base, 'rb') as fh:
                    src = fh.read()
                try:
                    res = await F.edit_image(src, p)
                except Exception as exc:
                    print(f'FAIL {key} pos={pos}: {str(exc)[:160]}')
                    continue
                if not res:
                    print(f'EMPTY {key} pos={pos}')
                    continue
                gender = 'муж' if pos == 1 else 'жен'
                sent = await bot.send_photo(
                    admin, BufferedInputFile(res, f'{key}_{pos}.png'),
                    caption=f'🎨 <b>{label}</b> · {gender}',
                )
                fid = sent.photo[-1].file_id
                async with AsyncSessionFactory() as session:
                    await save_preview(session, key, fid, position=pos,
                                       source_file=f'seed:{key}')
                    await session.commit()
                print(f'OK {key} pos={pos} ({gender})')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    keys = sys.argv[1:] or []
    if not keys:
        print('usage: gen_previews.py key1 key2 ...')
        sys.exit(1)
    asyncio.run(main(keys))

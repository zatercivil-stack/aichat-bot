"""Разовый скрипт: из data/base_face.jpg сделать 2 «обычных» селфи (база Ж)
для основы под TikTok-ролики. Без оригинальной футболки (Dior).
Вариант 1 — селфи в зеркале. Вариант 2 — обычное селфи в руке (не зеркало).
Готовые фото шлём владельцу в личку.
"""
import asyncio
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.config import settings
from app.services import fal_image_service

ADMIN_ID = 0
BASE = Path('data/base_face.jpg')

V1_MIRROR = (
    "Keep this as an ordinary mirror selfie: she still holds the phone up to a "
    "plain wall mirror, everyday bedroom in the background, soft natural daylight "
    "from a window. Replace her black t-shirt with a plain light-grey cotton "
    "t-shirt with NO logos and NO text. Casual, candid, slightly imperfect "
    "amateur phone-photo look — like a normal girl's selfie, not a fashion shoot. "
    "No glamour retouching, natural skin texture."
)

V2_HANDHELD = (
    "Turn this into an ordinary FRONT-FACING handheld selfie — NOT a mirror photo. "
    "She holds the phone at arm's length and looks straight into the camera lens; "
    "no mirror, no mirror reflection, no phone seen sideways. Casual everyday "
    "setting next to a window in a plain room, soft daylight. Replace her black "
    "t-shirt with a plain white oversized t-shirt with NO logos and NO text. "
    "Candid amateur phone-photo look — a normal everyday selfie, no glamour "
    "retouching, natural skin texture."
)


async def main():
    if not fal_image_service.is_enabled():
        print('FAL disabled — no key')
        return
    base_bytes = BASE.read_bytes()
    print('base loaded:', len(base_bytes), 'bytes')

    bot = Bot(token=settings.BOT_TOKEN)
    try:
        for idx, (tag, prompt) in enumerate(
            [('зеркало', V1_MIRROR), ('в руке', V2_HANDHELD)], start=1
        ):
            print(f'--- variant {idx} ({tag}) generating...')
            out = await fal_image_service.edit_image(
                base_bytes, prompt, mime='image/jpeg', image_size=None,
            )
            if not out:
                print(f'variant {idx}: FAILED (None)')
                await bot.send_message(
                    ADMIN_ID, f'⚠️ Вариант {idx} ({tag}) не сгенерился (fal вернул None)'
                )
                continue
            fname = f'neyra_base_v{idx}_{tag}.png'
            Path(f'data/{fname}').write_bytes(out)
            print(f'variant {idx}: ok, {len(out)} bytes -> data/{fname}')
            await bot.send_photo(
                ADMIN_ID,
                BufferedInputFile(out, fname),
                caption=(
                    f'🎬 База Ж для TikTok — вариант {idx} ({tag}).\n'
                    'Обычное селфи, без оригинальной футболки. '
                    'Бери как основу под ролики.'
                ),
            )
            print(f'variant {idx}: sent to {ADMIN_ID}')
    finally:
        await bot.session.close()
    print('DONE')


if __name__ == '__main__':
    asyncio.run(main())

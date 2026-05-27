"""Генерация бренд-ассетов для Нейры:
  - hero banner 16:9 (landscape_16_9) — для использования где-нибудь в боте
  - bot avatar 1:1 (square_hd) — аватарка @NeyraChatBot в Telegram

По 2 варианта каждого формата → 4 файла, ~$0.20 в fal.ai.

Запуск:
    docker compose exec bot python -m scripts.generate_brand_assets
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from app.services.fal_image_service import generate_image, is_enabled

logger = structlog.get_logger(__name__)

OUTPUT_DIR = Path('/app/data/brand_assets')

ASSETS: list[tuple[str, str, str]] = [
    # ── HERO BANNER 16:9 — две версии ──
    ('hero_banner_v1.jpg', 'landscape_16_9',
     'Cinematic 16:9 widescreen hero banner. A young woman with flowing platinum-blonde '
     'hair stands center-right in a confident editorial pose, wearing a flowing '
     'iridescent silver-chrome gown that catches dramatic backlight from a soft pastel-'
     'pink and lavender sunset sky. Wind moves the gown and hair gently. To her left, '
     'negative space showing the dreamy sky gradient — perfect for text overlay. '
     'Hyper-photorealistic editorial fashion photography aesthetic, ultra-sharp focus, '
     'magazine cover quality, refined yet vivid color palette, atmospheric haze in '
     'background, soft golden hour rim-light. No text, no logos.'),
    ('hero_banner_v2.jpg', 'landscape_16_9',
     'Cinematic 16:9 widescreen hero banner. Bold editorial cover shot: a young woman '
     'with sleek brown hair and dramatic Vogue-style makeup stands in profile against '
     'a saturated gradient backdrop transitioning from deep magenta on the left to '
     'soft peach on the right. She wears a glossy red satin draped gown. Soft glamour '
     'lighting catches her sharp cheekbones. Composition leaves clean negative space '
     'on the left third of the frame for potential text. Hyper-photorealistic, '
     'magazine-cover quality, ultra-sharp focus, vivid yet refined palette, editorial '
     'fashion photography. No text, no logos.'),
    # ── AVATAR 1:1 — две версии ──
    ('avatar_v1.jpg', 'square_hd',
     'Iconic minimalist 1:1 square avatar for an AI fashion-photo studio bot. '
     'Striking close-up portrait of a young woman with sleek dark hair, glossy '
     'editorial makeup (soft pink-coral eyeshadow, defined lips), looking directly '
     'into the camera with calm confident gaze. Soft hot-pink rim light from the '
     'left, subtle cyan rim from the right, against a deep aubergine gradient '
     'background. Bold simple composition that reads clearly at small Telegram-'
     'thumbnail sizes. Hyper-photorealistic, sharp focus, magazine-cover quality, '
     'refined yet vivid color palette. No text, no logos.'),
    ('avatar_v2.jpg', 'square_hd',
     'Iconic 1:1 square avatar for an AI photo studio bot. Striking close-up '
     'portrait of a young woman with flowing platinum-blonde hair (light wind '
     'effect), pastel-pink editorial makeup, looking into camera with soft '
     'confident smile. Bright clean studio lighting against a soft gradient '
     'pastel-pink to cream background. Hyper-photorealistic, ultra-sharp focus, '
     'editorial fashion photography aesthetic, magazine-cover quality, clean '
     'composition optimized for use as a Telegram bot avatar (reads well at '
     'small sizes). No text, no logos.'),
]


async def main():
    if not is_enabled():
        print('fal.ai не настроен')
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    done = skipped = 0
    for fname, size, prompt in ASSETS:
        target = OUTPUT_DIR / fname
        if target.exists() and target.stat().st_size > 50_000:
            print(f'⏭ {fname} уже есть, пропускаю')
            skipped += 1
            continue
        print(f'🎨 Генерирую {fname} ({size})…')
        try:
            img = await generate_image(prompt, image_size=size)
            if img:
                target.write_bytes(img)
                print(f'   ✓ {fname} → {len(img) // 1024} KB')
                done += 1
            else:
                print(f'   ✗ {fname} — пустой ответ')
        except Exception as exc:
            print(f'   ✗ {fname} — {exc}')

    print(f'\n=== Готово: создано {done}, пропущено {skipped} ===')
    print(f'Файлы: {OUTPUT_DIR}')


if __name__ == '__main__':
    asyncio.run(main())

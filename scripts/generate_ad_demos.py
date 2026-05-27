"""Генерация свежих demo-изображений для рекламных креативов в TG-каналах.

Использует fal.ai text-to-image (nano-banana-2) для создания generic-кадров
по новым сценам Нейры — без реального селфи юзера. Полезно для:
  - hero-image на рекламные посты (показать что бот умеет)
  - превью в @conturD и других каналах
  - демонстрация на лендинге/сайте

Каждый кадр стоит ~$0.05 в fal.ai. 6 кадров = ~$0.30 = ~₽30.

Запуск:
    docker compose exec bot python -m scripts.generate_ad_demos
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from app.services.fal_image_service import generate_image, is_enabled

logger = structlog.get_logger(__name__)

OUTPUT_DIR = Path('/app/data/launch_scenes')

# Text-to-image версии сцен. Описываем «модель в сцене» — выглядят как
# готовые результаты Нейры. Identity preservation тут не нужен.
# Новая серия (v2) — fisheye/Y2K/bold-red палитра по референсам юзера.
DEMO_SCENES: list[tuple[str, str]] = [
    # ── V2: Fisheye / Y2K / bold-attitude series ──
    ('demo_v2_fisheye_porsche.jpg',
     'Fisheye wide-angle attitude photo of a young blonde woman sitting on the bonnet '
     'of a classic vintage red sports coupe (911-style, no brand text). She reaches one '
     'hand toward the camera in a confident gesture, the other rests on the car hood. '
     'She wears a black turtleneck crop-top, black leather pants with silver chain belt, '
     'mirror-lens aviator sunglasses, hair in messy high bun, layered silver necklaces. '
     'Background: empty urban highway curving up due to fisheye barrel distortion, '
     'blurred city in distance, sharp daylight, clear sky. Y2K retro street photography '
     'aesthetic, vivid saturated colors, sharp focus, photorealistic, high detail.'),
    ('demo_v2_y2k_fridge.jpg',
     'Y2K-aesthetic fisheye close-up portrait of a young woman in front of an open '
     'refrigerator at night. She holds an open box of fresh sushi rolls with playful '
     'surprised expression, mouth slightly open mid-bite. Hair in playful pigtails with '
     'small colourful plastic clips. Subtle freckles, layered thin gold necklaces, '
     'white tank top. Background: brightly-lit refrigerator interior packed with '
     'colourful soda cans, snack bags, condiments, curving outward due to fisheye '
     'distortion. Retro early-2000s digital camera feel, slight grain, vivid saturated '
     'pink/yellow/blue colors, photorealistic.'),
    ('demo_v2_macro_watermelon.jpg',
     'Surreal extreme macro close-up portrait. Young woman with bangs and bold red lips '
     'is drinking through a blue plastic straw, viewed through a circular glass surface '
     'covered in vivid pink-coral watermelon flesh and dark seeds. Her bright eyes look '
     'directly into the camera through the glass. Bright sunny daylight, vibrant blue '
     'sky visible at the curved edges. Hyper-realistic, sharp focus on her eyes and '
     'the straw, surreal commercial advertising photography aesthetic, vivid saturated '
     'pink/red/blue palette, photorealistic.'),
    ('demo_v2_night_racing.jpg',
     'Moody candid night street photo of a young blonde woman walking confidently down '
     'an urban road at night, mouth slightly open mid-stride. She wears an oversized '
     'red-and-black varsity racing jacket over a black crop top and biker shorts, '
     'red cat-eye sunglasses, layered silver chains. Holds a red plastic party cup in '
     'one hand. Long messy windblown hair. Background: dark night street, blurred '
     'headlights forming warm bokeh in distance, faint neon glow on wet asphalt. '
     '35mm film grain, high contrast, intentionally slightly underexposed, Y2K party-'
     'street aesthetic, photorealistic.'),
    ('demo_v2_bold_red_studio.jpg',
     'Bold editorial studio portrait shot from above with slight fisheye distortion. '
     'A young blonde woman crouches in a dynamic pose, her hands wearing oversized '
     'glossy red boxing gloves reaching upward toward the camera dominating the '
     'foreground. She wears a glossy red leather cropped jacket, red corduroy baseball '
     'cap with no logo, dark wayfarer sunglasses. Long messy blonde hair, tongue out '
     'playfully. Clean white studio background. Strong vivid red colour palette '
     'throughout, sharp focus, high-fashion editorial aesthetic, photorealistic.'),
    ('demo_v2_magazine_cover.jpg',
     'Mock magazine cover layout in Y2K aesthetic. Hero image: fisheye close-up of a '
     'young blonde woman with playful pigtails and small colourful hair clips, looking '
     'up into the camera with bold Gen-Z expression, gold layered chain necklaces. '
     'Background: brightly-lit convenience-store aisle full of vivid snack packages '
     'curving dramatically due to fisheye distortion. Cover-text overlay: large red '
     'sans-serif "VIBE" wordmark at top, vertical "2000\'S" on the left, "RAW & REAL" '
     'top-right corner, small footer "NO RULES — NO LIMITS · ISSUE 2016" at bottom. '
     'Retro magazine cover aesthetic, slight scratch/dust film grain, vintage paper '
     'texture overlay, photorealistic.'),
    # ── V1: оставляем для совместимости (старые demo_*.jpg) ──
    ('demo_paris_night.jpg',
     'Full-body editorial night fashion photo on a Parisian Haussmannian street corner. '
     'A young woman in a floor-length backless gold metallic liquid-satin gown with '
     'shimmering reflective finish stands gracefully, looking back over her shoulder '
     'into the camera with a sultry, confident expression. A corner brasserie glows '
     'with vivid red-pink neon casting warm light on the gown. Classical beige stone '
     'building behind, wrought-iron balconies, vintage wall lantern overhead, blurred '
     'pedestrians and parked cars in the distance. Long flowing wavy hair softly '
     'tousled by night wind. Warm cinematic lighting, moody iPhone night-photo '
     'aesthetic, slight film grain, low camera angle, photorealistic, high detail.'),
    ('demo_vintage_car.jpg',
     'Editorial fashion film photo. A young woman seated inside the open door of a '
     'vintage classic black car parked on a cobblestone street, legs crossed elegantly '
     'extending out onto the cobblestones. She wears a fluffy white fur Russian-style '
     'hat, a luxurious white fur coat with oversized collar over a delicate white lace '
     'slip dress, multi-strand pearl necklace, white patterned lace tights, and white '
     'pointed-toe heels with bow detail. Long wavy blonde hair with soft curls on '
     'shoulders, soft glamorous makeup, slight smile, head tilted slightly. Warm '
     'cinematic daylight, vintage 35mm film aesthetic, soft grain, editorial fashion '
     'mood, photorealistic.'),
    ('demo_warrior.jpg',
     'Cinematic vintage-film fantasy portrait. A young woman with platinum blonde hair '
     'spread softly on dark green grass lies on her back, head tilted, lips slightly '
     'parted, calm sensual expression. She wears a silver metallic medieval fantasy '
     'armor (chest plate, shoulder pauldron, articulated arm gauntlet) with brown '
     'leather belt and harness straps. Warm natural sunlight beam illuminates her '
     'face and armor, cinematic side shadow. Vintage film photo aesthetic, soft grain, '
     'warm tones, photorealistic, magazine-quality.'),
    ('demo_y2k.jpg',
     'Y2K-aesthetic fisheye close-up portrait of a young woman inside a brightly-lit '
     'American convenience store. She leans into a wide-angle fisheye lens so her '
     'head and shoulders dominate the frame; the surroundings curve outward '
     'dramatically. Hair in playful pigtails decorated with small colourful plastic '
     'clips. Subtle natural freckles across cheeks and nose. Confident playful Gen-Z '
     'expression, lips softly parted. Layered thin gold necklaces, casual graphic '
     'top. Background: bright fluorescent aisle filled with colourful Doritos-style '
     'snack bags in pink, purple, blue, yellow stacked on shelves on either side, '
     'curving out-of-focus due to fisheye distortion. Y2K nostalgia aesthetic, '
     'retro early-2000s digital camera feel, slight grain, vivid saturated colors.'),
    ('demo_giant_city.jpg',
     'Surreal forced-perspective composite. A young woman appears as a giant figure '
     'striding through a miniature New York City — towering modern skyscrapers visible '
     'at full normal scale on the horizon, but the streets, signs and traffic below '
     'shrunk to tiny ant-sized scale. She is mid-stride, one foot lifted high above '
     'tiny yellow taxis, crosswalks and pedestrians below. Wears oversized graphic '
     'crop tee, loose olive cargo pants, chunky white sneakers, layered gold '
     'earrings. Long red hair flowing. Bright sunny daytime, clear blue sky, sharp '
     'clean shadows. Street signs (ONE WAY visible) on poles. Surreal pop-editorial '
     'aesthetic, vivid saturated colors, sharp focus, high contrast, photorealistic.'),
    ('demo_metro_bouquet.jpg',
     'Moody cinematic editorial portrait on an underground subway platform. A young '
     'woman with bangs and a melancholy expression stands facing the camera, holding '
     'a vibrant bouquet of red gerbera flowers close to her chest with both hands. '
     'Behind her, a yellow subway train passes in strong horizontal motion blur, '
     'painting warm golden streaks across the right half of the frame. Hair softly '
     'tousled by the wind from the passing train. She wears a dark navy oversized '
     'wool coat with high collar, simple gold hoop earrings. Industrial subway '
     'platform, dim tunnel ambient lights. Cinematic shallow depth of field on '
     'subject, motion blur only on background. 35mm film aesthetic, slight grain, '
     'warm-yellow vs cool-shadow color contrast, photorealistic.'),
]


async def main():
    if not is_enabled():
        print('fal.ai не настроен (нет API ключа)')
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    total = len(DEMO_SCENES)
    done = 0
    skipped = 0
    for fname, prompt in DEMO_SCENES:
        target = OUTPUT_DIR / fname
        if target.exists() and target.stat().st_size > 50_000:
            print(f'⏭ {fname} уже есть ({target.stat().st_size // 1024}KB), пропускаю')
            skipped += 1
            continue
        print(f'🎨 [{done + skipped + 1}/{total}] Генерирую {fname}…')
        try:
            # Portrait 4:3 для V2 (новые fisheye-серии) — лучше смотрится в TG
            # на мобиле, выше плотность визуала на экране
            size = 'portrait_4_3' if fname.startswith('demo_v2_') else None
            img = await generate_image(prompt, image_size=size)
            if img:
                target.write_bytes(img)
                print(f'   ✓ {fname} → {len(img) // 1024}KB')
                done += 1
            else:
                print(f'   ✗ {fname} — генерация вернула None')
        except Exception as exc:
            print(f'   ✗ {fname} — ошибка: {exc}')

    print(f'\n=== Итого: сгенерировано {done}, пропущено {skipped}/{total} ===')


if __name__ == '__main__':
    asyncio.run(main())

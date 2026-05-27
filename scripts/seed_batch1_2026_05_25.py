"""Батч 1 новых пресетов (25.05) из uploads-залпа p01-p05.
Сидит в custom_presets (idempotent по key) + reload + генерит превью (М+Ж).
Запуск: docker compose exec -T bot python3 scripts/seed_batch1_2026_05_25.py
"""
import asyncio

from sqlalchemy import select

from app.database.base import AsyncSessionFactory
from app.database.models import CustomPreset
from app.database.repo.custom_presets import create_custom_preset
from app.handlers.image_edit import reload_custom_presets

ID = (
    "\n\nCRITICAL: This is the SAME person from the input photo. Keep the exact "
    "face — same eye shape and colour, nose, lips, jawline, skin tone, hair "
    "colour. Only the scene, outfit and lighting are built around them."
)

PRESETS = [
    dict(
        key="sky_ledge", label="🏙 На карнизе высоток",
        category="🌃 Сцены и города",
        prompt=(
            "Restyle the input photo as a striking ultra-low worm's-eye-view shot "
            "looking up at the subject perched on the edge of a glass skyscraper "
            "ledge against a vivid cyan sky.\n\n"
            "POSE: subject seated on a concrete ledge high above the camera, legs "
            "apart framing the sides of the shot, leaning forward to look down into "
            "the lens, one hand raised pointing two fingers toward the camera with "
            "cool confidence.\n"
            "OUTFIT: black distressed denim-and-leather jacket and matching trousers, "
            "chunky lace-up combat boots, tiny narrow black sunglasses, thin gold "
            "chain.\n"
            "BACKGROUND: a tall blue-glass-and-steel skyscraper rising behind, a "
            "second tower at the right edge, bright cyan sky with soft white clouds, "
            "tiled facade below.\n"
            "LIGHTING: bright clean daylight, crisp directional shadows, teal-and-warm "
            "colour grading, high clarity.\n"
            "ANATOMY: exactly ONE person, ONE head, two arms, two hands with five "
            "fingers each, two legs — no extra limbs.\n\n"
            "8k, ultrarealistic, 24mm wide lens from below, photorealistic, high "
            "detail, bold street-fashion mood." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="bag_pov", label="👜 Кадр из сумки",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a creative POV shot framed through the open "
            "mouth of a bag — the unzipped opening with visible metallic zipper teeth "
            "forms an organic porthole frame around all edges, the subject seen from "
            "inside looking down into the lens.\n\n"
            "POSE: subject leaning over the open bag from above, head and shoulders "
            "filling the centre, looking down into the camera with a calm confident "
            "expression, one hand resting on the bag's edge.\n"
            "OUTFIT: light textured ribbed sleeveless summer knit top, narrow "
            "rectangular sunglasses, minimal jewellery.\n"
            "FRAME: dark fabric and silver zipper teeth border the shot like a "
            "porthole; soft sunlit concrete wall visible in the gap behind the head.\n"
            "LIGHTING: bright natural daylight, warm editorial colour grading.\n"
            "ANATOMY: exactly ONE person, one head, two hands with five fingers — no "
            "extra limbs.\n\n"
            "8k, ultrarealistic, 35mm lens, photorealistic, high detail, "
            "fashion-campaign mood." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="times_fisheye", label="🗽 Тайм-сквер в рыбий глаз",
        category="🌃 Сцены и города",
        prompt=(
            "Restyle the input photo as a dynamic fisheye low-angle street shot in a "
            "busy neon city square (Times-Square style) packed with giant glowing "
            "billboards and a blurred crowd of passers-by.\n\n"
            "POSE: subject striding toward the camera, thrusting a glossy patent "
            "handbag forward toward the lens, hair flowing, chin slightly raised "
            "behind sunglasses, full-of-attitude walk.\n"
            "OUTFIT: light blue cropped ribbed knit, satin track trousers, "
            "mirror-silver chunky platform ankle boots with chain detail.\n"
            "PROP: large glossy patent handbag held forward, catching light.\n"
            "BACKGROUND: towering illuminated billboards and skyscrapers bending in "
            "fisheye distortion, blue sky at top, crosswalk underfoot, small blurred "
            "crowd.\n"
            "ANATOMY: exactly ONE main person in focus, one head, two hands — crowd "
            "kept small and blurred.\n\n"
            "8k, ultrarealistic, 8mm fisheye lens with strong barrel distortion, "
            "bright saturated colour grading, photorealistic, high detail, "
            "street-fashion energy." + ID
        ),
        male_prompt=(
            "Restyle the input photo as a dynamic fisheye low-angle street shot in a "
            "busy neon city square (Times-Square style) packed with giant glowing "
            "billboards and a blurred crowd.\n\n"
            "POSE: subject striding confidently toward the camera, holding a glossy "
            "designer duffel bag forward toward the lens, chin raised behind narrow "
            "sunglasses, swagger stride.\n"
            "OUTFIT: oversized boxy bomber or tech jacket, baggy cargo trousers, "
            "chunky sneakers; layered silver chain. NO crop top, NO heels.\n"
            "PROP: glossy designer duffel/holdall held forward, catching light.\n"
            "BACKGROUND: towering illuminated billboards and skyscrapers bending in "
            "fisheye distortion, blue sky at top, crosswalk underfoot, small blurred "
            "crowd.\n"
            "ANATOMY: exactly ONE main person in focus, one head, two hands — crowd "
            "kept small and blurred.\n\n"
            "8k, ultrarealistic, 8mm fisheye lens with strong barrel distortion, "
            "bright saturated colour grading, photorealistic, high detail, "
            "street-fashion energy." + ID
        ),
    ),
    dict(
        key="rebel_poster", label="🔴 Постер бунтаря",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a bold street-art magazine poster: the "
            "subject crouches confidently in front of a torn-paper collage wall in "
            "aggressive red, black and white tones.\n\n"
            "POSE: subject in a low confident crouch, forearms resting on knees, "
            "direct intense gaze at the camera.\n"
            "OUTFIT: oversized red bomber jacket over a black top, black cargo "
            "trousers, chunky black lace-up boots.\n"
            "BACKGROUND: layered ripped-paper collage in red, black and white — "
            "fragments of newspaper, halftone textures, and a few bold torn Cyrillic "
            "poster words «СИЛА», «БУНТ», «УЛИЦА» in heavy type, grungy editorial "
            "layout.\n"
            "LIGHTING: punchy studio key light, crisp shadow, high-contrast red/black "
            "grade.\n"
            "ANATOMY: exactly ONE person, one head, two arms, two hands, two legs — "
            "no extra limbs.\n\n"
            "8k, ultrarealistic, 50mm lens, high-contrast red-black colour grading, "
            "photorealistic, high detail, rebellious streetwear poster mood." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="flash_party", label="📸 Кадр со вспышкой",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a gritty direct-flash party snapshot at "
            "night — harsh on-camera flash, deep shadows, a blurred crowd behind in "
            "a dark venue.\n\n"
            "POSE: subject close to the camera, making a relaxed two-finger peace "
            "sign near the eye, slight playful smirk, head tilted.\n"
            "OUTFIT: black graphic tee or cropped jacket, clear thin-framed glasses, "
            "layered silver necklaces.\n"
            "BACKGROUND: dark club / house-party with out-of-focus people, tiny "
            "specular highlights and bokeh, faint coloured lights.\n"
            "LIGHTING: hard direct on-camera flash, bright on the face, fast falloff "
            "to deep shadow, slight skin overexposure — authentic flash-photography "
            "look.\n"
            "ANATOMY: exactly ONE main person in focus, one head, two hands with five "
            "fingers — background people blurred.\n\n"
            "8k, ultrarealistic, 28mm lens with on-camera flash, cool night grade "
            "with warm flash, photorealistic, high detail, candid nightlife mood. "
            "NO alcohol, no bottles, no cans." + ID
        ),
        male_prompt=None,
    ),
]


async def main():
    from app.bot import create_bot
    create_bot()  # нужен get_bot() внутри генератора превью

    async with AsyncSessionFactory() as session:
        existing = set((await session.execute(select(CustomPreset.key))).scalars().all())
        created = []
        for p in PRESETS:
            if p['key'] in existing:
                print(f"skip (exists): {p['key']}")
                continue
            await create_custom_preset(
                session, key=p['key'], label=p['label'], prompt=p['prompt'],
                male_prompt=p.get('male_prompt'), quality_mode='quality',
                category=p['category'], female_only=False,
            )
            created.append(p['key'])
        await session.commit()
    print('created:', created)

    n = await reload_custom_presets()
    print('reloaded EDIT_PRESETS, total:', n)

    # Генерация превью (М+Ж) — платно (~$0.12 каждое). Шлёт админу + сохраняет file_id.
    from app.webserver.admin import _do_generate_preview_by_key
    for p in PRESETS:
        if p['key'] not in created:
            continue
        print(f"generating previews: {p['key']} …")
        await _do_generate_preview_by_key(
            p['key'], p['label'], p['prompt'], p.get('male_prompt'), False,
        )
        await asyncio.sleep(1.0)
    print('DONE batch 1')


if __name__ == '__main__':
    asyncio.run(main())

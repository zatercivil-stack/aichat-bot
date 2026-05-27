"""Батч 2 новых пресетов (25.05) из uploads-залпа p06-p10.
Детальные промпты с сохранением операторских фишек (фидбэк владельца 25.05).
Запуск: docker compose exec -T bot bash -c 'PYTHONPATH=/app python3 -m scripts.seed_batch2_2026_05_25'
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
        key="pop_zine", label="📰 Обложка поп-зина",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a bold pop-art zine MAGAZINE COVER built as "
            "a cut-out collage: the subject is cleanly cut out with a thick white "
            "sticker-outline and pasted over a flat halftone-dotted electric-blue "
            "background, with giant overlapping display type behind the head.\n\n"
            "POSE: confident three-quarter stance, one hand on hip, chin slightly up, "
            "direct gaze; head and torso fill most of the frame as a cut-out.\n"
            "OUTFIT: oversized glossy red jacket with sewn-on patches and a "
            "wristwatch, layered cords.\n"
            "GRAPHICS (key effect): a huge bold sans-serif Russian display word "
            "«СЛАДКО» in yellow stretches across the cover BEHIND the cut-out head "
            "and is partly hidden by it; scattered zine stickers — a small heart "
            "sticker on the cheek, a pink swirl sticker, three little hearts, a "
            "hand-scrawled red signature, a vertical barcode, and the year «2025» "
            "small at the bottom.\n"
            "STYLE: cut-out paper collage, crisp white outline around the figure, "
            "halftone dot texture, glossy magazine print, slight paper grain.\n"
            "ANATOMY: exactly ONE person, one head, two hands — no extra limbs.\n\n"
            "8k, photorealistic face with graphic-collage treatment, bold saturated "
            "yellow/red/blue palette, high detail, pop-zine cover mood." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="ramen_night", label="🍜 Ночь в раменной",
        category="🌃 Сцены и города",
        prompt=(
            "Restyle the input photo as a cinematic fashion-magazine film still set "
            "inside a tiny neon-lit late-night Japanese ramen shop.\n\n"
            "POSE: subject seated side-on at the counter on a stool, leaning to eat "
            "with chopsticks raised toward the mouth, calm cinematic gaze, elegant "
            "relaxed posture.\n"
            "OUTFIT: glossy black patent-leather long trench coat over a white shirt, "
            "a couple of silver rings.\n"
            "FOREGROUND (key composition): a glossy RED laminate counter runs as a "
            "strong diagonal leading line across the lower frame, with a steaming "
            "pale-blue ramen bowl, a chopstick holder, soy-sauce bottles, and stacked "
            "pale-blue bowls on the shelf to the left.\n"
            "BACKGROUND: cramped authentic ramen-ya — stainless kitchen, hanging menu "
            "banners, a vintage poster on the wall, warm fluorescent ceiling tubes.\n"
            "LIGHTING: warm fluorescent plus faint neon, soft film-grade contrast, "
            "slight grain, moody cinematic colour.\n"
            "ANATOMY: exactly ONE person, one head, two hands holding chopsticks "
            "correctly — no extra limbs.\n\n"
            "8k, ultrarealistic, 50mm cinematic film still, warm editorial colour "
            "grading, photorealistic, high detail, night-editorial mood." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="cctv_muse", label="🎥 Камера наблюдения",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a stylised security-camera frame: a high "
            "CCTV angle looking down at the subject standing on a zebra crosswalk, "
            "overlaid with a Y2K surveillance HUD.\n\n"
            "POSE: subject standing on the white crosswalk stripes seen from a high "
            "angle, one hand in pocket, the other holding a red paper coffee cup, "
            "looking up toward the camera through narrow sunglasses, cool attitude.\n"
            "OUTFIT: beige ribbed sleeveless top, baggy low-rise jeans, layered "
            "silver necklaces.\n"
            "HUD OVERLAY (key effect): grainy CCTV look with a faint grid; thin blue "
            "rectangular detection boxes around the face and around a shoe with tiny "
            "monospace tags; a white mouse-cursor arrow near the head; small "
            "Windows-style folder icons on the left; a yellow warning triangle; "
            "corner readouts in Russian monospace «КАМЕРА 01», «ОБЪЕКТ 01», a "
            "timestamp «13:01:49», «КАДР 05/20»; faint scanlines.\n"
            "LIGHTING: flat overhead daylight on asphalt, slight CCTV desaturation "
            "and grain.\n"
            "ANATOMY: exactly ONE person full-body, one head, two arms, two legs — "
            "no extra limbs.\n\n"
            "8k, photorealistic subject under a stylised security-cam overlay, muted "
            "grainy palette with electric-blue UI accents, high detail, Y2K "
            "surveillance-collage mood." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="fireworks_night", label="🎆 Огни фейерверка",
        category="🌃 Сцены и города",
        prompt=(
            "Restyle the input photo as a cinematic night portrait at a summer "
            "fireworks festival, the sky behind filled with huge multicoloured "
            "fireworks bursting over a dark crowd.\n\n"
            "POSE: subject turned, glancing back over the shoulder toward the camera, "
            "one hand raised near the cheek, soft dreamy expression.\n"
            "OUTFIT: traditional floral yukata in cream with a pink-and-red poppy "
            "print and an obi sash.\n"
            "BACKGROUND (key effect): dark night sky dominated by several massive "
            "colourful firework bursts (golden, magenta, blue) casting warm light "
            "onto the subject; silhouettes of a festival crowd along the bottom, "
            "distant city glow, soft bokeh embers.\n"
            "LIGHTING: low ambient night lit mainly by the warm coloured glow of the "
            "fireworks on the face, gentle film grain, slightly hazy.\n"
            "ANATOMY: exactly ONE main person in focus, one head, two hands — crowd "
            "kept as dark silhouettes.\n\n"
            "8k, ultrarealistic, 35mm film grain, moody night grade with vivid "
            "firework colours, photorealistic, high detail, dreamy festival mood." + ID
        ),
        male_prompt=(
            "Restyle the input photo as a cinematic night portrait at a summer "
            "fireworks festival, the sky filled with huge multicoloured bursts over "
            "a dark crowd.\n\n"
            "POSE: subject glancing back toward the camera, calm cinematic "
            "expression, one hand near the jaw.\n"
            "OUTFIT: a dark men's yukata or a relaxed dark jacket over a plain tee — "
            "NO floral kimono, no women's print, no makeup.\n"
            "BACKGROUND (key effect): dark night sky with several massive colourful "
            "firework bursts casting warm light onto the subject; festival crowd "
            "silhouettes, distant city glow, soft bokeh embers.\n"
            "LIGHTING: low ambient night lit mainly by the warm firework glow on the "
            "face, gentle film grain.\n"
            "ANATOMY: exactly ONE main person in focus, one head, two hands — crowd "
            "as dark silhouettes.\n\n"
            "8k, ultrarealistic, 35mm film grain, moody night grade with vivid "
            "firework colours, photorealistic, high detail, dreamy festival mood." + ID
        ),
    ),
    dict(
        key="sparkler_night", label="🎇 Бенгальский огонь",
        category="✨ Стиль и портрет",
        prompt=(
            "Restyle the input photo as an intimate low-light night portrait lit "
            "almost entirely by a single hand-held sparkler.\n\n"
            "POSE: subject holding a lit sparkler up near the face, looking softly "
            "toward it, calm tender expression, a small bundle of unlit sparklers in "
            "the other hand.\n"
            "OUTFIT: simple dark jacket over a white tee, natural relaxed styling.\n"
            "LIGHTING (key effect): the sparkler is the ONLY warm light source — "
            "bright crackling sparks throw warm orange light across one side of the "
            "face while the rest of the scene stays deep-blue night darkness; strong "
            "chiaroscuro, warm glints in the eyes.\n"
            "BACKGROUND: near-black blue night, faint bokeh, barely-visible "
            "surroundings.\n"
            "ANATOMY: exactly ONE person, one head, two hands with five fingers — no "
            "extra limbs; sparks rendered as fine bright points, not a blob.\n\n"
            "8k, ultrarealistic, 50mm f/1.8 shallow depth, deep-blue-and-warm "
            "chiaroscuro grade, photorealistic, high detail, intimate cinematic "
            "night mood." + ID
        ),
        male_prompt=None,
    ),
]


async def main():
    from app.bot import create_bot
    create_bot()

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

    from app.webserver.admin import _do_generate_preview_by_key
    for p in PRESETS:
        if p['key'] not in created:
            continue
        print(f"generating previews: {p['key']} …")
        await _do_generate_preview_by_key(
            p['key'], p['label'], p['prompt'], p.get('male_prompt'), False,
        )
        await asyncio.sleep(1.0)
    print('DONE batch 2')


if __name__ == '__main__':
    asyncio.run(main())

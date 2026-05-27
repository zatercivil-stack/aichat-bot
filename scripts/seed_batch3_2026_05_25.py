"""Батч 3 (p11-p15). Grade-matching по каждому оригиналу (фидбэк 25.05).
Запуск: docker compose exec -T bot bash -c 'PYTHONPATH=/app python3 -m scripts.seed_batch3_2026_05_25'
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
    "colour. Only the scene, outfit, style and lighting are built around them."
)

PRESETS = [
    dict(
        key="text_portrait", label="✒️ Я — это я",
        category="✨ Стиль и портрет",
        prompt=(
            "Restyle the input photo as an editorial typographic split-poster.\n\n"
            "LAYOUT (key effect): the frame is split — the LEFT half is a tight "
            "half-face portrait of the subject (one eye, cheek, lips, bare shoulder, "
            "a delicate chain necklace); the RIGHT half is filled with the bold "
            "black serif words «Я — ЭТО Я» stacked in large heavy type, where the "
            "letters act as a clipping mask filled with the continuation of the "
            "subject's hair and skin texture so the portrait bleeds into the "
            "typography.\n"
            "POSE: half-face turned to camera, calm direct gaze, chin slightly "
            "down.\n"
            "COLOUR & TREATMENT (match exactly): muted desaturated warm grey / sepia "
            "palette, soft visible film grain, gentle low contrast, matte editorial "
            "finish, slightly faded — NOT vivid, NOT glossy, NOT 8k. Intimate, "
            "moody, magazine-art mood.\n"
            "ANATOMY: one head, the visible eye area sharp, recognizable as the same "
            "person.\n\n"
            "Editorial typographic collage: muted grainy sepia half-portrait beside "
            "bold black serif Cyrillic letters." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="fashion_doll", label="🪆 Гламурная кукла",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo, turning the subject into a glossy collectible "
            "FASHION DOLL — keep the face recognizably theirs but rendered as a "
            "stylised doll.\n\n"
            "DOLL STYLE (key effect): smooth glossy vinyl/plastic doll skin with a "
            "soft sheen, an oversized doll head on a slim doll-proportioned body, "
            "large glossy plump lips, big stylised almond doll eyes with long "
            "painted lashes, perfectly styled glossy hair with blunt bangs.\n"
            "OUTFIT: black ribbed cropped tank, studded belt, dark denim, tiny "
            "beaded jewellery — miniature fashion-doll clothing.\n"
            "BACKGROUND: soft-focus neutral grey studio with a warm rim light, like "
            "a doll product photo.\n"
            "COLOUR & TREATMENT (match exactly): clean glossy product-shot lighting, "
            "smooth plastic highlights, slightly warm, crisp — this one IS polished "
            "and glossy (it is a doll).\n"
            "ANATOMY: exactly ONE doll, one head, two arms, two hands — doll "
            "proportions.\n\n"
            "Collectible fashion-doll product photo, glossy vinyl, stylised "
            "oversized features, recognizable face." + ID
        ),
        male_prompt=(
            "Restyle the input photo, turning the subject into a glossy collectible "
            "MALE FASHION DOLL — face recognizably theirs, rendered as a stylised "
            "doll.\n\n"
            "DOLL STYLE: smooth glossy vinyl doll skin, slightly oversized doll "
            "head, stylised masculine doll eyes and brows, styled glossy doll hair "
            "— NO lipstick, NO long lashes, NO feminine makeup.\n"
            "OUTFIT: miniature streetwear — black tee or tank, studded belt, baggy "
            "cargo/denim, a small chain.\n"
            "BACKGROUND: soft-focus neutral grey studio with warm rim light, doll "
            "product photo.\n"
            "COLOUR & TREATMENT: clean glossy product-shot lighting, smooth plastic "
            "highlights, crisp.\n"
            "ANATOMY: exactly ONE doll, one head, two arms, two hands.\n\n"
            "Collectible fashion-doll product photo, glossy vinyl, stylised, "
            "recognizable male face." + ID
        ),
    ),
    dict(
        key="retro_horror", label="📞 Звонок из ужастика",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a cinematic still from a 1980s slasher "
            "movie.\n\n"
            "SCENE (key composition): the subject lies on their front across a "
            "glossy RED SATIN bed in a teen bedroom, holding a vintage cream corded "
            "landline phone to the ear, the coiled cord trailing across the "
            "foreground; walls covered in retro 80s movie/music posters; a bowl of "
            "colourful candy and glossy magazines beside them; in the lit doorway "
            "DEEP in the background a costumed figure in a white skull/ghost mask "
            "lurks ominously (clearly a playful movie-prop, not gory).\n"
            "POSE: lying propped on forearms, phone to ear, looking off "
            "thoughtfully.\n"
            "OUTFIT: pale strapless top, layered gold necklaces, chunky gold "
            "rings.\n"
            "COLOUR & TREATMENT (match exactly): warm tungsten 1980s film grade, "
            "slightly muted, soft film grain, gentle halation on highlights, "
            "cinematic shallow depth — vintage movie-still look, NOT modern glossy, "
            "NOT 8k clean.\n"
            "ANATOMY: exactly ONE main person, one head, two hands holding the "
            "phone; the masked figure small and dark in the far doorway.\n\n"
            "1980s slasher film still, warm grainy tungsten grade, red satin, retro "
            "posters." + ID
        ),
        male_prompt=(
            "Restyle the input photo as a cinematic still from a 1980s slasher "
            "movie.\n\n"
            "SCENE: the subject reclines on a glossy RED SATIN bed in a retro "
            "bedroom, holding a vintage corded landline phone to the ear, cord "
            "trailing in the foreground; walls covered in retro 80s posters; candy "
            "bowl and magazines nearby; in the lit doorway deep in the background a "
            "costumed figure in a white skull mask lurks (playful movie prop, not "
            "gory).\n"
            "POSE: reclining propped on an elbow, phone to ear, looking off.\n"
            "OUTFIT: plain tee or open shirt, a couple of rings — NO strapless top, "
            "no feminine styling.\n"
            "COLOUR & TREATMENT: warm tungsten 80s film grade, muted, soft grain, "
            "halation, cinematic — vintage movie still, NOT clean 8k.\n"
            "ANATOMY: exactly ONE main person, one head, two hands; masked figure "
            "small/dark in doorway.\n\n"
            "1980s slasher film still, warm grainy tungsten grade, red satin, retro "
            "posters." + ID
        ),
    ),
    dict(
        key="cartoon_princess", label="🏰 Сказочный мультфильм",
        category="✨ Стиль и портрет",
        prompt=(
            "Restyle the input photo as a hand-animated fairytale CARTOON character "
            "(classic 2D animated-movie style), keeping the face recognizably theirs "
            "but drawn as an animated heroine.\n\n"
            "STYLE (key effect): smooth cel/painterly animation rendering, large "
            "expressive stylised eyes with long lashes, delicate freckles, soft "
            "painted shading, glossy animated hair — like a frame from a classic "
            "animated feature.\n"
            "OUTFIT: sparkly black spaghetti-strap gown.\n"
            "BACKGROUND: dreamy dark-night fairytale town — silhouetted castle "
            "spires, glowing warm street lanterns, soft bokeh, painted depth.\n"
            "COLOUR & TREATMENT (match exactly): rich but moody animated-night "
            "palette, deep blue shadows plus warm lantern glow, smooth painterly "
            "finish — illustration, NOT a photo, NOT a glossy 3D render.\n"
            "ANATOMY: one head, two shoulders — clean animated character.\n\n"
            "Classic 2D animated-movie still, painterly, moody night, recognizable "
            "stylised face." + ID
        ),
        male_prompt=(
            "Restyle the input photo as a hand-animated fairytale CARTOON character "
            "(classic 2D animated-movie style), face recognizably theirs, drawn as "
            "an animated hero/prince.\n\n"
            "STYLE: smooth cel/painterly animation, expressive stylised masculine "
            "eyes and brows (NO lipstick, NO long lashes), soft painted shading, "
            "animated hair.\n"
            "OUTFIT: dark elegant jacket or shirt.\n"
            "BACKGROUND: dreamy dark-night fairytale town — silhouetted castle "
            "spires, glowing lanterns, soft bokeh.\n"
            "COLOUR & TREATMENT: moody animated-night palette, deep blue plus warm "
            "lantern glow, painterly — illustration, NOT a photo.\n"
            "ANATOMY: one head, two shoulders — clean animated character.\n\n"
            "Classic 2D animated-movie still, painterly, moody night, recognizable "
            "stylised male face." + ID
        ),
    ),
    dict(
        key="anime_90s", label="📺 Аниме девяностых",
        category="✨ Стиль и портрет",
        prompt=(
            "Restyle the input photo in authentic 1990s retro ANIME style "
            "(Sailor-Moon / city-pop era cel animation), keeping the face "
            "recognizably theirs but drawn as a 90s anime character.\n\n"
            "STYLE (key effect): hand-drawn cel-animation look, large glossy "
            "reflective anime eyes, voluminous flowing retro 90s hair, bold clean "
            "linework, flat cel shading with airbrushed gradients.\n"
            "OUTFIT: oversized red retro windbreaker shirt, high-waist grey "
            "trousers, a fingerless red glove, gold earrings.\n"
            "BACKGROUND: dim retro mecha / engine-room interior with pipes, panels "
            "and small indicator lights, muted teal-grey.\n"
            "COLOUR & TREATMENT (match exactly): vintage 90s anime palette — "
            "slightly faded, muted warm-and-teal tones, subtle film grain / VHS "
            "softness over the cel art, nostalgic retro-anime mood — NOT modern "
            "crisp digital anime, NOT photoreal.\n"
            "ANATOMY: one head, two arms, two hands — clean anime character.\n\n"
            "1990s retro cel-anime still, faded muted palette, glossy eyes, "
            "voluminous hair." + ID
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
                print(f"skip (exists): {p['key']}"); continue
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
    print('DONE batch 3')


if __name__ == '__main__':
    asyncio.run(main())

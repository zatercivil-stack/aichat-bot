"""Батч 4 (p18,p19,p20). p16/p17 пропущены как дубли cartoon_princess.
grade-matching по каждому. Запуск:
docker compose exec -T bot bash -c 'PYTHONPATH=/app python3 -m scripts.seed_batch4_2026_05_25'
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
        key="neon_portrait", label="🕶 Неоновый портрет",
        category="🔥 Дерзкие и яркие",
        prompt=(
            "Restyle the input photo as a bold studio editorial portrait under "
            "dramatic neon gradient lighting.\n\n"
            "POSE: low-angle shot looking up at the subject, head tilted back and "
            "up, chin raised, tousled hair, cool confident attitude, wearing "
            "tinted yellow-lens sunglasses.\n"
            "OUTFIT: black leather jacket over a bare collar.\n"
            "BACKGROUND: smooth seamless studio backdrop in a magenta-to-purple "
            "gradient.\n"
            "LIGHTING (key effect): dramatic dual-gel neon lighting — cool blue/teal "
            "light from one side, warm magenta/orange from the other, sculpting the "
            "face; glossy highlights on skin and leather.\n"
            "COLOUR & TREATMENT (match exactly): clean, sharp, saturated neon colour "
            "grade, crisp studio finish (this one IS polished and glossy), strong "
            "contrast.\n"
            "ANATOMY: one head, two shoulders.\n\n"
            "8k editorial studio portrait, dual-gel neon lighting, saturated, "
            "glossy, dramatic." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="moody_bar", label="🥃 Ночь в баре",
        category="🌃 Сцены и города",
        prompt=(
            "Restyle the input photo as a gritty candid film-grain night photo in a "
            "dim moody vintage bar.\n\n"
            "POSE: subject seated alone at a small bar table, relaxed, looking "
            "toward the camera, one arm resting on the table beside a short whisky "
            "glass.\n"
            "OUTFIT: crisp white shirt, dark blazer, plaid mini skirt (or dark "
            "trousers).\n"
            "BACKGROUND: cramped dim bar — a warm chandelier, shelves of bottles, "
            "framed pictures and vintage clutter, deep shadows.\n"
            "COLOUR & TREATMENT (match exactly): shot on 35mm film with HEAVY "
            "visible grain and dust, underexposed warm tungsten, deep shadows, "
            "muted warm palette, slight haze and faint light leaks, candid amateur "
            "night snapshot — NOT polished, NOT glossy, NOT 8k, NOT bright.\n"
            "ANATOMY: exactly ONE person, one head, two hands; if a second figure "
            "appears keep it a dark out-of-focus silhouette.\n\n"
            "35mm film bar photo, heavy grain, warm tungsten, dark, moody, candid." + ID
        ),
        male_prompt=None,
    ),
    dict(
        key="arcade_night", label="🕹 Ночь в зале автоматов",
        category="🌃 Сцены и города",
        prompt=(
            "Restyle the input photo as a cinematic vintage film photo in a "
            "neon-lit Japanese arcade at night.\n\n"
            "POSE: subject standing, leaning lightly at a glowing arcade machine, "
            "glancing back toward the camera over the shoulder, calm expression; "
            "modest tasteful framing.\n"
            "OUTFIT: retro cream short-sleeve usherette/diner uniform dress with a "
            "little cap, modest length.\n"
            "BACKGROUND: cramped neon arcade — glowing screens, machines, Japanese "
            "signage, warm-and-cool neon reflections, smoky depth.\n"
            "COLOUR & TREATMENT (match exactly): shot on grainy vintage film, HEAVY "
            "grain and dust, warm neon glow against deep shadows, slightly faded "
            "muted palette, hazy, nostalgic — candid, NOT glossy, NOT 8k, NOT "
            "bright.\n"
            "ANATOMY: exactly ONE person, one head, two hands; modest framing.\n\n"
            "Vintage film arcade photo, heavy grain, warm neon, faded, nostalgic, "
            "cinematic." + ID
        ),
        male_prompt=(
            "Restyle the input photo as a cinematic vintage film photo in a "
            "neon-lit Japanese arcade at night.\n\n"
            "POSE: subject standing, leaning at a glowing arcade machine, glancing "
            "toward the camera, calm cool expression.\n"
            "OUTFIT: retro short-sleeve bowling/work shirt and trousers (NO dress, "
            "no cap, no feminine styling).\n"
            "BACKGROUND: cramped neon arcade — glowing screens, machines, Japanese "
            "signage, warm-and-cool neon reflections, smoky depth.\n"
            "COLOUR & TREATMENT (match exactly): grainy vintage film, HEAVY grain "
            "and dust, warm neon glow against deep shadows, faded muted palette, "
            "hazy, nostalgic — candid, NOT glossy, NOT 8k, NOT bright.\n"
            "ANATOMY: exactly ONE person, one head, two hands.\n\n"
            "Vintage film arcade photo, heavy grain, warm neon, faded, nostalgic, "
            "cinematic." + ID
        ),
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
    print('reloaded total:', n)
    from app.webserver.admin import _do_generate_preview_by_key
    for p in PRESETS:
        if p['key'] not in created:
            continue
        print(f"generating previews: {p['key']} …")
        await _do_generate_preview_by_key(
            p['key'], p['label'], p['prompt'], p.get('male_prompt'), False,
        )
        await asyncio.sleep(1.0)
    print('DONE batch 4')


if __name__ == '__main__':
    asyncio.run(main())

"""Перегенерация 2 пресетов (fireworks_night, sparkler_night) с корректным
film-look treatment-блоком (фидбэк 25.05: матчить грейд/плёнку/настроение
оригинала, без глянцевого 8k-хвоста). Обновляет prompt в custom_presets +
reload + регенерит превью (перезаписывает кривые)."""
import asyncio
from sqlalchemy import select

from app.database.base import AsyncSessionFactory
from app.database.models import CustomPreset
from app.handlers.image_edit import reload_custom_presets

ID = (
    "\n\nCRITICAL: This is the SAME person from the input photo. Keep the exact "
    "face — same eye shape and colour, nose, lips, jawline, skin tone, hair "
    "colour. Only the scene, outfit and lighting are built around them."
)

FIREWORKS_F = (
    "Restyle the input photo as a moody, melancholic amateur night snapshot at a "
    "Japanese summer fireworks festival.\n\n"
    "POSE: subject crouched low in the crowd, turned to glance back over the "
    "shoulder toward the camera, one hand resting against the cheek, soft pensive "
    "expression, face partly in shadow.\n"
    "OUTFIT: cream floral yukata with a pink-and-red poppy print and obi sash.\n"
    "BACKGROUND: dark night sky filled with large soft fireworks (magenta, gold, "
    "blue) and lower golden firework fountains; a dark silhouetted festival crowd "
    "along the bottom; faint drifting embers.\n"
    "COLOUR & TREATMENT (match exactly): shot on 35mm film with heavy visible "
    "grain and noise; heavily underexposed, low-key night, deep crushed dark "
    "shadows; muted desaturated palette where the ONLY real colour comes from the "
    "warm pink-and-gold firework glow; cool blue-black ambient darkness; soft "
    "slightly hazy focus, smoky atmospheric haze; candid amateur snapshot feel — "
    "NOT polished, NOT glossy, NOT studio, NOT high-fashion, NOT sharp, NOT 8k, "
    "NOT bright. Nostalgic, intimate, cinematic lo-fi.\n"
    "FACE: dimly and warmly lit, partly in shadow, still clearly the same "
    "recognizable person.\n"
    "ANATOMY: exactly ONE main person in focus, one head, two hands; crowd as "
    "dark silhouettes.\n\n"
    "35mm film photograph, grainy, dark, moody, muted colours with warm firework "
    "glow." + ID
)

FIREWORKS_M = (
    "Restyle the input photo as a moody, melancholic amateur night snapshot at a "
    "Japanese summer fireworks festival.\n\n"
    "POSE: subject turned to glance back toward the camera, calm pensive "
    "expression, one hand near the jaw, face partly in shadow.\n"
    "OUTFIT: a dark men's yukata or a plain dark jacket over a tee — NO floral "
    "kimono, no women's print, no makeup.\n"
    "BACKGROUND: dark night sky filled with large soft fireworks (magenta, gold, "
    "blue) and lower golden fountains; silhouetted festival crowd; faint embers.\n"
    "COLOUR & TREATMENT (match exactly): shot on 35mm film with heavy visible "
    "grain; heavily underexposed low-key night, deep crushed shadows; muted "
    "desaturated palette, colour ONLY from the warm firework glow; cool blue-black "
    "ambient; soft hazy focus, atmospheric haze; candid amateur snapshot — NOT "
    "polished, NOT glossy, NOT studio, NOT sharp, NOT 8k, NOT bright. Nostalgic, "
    "intimate, cinematic lo-fi.\n"
    "FACE: dimly warmly lit, partly shadowed, still clearly the same recognizable "
    "person.\n"
    "ANATOMY: exactly ONE main person, one head, two hands; crowd as dark "
    "silhouettes.\n\n"
    "35mm film photograph, grainy, dark, moody, muted colours with warm firework "
    "glow." + ID
)

SPARKLER = (
    "Restyle the input photo as an intimate, moody amateur night portrait lit only "
    "by a hand-held sparkler.\n\n"
    "POSE: subject holding a lit sparkler near the face, looking softly down "
    "toward it, calm tender melancholic expression, a small bundle of unlit "
    "sparklers in the other hand.\n"
    "OUTFIT: simple dark jacket over a white tee.\n"
    "COLOUR & TREATMENT (match exactly): shot on 35mm film with heavy visible "
    "grain; very dark low-key night, deep blue-black shadows filling most of the "
    "frame; the sparkler is the ONLY light — warm amber glow on one side of the "
    "face falling off fast into darkness; muted desaturated palette except the "
    "warm sparkler light and a faint cool-blue rim; soft slightly hazy focus; "
    "candid amateur snapshot — NOT polished, NOT glossy, NOT studio, NOT sharp, "
    "NOT 8k, NOT bright. Nostalgic, intimate, cinematic lo-fi.\n"
    "FACE: lit dimly by the warm sparkler, half in shadow, still clearly the same "
    "recognizable person, warm glints in the eyes.\n"
    "BACKGROUND: near-black blue night, faint distant bokeh lights across dark "
    "water.\n"
    "ANATOMY: exactly ONE person, one head, two hands with five fingers; sparks "
    "rendered as fine bright points, not a blob.\n\n"
    "35mm film photograph, grainy, dark, low-key, intimate, warm sparkler glow in "
    "blue night." + ID
)

UPDATES = {
    'fireworks_night': dict(prompt=FIREWORKS_F, male_prompt=FIREWORKS_M),
    'sparkler_night':  dict(prompt=SPARKLER, male_prompt=None),
}


async def main():
    from app.bot import create_bot
    create_bot()

    async with AsyncSessionFactory() as session:
        for key, vals in UPDATES.items():
            row = await session.scalar(select(CustomPreset).where(CustomPreset.key == key))
            if not row:
                print(f'NOT FOUND: {key}'); continue
            row.prompt = vals['prompt']
            row.male_prompt = vals['male_prompt']
            print(f'updated prompt: {key}')
        await session.commit()

    await reload_custom_presets()
    print('reloaded')

    from app.webserver.admin import _do_generate_preview_by_key
    for key, vals in UPDATES.items():
        print(f'regenerating previews: {key} …')
        await _do_generate_preview_by_key(
            key, key, vals['prompt'], vals['male_prompt'], False,
        )
        await asyncio.sleep(1.0)
    print('DONE regen filmlook')


if __name__ == '__main__':
    asyncio.run(main())

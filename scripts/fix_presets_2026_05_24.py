"""Правки пресетов по фидбэку 24.05:
- tokyo_vending: male_prompt без цветов на очках и без ошейника
- red_punk: male_prompt без женской одежды (бандо/перья/ногти/макияж)
- cat_beanie: female_only=True (убираем мужскую версию)
"""
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

TOKYO_MALE = (
    "Restyle the input photo as a fisheye street photo at night by a Japanese "
    "vending machine. A MAN leans into the wide lens on a narrow Tokyo back-"
    "street, the machine glowing beside him.\n\n"
    "POSE: leaning toward the fisheye lens with playful confident attitude, one "
    "hand up near a glowing vending machine.\n"
    "OUTFIT (MALE): plain narrow black sunglasses — NO flowers, NO decorations on "
    "the glasses; a black graphic tee, glossy dark trousers, a few plain silver "
    "rings. STRICTLY NO choker, NO studded collar, NO neck jewellery, NO feminine "
    "accessories.\n"
    "BACKGROUND: narrow Japanese street at dusk — a glowing vending machine with "
    "Japanese signage, low buildings, bicycles, power lines, warped by the fisheye "
    "lens.\n"
    "LIGHTING: mixed neon and street light, warm machine glow, candid flash feel, "
    "vivid colours.\n\n"
    "Tokyo street-style fisheye aesthetic, lively candid mood, photorealistic, "
    "high detail. Clearly male, masculine styling, NO makeup, NO earrings." + ID
)

RED_PUNK_MALE = (
    "Restyle the input photo as a bold studio portrait of a MAN with vivid red "
    "punk hair, warm cinematic tone.\n\n"
    "POSE: facing the camera, chin slightly up, confident smirk with closed lips, "
    "one hand raised near the collar.\n"
    "OUTFIT (MALE — masculine ONLY): vivid scarlet-red messy shag haircut with "
    "wispy bangs; a fitted black crewneck or black band tee; a single heavy silver "
    "chain necklace with a small cross pendant. STRICTLY NO bandeau/tube top, NO "
    "feather shrug, NO feminine clothing, NO glam makeup, NO long painted nails, "
    "NO earrings.\n"
    "BACKGROUND: warm seamless brown-to-amber studio gradient.\n"
    "LIGHTING: warm directional studio key, soft glow on the red hair, premium "
    "fashion-cover tone.\n\n"
    "Bold magazine-cover aesthetic, high-fashion punk mood, photorealistic, high "
    "detail. Clearly male, masculine styling." + ID
)


async def main():
    async with AsyncSessionFactory() as session:
        # tokyo_vending male
        p = await session.scalar(select(CustomPreset).where(CustomPreset.key == 'tokyo_vending'))
        if p:
            p.male_prompt = TOKYO_MALE
            print('tokyo_vending: male_prompt set')
        # red_punk male
        p = await session.scalar(select(CustomPreset).where(CustomPreset.key == 'red_punk'))
        if p:
            p.male_prompt = RED_PUNK_MALE
            print('red_punk: male_prompt set')
        # cat_beanie female_only
        p = await session.scalar(select(CustomPreset).where(CustomPreset.key == 'cat_beanie'))
        if p:
            p.female_only = True
            print('cat_beanie: female_only=True')
        await session.commit()
    n = await reload_custom_presets()
    print(f'reloaded, active custom: {n}')


if __name__ == '__main__':
    asyncio.run(main())

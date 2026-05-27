"""Вторая пачка пресетов из uploads (24.05). Провокационные референсы сделаны
brand-safe (без языка/белья/сетки — риск Telegram-бана). Запуск:
    docker compose exec -T -e PYTHONPATH=/app bot python3 /app/scripts/seed_presets_2026_05_24b.py
"""
import asyncio

from app.database.base import AsyncSessionFactory
from app.database.repo.custom_presets import create_custom_preset, list_custom_presets
from app.handlers.image_edit import reload_custom_presets

ID = (
    "\n\nCRITICAL: This is the SAME person from the input photo. Keep the exact "
    "face — same eye shape and colour, nose, lips, jawline, skin tone, hair "
    "colour. Only the scene, outfit and lighting are built around them."
)

PRESETS = [
    # img_1 — fisheye diner, хлопья в невесомости
    dict(key="diner_cereal", label="🥣 Хлопья в невесомости",
         category="🔥 Дерзкие кадры",
         prompt=(
             "Restyle the input photo as a playful fisheye photo inside a retro "
             "American diner. The subject sits at a wooden table eating from a bowl, "
             "colourful cereal loops frozen in mid-air floating all around the frame.\n\n"
             "POSE: leaning toward the wide fisheye lens, holding a spoon near the "
             "mouth, eyes wide and playful, sunglasses pushed up on the head.\n"
             "OUTFIT: casual white graphic tee, silver rings, layered chains, two "
             "braided pigtails.\n"
             "FOREGROUND: colourful breakfast cereal loops (orange, green, yellow) "
             "suspended floating in the air around the subject, a bowl on the table.\n"
             "BACKGROUND: cosy retro diner — red checkerboard floor, red vinyl booths, "
             "vintage posters and stickers on the walls, warm interior light.\n"
             "LIGHTING: warm cosy diner glow, on-camera flash feel, vivid saturated "
             "colours.\n\n"
             "Y2K fisheye aesthetic, surreal floating composition, sharp focus on the "
             "face, photorealistic, high detail." + ID),
         male_prompt=None),

    # img_4 — convenience store glass cooler, motion
    dict(key="store_cooler", label="🥤 У холодильника в маркете",
         category="🌃 Городские сцены",
         prompt=(
             "Restyle the input photo as a candid night photo of the subject opening "
             "the glass door of a convenience-store drinks cooler, seen through the "
             "glass with subtle motion blur and reflections.\n\n"
             "POSE: standing at the open glass cooler door, one hand on the handle, "
             "hood up, looking over the shoulder toward the camera, candid stolen "
             "moment.\n"
             "OUTFIT: layered street style — beige technical coat over a bright orange "
             "hoodie, hood up.\n"
             "BACKGROUND: brightly lit convenience-store cooler stacked with colourful "
             "drinks and cans behind the glass, fluorescent light, faint motion blur "
             "and glass reflections.\n"
             "LIGHTING: cool fluorescent store light, glass reflections, slight long-"
             "exposure motion smear, moody night-store vibe.\n\n"
             "Street-style documentary aesthetic, 35mm look, cool palette, "
             "photorealistic, high detail." + ID),
         male_prompt=None),

    # img_5 — low-angle wide street, hand reach
    dict(key="street_lowangle", label="✋ Дерзкий кадр снизу",
         category="🔥 Дерзкие кадры",
         prompt=(
             "Restyle the input photo as a dramatic ultra-low-angle wide photo on an "
             "empty city avenue. The subject towers over the camera, one hand reaching "
             "down toward the lens, skyscrapers rising behind.\n\n"
             "POSE: standing over the low camera, one arm stretched down toward the "
             "lens with an open hand, confident dominant gaze down, slight lean.\n"
             "OUTFIT: dark denim jacket and cargo skirt-or-trousers covered in white "
             "graffiti-style print, hearts, stars and flowers, layered silver chains.\n"
             "ANATOMY: exactly ONE reaching hand with five fingers, natural foreshorten"
             "ing, no extra limbs.\n"
             "BACKGROUND: empty asphalt avenue lined with glass skyscrapers and trees, "
             "overcast bright sky, strong wide-angle perspective with buildings "
             "leaning.\n"
             "LIGHTING: flat bright daylight, crisp urban tones, high clarity.\n\n"
             "Street-fashion wide-angle aesthetic, bold attitude, photorealistic, high "
             "detail." + ID),
         male_prompt=None),

    # img_7 — chrome wraparound goggles, dramatic sky fisheye
    dict(key="chrome_goggles", label="🕶 Хром-гогглы",
         category="🔥 Дерзкие кадры",
         prompt=(
             "Restyle the input photo as a Y2K fisheye portrait under a dramatic sky. "
             "The subject leans into the wide lens wearing futuristic silver wraparound "
             "goggles, the head and shoulders dominating the frame.\n\n"
             "POSE: leaning into the fisheye lens, chin slightly up, head-and-shoulders "
             "dominating the frame, bold confident expression.\n"
             "OUTFIT: futuristic mirrored silver wraparound goggles, a checkered "
             "red-and-white long-sleeve top, a white bandana scarf at the neck with a "
             "silver heart pendant.\n"
             "BACKGROUND: dramatic deep-blue sky with bright sunlit clouds curving from "
             "fisheye distortion, a low-rise street at the warped horizon.\n"
             "LIGHTING: bright hard sunlight, strong contrast, chrome reflections on "
             "the goggles, vivid blue-and-white palette.\n\n"
             "Y2K cyber-fisheye aesthetic, glossy futuristic vibe, sharp focus, "
             "photorealistic, high detail." + ID),
         male_prompt=None),

    # img_8 — Tokyo vending machine, fisheye, butterfly glasses
    dict(key="tokyo_vending", label="🎌 Токийский автомат",
         category="🌃 Городские сцены",
         prompt=(
             "Restyle the input photo as a fisheye street photo at night by a Japanese "
             "vending machine. The subject leans into the wide lens on a narrow Tokyo "
             "back-street, the machine glowing beside them.\n\n"
             "POSE: leaning toward the fisheye lens with playful attitude, one hand up "
             "near a glowing vending machine, lively expression.\n"
             "OUTFIT: decorated cat-eye glasses with small flowers, a black graphic "
             "tee, silver studded choker, glossy metallic trousers, layered jewellery.\n"
             "BACKGROUND: narrow Japanese street at dusk — a glowing vending machine "
             "with Japanese signage, low buildings, bicycles, power lines, warped by "
             "the fisheye lens.\n"
             "LIGHTING: mixed neon and street light, warm machine glow, candid flash "
             "feel, vivid colours.\n\n"
             "Tokyo street-style fisheye aesthetic, lively candid mood, photorealistic, "
             "high detail." + ID),
         male_prompt=None),

    # img_10 — bubblegum in luxury car, sneakers foreground
    dict(key="bubblegum_car", label="🫧 Пузырь в машине",
         category="🌃 Городские сцены",
         prompt=(
             "Restyle the input photo as a stylish photo of the subject relaxing "
             "across the back seat of a luxury car, blowing a bubblegum bubble, a "
             "sneaker propped up large in the foreground.\n\n"
             "POSE: reclining across the cream leather back seat, legs extended with "
             "one chunky sneaker propped large toward the camera, blowing a pink "
             "bubblegum bubble, tiny tinted sunglasses, relaxed cool attitude.\n"
             "OUTFIT: bright red track trousers, a fitted embroidered floral bustier "
             "top, white-and-red high-top sneakers, hoop earrings, slicked-back hair.\n"
             "ANATOMY: exactly ONE person, TWO legs, TWO arms, natural proportions, "
             "no extra limbs.\n"
             "BACKGROUND: cream leather car interior at night, window with blurred "
             "city lights passing.\n"
             "LIGHTING: cool cinematic night light, soft glow from passing lights, "
             "moody premium tone.\n\n"
             "Streetwear-luxury aesthetic, magazine-editorial mood, photorealistic, "
             "high detail." + ID),
         male_prompt=None),

    # img_11 — courtside glamour, fur coat (DUAL)
    dict(key="courtside_glam", label="💎 Звезда на трибуне",
         category="✨ Стиль и свет",
         prompt=(
             "Restyle the input photo as a glamorous candid celebrity photo in the "
             "front row of a sports arena. The subject sits among the crowd with "
             "flawless styling, caught mid-moment.\n\n"
             "POSE: seated in the front row, one hand softly touching the hair, calm "
             "confident gaze slightly off-camera, effortless star presence.\n"
             "OUTFIT: a luxurious cream shearling-and-leather coat with a black collar, "
             "glamorous soft makeup, subtle jewellery.\n"
             "BACKGROUND: blurred sports-arena crowd and seating behind, candid "
             "paparazzi feel, shallow depth of field.\n"
             "LIGHTING: bright even arena light, clean glamorous tone, sharp focus on "
             "the face.\n\n"
             "Celebrity candid editorial aesthetic, polished and premium, "
             "photorealistic, high detail." + ID),
         male_prompt=None),

    # img_12 — white light-box void, goth (brand-safe)
    dict(key="white_void", label="🤍 Белый бокс",
         category="🎨 Дерзкие портреты",
         prompt=(
             "Restyle the input photo as a striking fisheye photo inside a glowing "
             "all-white light-box room. The subject leans toward the wide lens, "
             "long straight black hair with blunt bangs framing the face.\n\n"
             "POSE: leaning forward into the fisheye lens from the centre of a bright "
             "white void, calm intense gaze, hands resting on the knees.\n"
             "OUTFIT: long straight jet-black hair with blunt fringe, a chunky silver "
             "chain choker, a fitted black top, opaque black tights, lace-up platform "
             "boots. Edgy but fully clothed, tasteful.\n"
             "BACKGROUND: a completely white glowing padded light-box room, soft skylight "
             "panel above, no other objects — pure white void.\n"
             "LIGHTING: bright even white studio glow filling the box, soft shadows, "
             "clean high-key tone with cool cast.\n\n"
             "High-fashion editorial void aesthetic, fisheye distortion, dark-glam "
             "mood, photorealistic, high detail." + ID),
         male_prompt=None),

    # img_13 — red hair punk + gothic jewelry (brand-safe, no tongue)
    dict(key="red_punk", label="🔥 Рыжий панк",
         category="🎨 Дерзкие портреты",
         prompt=(
             "Restyle the input photo as a bold studio beauty portrait with vivid red "
             "punk hair and layered gothic jewellery, warm cinematic tone.\n\n"
             "POSE: facing the camera, chin slightly up, confident smirk with closed "
             "lips, both hands raised near the neck adjusting the jewellery, long "
             "black-painted nails.\n"
             "OUTFIT: vivid scarlet-red messy shag haircut with wispy bangs, smoky eye "
             "makeup, a chunky ornate metal letter-choker on layered silver and gold "
             "chains with a small cross pendant, a fitted black printed bandeau top, a "
             "fluffy white feather shrug on the shoulders.\n"
             "BACKGROUND: warm seamless brown-to-amber studio gradient.\n"
             "LIGHTING: warm directional studio key, soft glow on red hair, premium "
             "fashion-cover tone.\n\n"
             "Bold magazine-cover beauty aesthetic, high-fashion punk-glam mood, "
             "photorealistic, high detail." + ID),
         male_prompt=None),

    # img_19 — cat-ear beanie, teal wall, puffer (brand-safe, no tongue)
    dict(key="cat_beanie", label="🐱 Кошачья шапка",
         category="🌃 Городские сцены",
         prompt=(
             "Restyle the input photo as a cool street-style photo against a solid "
             "teal wall. The subject poses confidently in a chunky knit cat-ear beanie "
             "and a black puffer jacket.\n\n"
             "POSE: standing side-on against the wall, body slightly arched, looking "
             "back toward the camera with a cool confident expression, one hand "
             "extended, long almond nails.\n"
             "OUTFIT: a cream chunky-knit beanie with cat ears, a glossy black cropped "
             "puffer jacket, a mustard-yellow plush bag on the back, cream ribbed "
             "leggings.\n"
             "BACKGROUND: a clean solid deep-teal painted wall, minimal, lots of "
             "negative space.\n"
             "LIGHTING: soft natural daylight, gentle shadow on the teal wall, clean "
             "saturated tone.\n\n"
             "Minimalist streetwear-editorial aesthetic, bold teal palette, "
             "photorealistic, high detail." + ID),
         male_prompt=None),
]


async def main():
    async with AsyncSessionFactory() as session:
        existing = {p.key for p in await list_custom_presets(session, only_enabled=False)}
        added = []
        for p in PRESETS:
            if p["key"] in existing:
                print(f"skip (exists): {p['key']}")
                continue
            await create_custom_preset(
                session, key=p["key"], label=p["label"], prompt=p["prompt"],
                male_prompt=p.get("male_prompt"), quality_mode="quality",
                category=p["category"], female_only=False,
            )
            added.append(p["key"])
        await session.commit()
    n = await reload_custom_presets()
    print(f"ADDED: {added}")
    print(f"active custom count: {n}")


if __name__ == "__main__":
    asyncio.run(main())

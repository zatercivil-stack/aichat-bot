"""Сидинг новых пресетов из uploads (24.05.2026). Запуск:
    docker compose exec -T bot python3 scripts/seed_presets_2026_05_24.py
Вставляет в custom_presets (idempotent по key через on-conflict skip) и зовёт
reload_custom_presets(). Превью НЕ генерит (это платный отдельный шаг)."""
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
    # 1. Колодец небоскрёбов (look-up vertigo) — image_6
    dict(
        key="sky_well", label="🏙 Колодец небоскрёбов",
        category="🌃 Городские сцены",
        prompt=(
            "Restyle the input photo as a dramatic extreme worm's-eye-view photo "
            "looking straight up. The subject is seated high above the camera as if "
            "on a glass ledge, looking down into the lens, surrounded by a ring of "
            "towering skyscrapers that radiate inward toward a bright cloudy blue sky "
            "at the centre of the frame.\n\n"
            "POSE: subject seated, legs framing the top of the shot, leaning slightly "
            "forward to look down at the camera with a calm dominant gaze, one hand "
            "near the chin.\n"
            "OUTFIT: structured houndstooth check suit (blazer + trousers), modern "
            "editorial tailoring.\n"
            "BACKGROUND: vertiginous circle of glass-and-steel skyscrapers converging "
            "toward a vivid blue sky scattered with white clouds, strong forced "
            "perspective, buildings leaning inward at the frame edges.\n"
            "LIGHTING: bright natural daylight from the sky centre, crisp shadows on "
            "the towers, high clarity.\n\n"
            "Surreal vertigo composition, ultra-wide look-up angle, vivid saturated "
            "colours, 8k, photorealistic, high detail, fashion-editorial mood." + ID
        ),
        male_prompt=None,
    ),
    # 2. Бросок костей (gambler / dice portal) — image_16 [male-led, unisex]
    dict(
        key="dice_throw", label="🎲 Бросок костей",
        category="🎭 Сцены-образы",
        prompt=(
            "Restyle the input photo as a cinematic low-angle shot seen through a "
            "circular glowing portal/tunnel opening. The subject leans through the "
            "round frame from a dark background, one hand reaching forward toward a "
            "scatter of glowing dice on a wet metallic floor in the foreground.\n\n"
            "POSE: subject leaning in from the back of a dark circular tunnel, dark "
            "sunglasses, focused intense expression, one hand stretched toward the "
            "camera reaching for the dice, fingers stacked with chunky silver rings.\n"
            "OUTFIT: glossy red windbreaker jacket, chunky silver statement rings.\n"
            "FOREGROUND: 7-8 pale glow-in-the-dark dice scattered on a dark wet "
            "reflective floor, sharp focus on the nearest dice and the reaching hand.\n"
            "ANATOMY: exactly ONE person, ONE reaching hand with five fingers, no "
            "extra limbs or duplicated dice-hands.\n"
            "BACKGROUND: dark circular metallic portal framing the subject, moody "
            "shadow, faint rim light on the metal ring.\n"
            "LIGHTING: dramatic low-key, cool steel tones with a warm rim on the face, "
            "cinematic chiaroscuro.\n\n"
            "High-contrast gambler aesthetic, 35mm cinematic look, photorealistic, "
            "high detail, mysterious mood." + ID
        ),
        male_prompt=None,
    ),
    # 3. С добберманами у суперкара — image_21 [male-led, unisex]
    dict(
        key="doberman_boss", label="🐕 Босс с добберманами",
        category="🎭 Сцены-образы",
        prompt=(
            "Restyle the input photo as a powerful luxury editorial portrait. The "
            "subject stands confidently on an ornate Persian rug holding the rope "
            "leashes of two alert Doberman dogs, a red classic supercar parked behind "
            "in a concrete studio-garage.\n\n"
            "POSE: subject standing centre, feet planted, both hands holding the rope "
            "leashes of two black-and-tan Dobermans sitting alert on either side, calm "
            "commanding expression facing the camera.\n"
            "OUTFIT: black technical tracksuit with reflective white piping lines "
            "running along the seams (subtly glowing under light).\n"
            "ANATOMY: exactly ONE person, TWO arms, TWO hands each holding one leash; "
            "exactly TWO separate Doberman dogs, anatomically correct, no extra legs.\n"
            "BACKGROUND: a red classic 80s supercar parked behind, raw concrete garage "
            "wall, ornate red-patterned Persian rug on the floor.\n"
            "LIGHTING: moody grey studio light, soft directional shadow, premium "
            "cinematic tone.\n\n"
            "Powerful luxury-editorial mood, 8k, photorealistic, high detail, high "
            "contrast." + ID
        ),
        male_prompt=None,
    ),
    # 4. Кофе по-джентльменски (gold aviators, high angle) — image_17 [male-led]
    dict(
        key="gold_coffee", label="☕ Кофе по-джентльменски",
        category="✨ Стиль и свет",
        prompt=(
            "Restyle the input photo as a stylish high-angle editorial photo shot "
            "looking down at the subject from above. The subject tilts their head up "
            "toward the camera while sipping from a teal ceramic cup, wearing oversized "
            "gold-rim aviator sunglasses with warm amber lenses.\n\n"
            "POSE: subject seen from a high angle looking up into the lens, holding a "
            "teal espresso cup to the lips with a ring-clad hand, confident relaxed "
            "expression.\n"
            "OUTFIT: brown houndstooth tweed blazer over a deep burgundy shirt, gold "
            "signet ring, oversized gold aviator sunglasses with amber lenses.\n"
            "BACKGROUND: vintage interior with teal-green tiled walls and a sunlit "
            "window, warm diffused daylight.\n"
            "LIGHTING: soft warm window light, gentle highlights on the gold frames "
            "and skin, cosy vintage tone.\n\n"
            "Editorial street-style mood, 35mm film look, warm colour grade, "
            "photorealistic, high detail." + ID
        ),
        male_prompt=None,
    ),
    # 5. Дайнер-постер (DUAL: female blue / male red) — image_20 + image_18
    dict(
        key="diner_poster", label="🥤 Дайнер-постер",
        category="🔥 Дерзкие кадры",
        prompt=(
            "Restyle the input photo as a retro diner editorial photo. The subject "
            "stands inside a small tiled diner corner, both arms stretched wide "
            "touching both walls, creating strong symmetric geometric framing; a "
            "milkshake glass and a retro diner sign sit on the table in the foreground.\n\n"
            "POSE: standing confidently between two tiled walls, both arms stretched "
            "out wide touching the walls, mouth slightly open with a soda straw, "
            "playful bold attitude.\n"
            "OUTFIT: fitted pastel-blue baby tee with a glittery retro wordmark print, "
            "white rectangular sunglasses, a plaid flannel shirt tied around the waist, "
            "small shoulder bag, hair in two braids.\n"
            "FOREGROUND: a tall glass of vanilla milkshake and a retro diner menu card "
            "on a pale-blue table.\n"
            "BACKGROUND: glossy pale-blue square-tiled diner walls, clean symmetric "
            "composition.\n"
            "LIGHTING: bright clean studio flash, vivid saturated blue palette, sharp "
            "focus.\n\n"
            "Retro-futuristic diner aesthetic, magazine-cover styling, 35mm look, high "
            "contrast, photorealistic, high detail." + ID
        ),
        male_prompt=(
            "Restyle the input photo as a high-fashion retro diner editorial portrait "
            "of a MAN in a full monochrome RED outfit. He stands confidently inside a "
            "small turquoise-tiled diner corner, both arms stretched wide touching both "
            "walls, creating strong symmetric geometric framing.\n\n"
            "POSE: standing between two turquoise-tiled walls, both arms stretched out "
            "wide touching the walls, confident rebellious cinematic attitude.\n"
            "OUTFIT (MALE — masculine fit, NO feminine elements): red tailored jacket, "
            "red turtleneck, red trousers, red leather gloves, red oval sunglasses.\n"
            "FOREGROUND: a tall glass of red milkshake and a retro diner sign on a "
            "turquoise table.\n"
            "BACKGROUND: glossy turquoise square-tiled walls, clean symmetric "
            "composition.\n"
            "LIGHTING: cinematic studio light, bold complementary red-vs-turquoise "
            "palette, sharp focus.\n\n"
            "Retro-futuristic diner aesthetic, magazine-cover styling, 35mm look, high "
            "contrast, photorealistic, high detail. Clearly male, NO makeup, NO "
            "earrings, NO feminine styling." + ID
        ),
    ),
    # 6. В автомате с игрушками (claw machine) — image_9
    dict(
        key="claw_machine", label="🧸 В автомате с игрушками",
        category="🌒 Магия и стихии",
        prompt=(
            "Restyle the input photo as a dreamy neon photo of the subject sitting "
            "INSIDE a claw-machine arcade cabinet, half-surrounded by colourful plush "
            "toys, the chrome claw and hot-pink machine frame around them, shot from "
            "outside through the glass.\n\n"
            "POSE: subject sitting among plush toys inside the glass cabinet, looking "
            "calmly out through the glass at the camera, soft serene expression, one "
            "hand resting on the toys.\n"
            "OUTFIT: casual white tee under a dark jacket, layered chain necklace.\n"
            "SURROUNDINGS: piles of colourful plush toys (orange, teal, white) filling "
            "the lower frame, a chrome claw grabber hanging above, hot-pink metal "
            "machine frame, arcade lights.\n"
            "LIGHTING: vivid pink-and-magenta neon glow from the machine ceiling "
            "lights, soft reflections on the glass, playful dreamy mood.\n\n"
            "Surreal cute arcade aesthetic, vivid pink palette, shallow depth of field "
            "with blurred plushies in the foreground, photorealistic, high detail." + ID
        ),
        male_prompt=None,
    ),
    # 7. За пультом (DJ floor setup) — image_14
    dict(
        key="dj_booth", label="🎧 За пультом диджея",
        category="🎭 Сцены-образы",
        prompt=(
            "Restyle the input photo as an editorial studio photo of the subject "
            "sitting on the floor playing on a professional DJ controller, large "
            "headphones on, legs spread with chunky platform boots framing the "
            "foreground.\n\n"
            "POSE: sitting on a seamless studio floor, large over-ear headphones on, "
            "leaning back on one hand, the other hand on the DJ controller, confident "
            "relaxed attitude, chunky platform boots in the foreground.\n"
            "OUTFIT: printed mesh long-sleeve top, printed cargo trousers, layered "
            "silver chains, massive black platform boots.\n"
            "FOREGROUND: a Pioneer-style DJ controller with cables, knobs and jog "
            "wheels, sharp focus.\n"
            "BACKGROUND: clean grey seamless studio backdrop, soft gradient shadow.\n"
            "LIGHTING: soft even studio key light, subtle rim, modern fashion-editorial "
            "tone.\n\n"
            "Underground club-culture aesthetic, sharp focus, cool desaturated palette, "
            "photorealistic, high detail." + ID
        ),
        male_prompt=None,
    ),
    # 8. Забег по супермаркету (motion blur cart) — image_3
    dict(
        key="supermarket_sweep", label="🛒 Забег по супермаркету",
        category="🌃 Городские сцены",
        prompt=(
            "Restyle the input photo as a vintage 70s-film candid photo of the subject "
            "pushing a chrome shopping cart fast through a brightly lit supermarket "
            "aisle, with strong radial motion blur on the shelves conveying speed.\n\n"
            "POSE: leaning forward over the handle of a chrome shopping cart, pushing "
            "it toward the camera with momentum, retro tinted sunglasses, lips parted "
            "in a playful gasp.\n"
            "OUTFIT: pastel-yellow knit short-sleeve cardigan, retro styling, small "
            "hoop earrings.\n"
            "BACKGROUND: supermarket aisles packed with colourful groceries, strong "
            "radial zoom motion blur radiating outward, only the subject and cart in "
            "sharp focus.\n"
            "LIGHTING: warm fluorescent supermarket light, nostalgic film tone.\n\n"
            "Vintage 70s film-photo aesthetic, warm Kodak grade, film grain, strong "
            "sense of speed, photorealistic, high detail." + ID
        ),
        male_prompt=None,
    ),
    # 9. Вульф-кат портрет (clean beauty close-up) — image_15
    dict(
        key="wolfcut_portrait", label="🕶 Вульф-кат портрет",
        category="✨ Стиль и свет",
        prompt=(
            "Restyle the input photo as a clean modern beauty close-up portrait. The "
            "subject has a trendy textured wolf-cut hairstyle with wispy bangs and "
            "wears transparent clear-frame rectangular glasses, against a soft pale "
            "studio background.\n\n"
            "POSE: head-and-shoulders close-up, head slightly tilted, calm confident "
            "gaze through the clear glasses, relaxed lips.\n"
            "OUTFIT: navy-and-white raglan baby tee with a retro varsity print, thin "
            "chain necklace.\n"
            "HAIR: layered shaggy wolf-cut with choppy textured ends and a wispy "
            "fringe.\n"
            "BACKGROUND: clean soft pale-grey studio backdrop, minimal.\n"
            "LIGHTING: soft even beauty light, gentle shadow, fresh clean tone, sharp "
            "focus on the eyes.\n\n"
            "Modern Gen-Z beauty-editorial aesthetic, clean and crisp, photorealistic, "
            "high detail." + ID
        ),
        male_prompt=None,
    ),
    # 10. Граффити-лифт (drinking in tagged elevator) — image_2
    dict(
        key="graffiti_elevator", label="🛗 Граффити-лифт",
        category="🌃 Городские сцены",
        prompt=(
            "Restyle the input photo as a moody cinematic photo of the subject "
            "standing inside an old graffiti-covered elevator, sipping from a small "
            "ceramic cup, wearing narrow sunglasses with a beaded chain.\n\n"
            "POSE: standing in the corner of a worn metal elevator, sipping from a "
            "small white ceramic mug held in one hand, leaning casually against the "
            "wall, cool detached expression behind narrow sunglasses.\n"
            "OUTFIT: grey oversized blazer over a checkerboard knit top, narrow black "
            "sunglasses with a beaded chain.\n"
            "BACKGROUND: vintage stainless-steel elevator interior covered in marker "
            "tags and red spray-paint graffiti, worn metal panels, a button panel on "
            "the side.\n"
            "LIGHTING: moody warm overhead elevator light with a greenish-yellow tint, "
            "gritty cinematic shadows.\n\n"
            "Urban-grunge editorial aesthetic, 35mm film grain, desaturated "
            "green-yellow grade, photorealistic, high detail." + ID
        ),
        male_prompt=None,
    ),
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
                session,
                key=p["key"], label=p["label"], prompt=p["prompt"],
                male_prompt=p.get("male_prompt"), quality_mode="quality",
                category=p["category"], female_only=False,
            )
            added.append(p["key"])
        await session.commit()
    n = await reload_custom_presets()
    print(f"ADDED: {added}")
    print(f"reload_custom_presets active count: {n}")


if __name__ == "__main__":
    asyncio.run(main())

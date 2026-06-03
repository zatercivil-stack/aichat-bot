from __future__ import annotations

import asyncio
import io
import time
from io import BytesIO

# Сколько секунд после голого upload фото мы считаем "очевидно edit-намерением".
# По истечении: ImageEditState.waiting_prompt сбрасывается, текст уходит в обычный чат.
PHOTO_AUTO_EDIT_TTL_S = 300  # 5 минут

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import MODEL_COSTS, add_credits, deduct_credits, grant_daily_if_needed
from app.database.repo.events import log_event_bg
from app.database.repo.users import get_user, is_paid_user
from app.keyboards.main import (
    PE_AU_GOLD, PE_BACK, PE_CARD, PE_CITY, PE_CLIP, PE_CROSS, PE_FIRE, PE_FRAME,
    PE_GIFT, PE_HANDSHAKE, PE_HOME, PE_LEFT, PE_MONEY, PE_MOON_DARK, PE_PAINT,
    PE_RIGHT, PE_ROBOT, PE_SPARKLES, PE_THEATER, PE_UNLOCK,
    buy_or_invite_kb,
)
from app.services import fal_image_service, image_edit_service, watermark
from app.states import ImageEditState
from app.utils import premium_text

logger = structlog.get_logger(__name__)
router = Router()

EDIT_COST = MODEL_COSTS['image_edit']             # 200 base (quality)
EDIT_COST_FAST = MODEL_COSTS['image_edit_fast']   # 40 (mini)
EDIT_COST_PRECISE = MODEL_COSTS['image_edit_precise']  # 300 base (edit + auto face-swap)
FACE_SWAP_COST = MODEL_COSTS['face_swap']         # 200 base
# Эффективные цены пересчитываются через app.services.pricing.effective_cost()
# с учётом активного launch-промо. Используем _cost_for() и effective_cost везде.


# ────────────────────────────────────────────────────────────
# Авто-детектор сложности правки. Используется когда юзер набрал prompt
# напрямую без выбора кнопки «Быстро/Качество». По умолчанию выбираем quality,
# потому что подмена identity в платном продукте бесит больше, чем переплата.
# ────────────────────────────────────────────────────────────
_QUALITY_KEYWORDS = (
    # лицо/тело — identity критична
    'лиц', 'face', 'кожа', 'skin', 'тело', 'body', 'фигур',
    'портрет', 'portrait', 'волос', 'hair',
    # одежда — структурные правки, mini часто меняет человека
    'одеж', 'костюм', 'платье', 'рубашк', 'футболк', 'куртк',
    'брюк', 'джинс', 'юбк', 'шорт', 'обувь', 'ботин', 'топик', 'топ ',
    # HD / детализация / pixel-perfect
    'детал', 'detail', 'sharp', 'четч', 'hd ', 'hd-', 'hd_',
    'улучш', 'enhance',
)
_FAST_KEYWORDS = (
    # стиль/арт
    'стил', 'style', 'anime', 'аниме', 'мульт', 'cartoon',
    'акварел', 'watercolor', 'масл', 'oil', 'pixar', 'ghibli', 'sketch',
    # фон
    'фон', 'background', ' bg',
    # цвет/тон (общая правка)
    'цвет', 'color', 'тон фото', 'lighting',
    # объекты не-люди
    'убери человек', 'remove person',
)


def _detect_quality_mode(prompt: str) -> str:
    """Единый режим — всегда 'quality' (nano-banana-2 @ 2K PNG).
    Fast (gpt-5-image-mini) и precise убраны: один качественный вариант, без
    выбора и без риска подмены identity дешёвой моделью."""
    return 'quality'


# Карта: пресет → режим.
#   fast    — gpt-5-image-mini, для фона/удаления (40 кр)
#   quality — nano-banana-2 @ 2K PNG, identity через identity-lock промпт
# precise УБРАН 22.05.2026: давал лесенку (face-swap пересохранял в JPEG 4:2:0).
# Все стилевые/сценовые пресеты теперь quality — чистый 2K PNG одним проходом.
PRESET_QUALITY_MAP: dict[str, str] = {
    # Стилизации (свет/мудборд) — лицо обычно в том же ракурсе, quality достаточно
    'cinema':        'quality',
    'vogue':         'quality',
    'anime':         'quality',
    'cyberpunk':     'quality',
    'golden_hour':   'quality',
    'retro_90s':     'quality',
    # Полнокадровые сцены — раньше precise; теперь quality (face-swap убран 22.05)
    'paris_night':    'quality',
    'rooftop_wine':   'quality',
    'vintage_car':    'quality',
    'lion_steps':     'quality',
    'tennis_70s':     'quality',
    'warrior':        'quality',
    'subway_bouquet': 'quality',
    'rainy_taxi':     'quality',
    'vintage_rain':   'quality',
    'berlin_metro':   'quality',
    'giant_city':     'quality',
    'fisheye_paris':  'quality',
    'y2k_attitude':   'quality',
    # Y2K / Fisheye / Bold-Red серия
    'fisheye_porsche':   'quality',
    'y2k_fridge':        'quality',
    'macro_watermelon':  'quality',
    'night_racing':      'quality',
    'bold_red_studio':   'quality',
    'magazine_vibe':     'quality',
    # Арт-серия (добавлено 21.05.2026 — концепт-арт / редакторские)
    'doll_porcelain':    'quality',   # close-up beauty, без смены сцены
    'clockwork_text':    'quality',   # лицо + текст-оверлей + корона
    'red_lust':          'quality',   # монохром-красный close-up
    'cyber_neon':        'quality',
    'marble_punk':       'quality',
    'graffiti_zine':     'quality',
    'skull_saint':       'quality',
    'underwater_red':    'quality',
    'goldfish_dream':    'quality',
    'korean_red_wall':   'quality',
    # Утилитарные правки
    'change_clothes': 'quality',  # одежда меняется, лицо обычно ок (есть кнопка «точнее»)
    'change_bg':     'quality',
    'remove_bg':     'quality',
}


# Пресеты, которые ЗАПРЕЩЕНО использовать для массовых рассылок (broadcast).
# Они допустимы в галерее (юзер сам выбирает — окей), но как face рассылки
# дают плохой первый сигнал: грубый жест / провокация → жалобы и блок-бан.
# Закрытый список — проверяется в _do_broadcast_by_key и в admin-форме.
# marble_punk — мраморная скульптура показывает «фак» (24.05: владелец явно
# запретил после неудачной re-engage рассылки).
NO_BROADCAST_PRESETS: set[str] = {'marble_punk'}

# Нативная вотермарка для бесплатных юзеров (24.05). Вместо уродливого corner-
# overlay просим модель ВПИСАТЬ «@NeyraChatBot» в сцену как органичный элемент
# (граффити/вывеска/постер/неон). Эффект: виральнее (юзеры шарят красивый кадр,
# не кропают), не убрать без потери фона, премиум-вид → платить хочется за
# чистый кадр. Добавляется в конец промпта ТОЛЬКО для не-платящих.
_NATIVE_WATERMARK_INSTRUCTION = (
    '\n\nNATIVE BRAND PLACEMENT (subtle, in-scene): also render the text '
    '"@NeyraChatBot" as a small NATURAL element in the BACKGROUND — graffiti, '
    'a poster, a sticker, a street sign, a neon sign, a chalkboard, etc. — '
    'matching the style, colour and surface of the scene (e.g. red marker for '
    'elevator graffiti, a sign on a pole for a street scene). The text MUST '
    'read EXACTLY "@NeyraChatBot" with no typos or missing letters. Place it '
    'peripherally, NEVER covering the subject\'s face, hands or body. Readable '
    'but not dominant. If the scene has no natural surface for text (extreme '
    'face close-up, underwater, plain sky) — skip it, do not force.'
)


# ────────────────────────────────────────────────────────────
# Пагинация меню пресетов. С 19 шаблонами keyboard слишком длинный,
# поэтому разносим на 4 страницы по теме. Заголовок + цены показываем
# в тексте сообщения, навигация в нижнем ряду keyboard.
# ────────────────────────────────────────────────────────────
PRESET_PAGES: list[dict] = [
    {
        'title': '✨ Стиль и портрет',
        'subtitle': 'Новая атмосфера на том же кадре: стиль, свет, смена одежды и фона.',
        'price_hint': '~200 кр · 1 минута',
        'items': ['cinema', 'vogue', 'anime', 'cyberpunk', 'golden_hour',
                  'retro_90s', 'change_clothes', 'remove_bg'],
    },
    {
        'title': '🌃 Сцены и города',
        'subtitle': 'Улица, метро, авто, перевоплощения, смена локации. Лицо <b>в точности твоё</b>.',
        'price_hint': '~200 кр · 1-2 минуты',
        'items': [
            'trunk_pov',
            'paris_night', 'rooftop_wine', 'subway_bouquet', 'rainy_taxi',
            'vintage_rain', 'berlin_metro', 'vintage_car', 'lion_steps',
            'tennis_70s', 'warrior', 'fisheye_paris', 'y2k_attitude',
            'giant_city', 'change_bg',
        ],
    },
    {
        'title': '🔥 Дерзкие и яркие',
        'subtitle': 'Дерзкие кадры, обложки, арт-портреты, неон и стихии. Бунт и характер.',
        'price_hint': '~200 кр',
        'items': [
            'suit_smoke',
            'fisheye_porsche', 'y2k_fridge', 'macro_watermelon', 'night_racing',
            'bold_red_studio', 'magazine_vibe', 'doll_porcelain',
            'graffiti_zine', 'clockwork_text', 'korean_red_wall', 'cyber_neon',
            'skull_saint', 'red_lust', 'underwater_red', 'goldfish_dream',
        ],
    },
]


def _cost_for(mode: str) -> int:
    """Базовая цена (синхронно). Для реальной цены с promo используй effective_cost_for()."""
    if mode == 'fast':
        return EDIT_COST_FAST
    if mode == 'precise':
        return EDIT_COST_PRECISE
    return EDIT_COST


async def effective_cost_for(mode: str) -> int:
    """Цена с учётом promo. Fast не попадает под promo (уже дёшево)."""
    from app.services.pricing import effective_cost
    if mode == 'fast':
        return EDIT_COST_FAST
    if mode == 'precise':
        return await effective_cost('image_edit_precise')
    return await effective_cost('image_edit')


async def effective_face_swap_cost() -> int:
    from app.services.pricing import effective_cost
    return await effective_cost('face_swap')


# ────────────────────────────────────────────────────────────
# Пресеты — готовые промпты под типичные сценарии редактирования.
# label показывается на кнопке, prompt = None означает "спросить уточнение у юзера".
# ────────────────────────────────────────────────────────────
# Мужской режим: добавляется к промпту когда user.gender == 'male'. Гасит
# дефолтные женские стайл-маркеры. Для пресетов с жёстко прописанными женскими
# элементами (косички, пучки, язык, серьги, платья) одного суффикса мало —
# там работает _MALE_PRESET_OVERRIDES (полностью мужской промпт).
_MALE_STYLE_SUFFIX = (
    '\n\nIMPORTANT — THE SUBJECT IS A MAN. Render him clearly male, masculine '
    'styling ONLY. STRICTLY FORBIDDEN: lipstick, lip gloss, glossy/shiny lips, any '
    'feminine makeup, eyeshadow glam, false eyelashes; EARRINGS of any kind and any '
    'dangly jewelry; braids, pigtails, hair clips, barrettes, hair pins, hair bows, '
    'space buns, rose buns, any feminine hairstyle; dresses, gowns, skirts, frilled '
    'or ruffled blouses or any feminine clothing; tongue sticking out; feminine pose '
    'or feminine facial features. USE INSTEAD: masculine short/slicked hairstyle, '
    'masculine clothing fitting the scene, natural masculine skin, confident '
    'masculine demeanor. He must read unmistakably as a man — no earrings at all.'
)

# Полностью мужские версии промптов для пресетов, где женские элементы вшиты в
# сам промпт / слишком сильны (по фидбэку владельца). Identity-lock добавит
# edit_image — здесь только сцена, уже мужская.
_MALE_PRESET_OVERRIDES: dict[str, str] = {
    'suit_smoke': (
        'Editorial portrait of a confident MAN standing against a distressed grey concrete wall '
        'in an abandoned industrial space, three-quarter body framing. Oversized navy-black '
        'tailored overcoat with wide lapels over a crisp white shirt and a thin black necktie, '
        'folded cuffs with a subtle pinstripe lining, black leather belt with a silver buckle, '
        'dark trousers — oversized menswear tailoring. One hand raised to his lips holding a thin '
        'lit cigarette with a faint wisp of smoke. Masculine short slicked or tousled hair, strong '
        'jaw, cool detached gaze turned off-camera. Soft diffused overcast daylight, low contrast, '
        'desaturated cool palette, 50-85mm lens, shallow depth of field, fine film grain, '
        'quiet-luxury grunge editorial. Clearly male, NO makeup, NO lipstick, NO earrings.'
    ),
    'trunk_pov': (
        'Cinematic wide-angle scene shot from INSIDE an open car trunk, looking up and out, the '
        'burgundy trunk carpet filling the foreground. A MAN in a burgundy 1970s suit jacket over '
        'a small-patterned paisley shirt, with dark wavy medium-length hair and a moustache, leans '
        'over the open trunk: one hand raised gripping the trunk lid at the top of the frame, the '
        'other reaching down toward the camera, looming large in exaggerated fisheye perspective. '
        'Intense, slightly menacing expression looking down into the trunk. Behind him: bright blue '
        'sky with wispy clouds, an American strip-mall with a red "RETAIL OUTLET" sign and street '
        'lamps; a woman in a bright orange dress by the storefront with her hands raised to her head '
        'in shock. Ultra-wide fisheye lens, strong barrel distortion, harsh bright midday sun, '
        'highly saturated colour, 1970s crime-comedy film aesthetic, photorealistic, slight film '
        'grain. Clearly male, masculine, NO makeup, NO earrings.'
    ),
    'fisheye_porsche': (
        'A confident MAN leaning on the hood of a sports coupe, shot with a wide '
        'low angle — he is large in the foreground, the car recedes behind him. '
        'Masculine cool outfit: leather jacket or open shirt with jeans (NO feminine '
        'clothing, NO dress). Strong relaxed pose, slight smirk. Warm evening light, '
        'reflections on the metal, automotive-magazine editorial look. Clearly male.'
    ),
    'y2k_attitude': (
        'Y2K early-2000s aesthetic portrait of a MAN with attitude. Masculine short '
        'or spiky 2000s hairstyle — NO hair clips, NO barrettes, NO pigtails, NO '
        'braids. Baggy y2k streetwear, tinted narrow sunglasses, chunky silver chain. '
        'On-camera flash look, slight grain, bold confident vibe. Clearly male, NO '
        'makeup, NO earrings.'
    ),
    'magazine_vibe': (
        'Y2K magazine cover portrait of a MAN, VIBE-cover style with bold layout and '
        'cover text blocks. Masculine short hairstyle — NO braids, NO pigtails, NO '
        'clips. Streetwear, confident frontal pose, glossy magazine print aesthetic. '
        'Clearly male, NO makeup, NO earrings.'
    ),
    'night_racing': (
        'Restyle the input photo as a moody candid night street photo of a MAN. He '
        'walks confidently down an urban road at night, body slightly turned, mouth '
        'softly open mid-stride, eyes catching neon light. The shot feels stolen, '
        'intimate.\n\n'
        'Outfit (MALE — STRICTLY NO crop top, NO biker shorts, NO leggings, NO '
        'feminine clothing): oversized red-and-black varsity racing jacket worn over '
        'a plain fitted black t-shirt, slim black or dark indigo straight-leg jeans '
        '(NOT shorts), layered silver chains, slim red cat-eye sunglasses pushed up '
        'on the head. One hand holds a red plastic party cup. Masculine short to '
        'medium messy windblown hair — NO braids, NO clips.\n\n'
        'Background: dark urban night street, blurred warm headlights forming bokeh '
        'in distance, faint neon glow reflecting on wet asphalt, subtle atmospheric '
        'haze.\n\n'
        '35mm film grain, high contrast, intentionally slightly underexposed, Y2K '
        'party-street aesthetic, candid composition, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — '
        'eye shape and colour, nose, lips, jawline, skin tone, hair colour. Clearly '
        'male, NO makeup, NO lipstick, NO earrings, NO crop top, NO biker shorts.'
    ),
    'clockwork_text': (
        'Hyperreal editorial PROFILE portrait of a MAN with GLOWING ORANGE handwritten '
        'GRAFFITI TEXT across his face, neck and jacket: "CLOCKWORK" boldly on the '
        'cheek, "ORANGE" on the jaw, plus smaller "OUT", "RANCK", "MNYW", "OneDFTA" '
        'scattered around; all glowing warm orange-amber. A tall ornate crown '
        'encrusted with orange rhinestones and a bright orange neon underband on his '
        'head. Dark collared jacket covered in the same glowing orange text. Near-'
        'black background, dramatic orange chiaroscuro. Clearly male — NO earrings, '
        'NO makeup, masculine.'
    ),
    'cyber_neon': (
        'Gritty cyberpunk concept-art portrait of a MAN, low-angle close-up. '
        'Confident edgy expression, closed mouth or slight smirk — NO tongue sticking '
        'out. Short dark messy or undercut masculine hair — NO buns, NO braids. Pale '
        'skin, sharp masculine features, light stubble. Dark olive nylon jacket over a '
        'black tee, a silver chain. Background: vivid purple and magenta neon, blown-'
        'out vertical light behind the head, gritty industrial scene. Cinematic, '
        'defiant, masculine. NO earrings, NO lip gloss, NO makeup.'
    ),
    'marble_punk': (
        'Stylized 3D WHITE MARBLE SCULPTURE of a MAN — the whole figure carved from '
        'matte white marble. Aggressive snarl, bared teeth, furrowed brows. ONE hand '
        'raised with the middle finger extended; the OTHER holds a clear plastic cup '
        'of red slushie with a straw. Short slicked-back marble hair — NO buns, NO '
        'roses. Bold red cat-eye sunglasses. Masculine high-collar marble shirt — NO '
        'frills, NO ruffles, NO blouse. Vivid flat red background. Surreal pop-art '
        'sculpture, masculine, satirical. NO earrings.'
    ),
    'graffiti_zine': (
        'Gritty vintage BLACK-AND-WHITE photo of a MAN with heavy white street-art '
        'graffiti overlay. Manic intense expression — mouth may be open mid-shout but '
        'NO long tongue sticking out; eyes rolled back showing the whites. Short '
        'masculine hair. Plain shirt. Thick aggressive WHITE gothic calligraphy and '
        'spray-paint swirling around the face like a frame. Strictly monochrome '
        '(blacks/whites/greys), film grain. Punk-zine collage, masculine, raw.'
    ),
    'warrior': (
        'Epic fantasy portrait of a MAN as a Norse VIKING WARRIOR. Rugged bearded '
        'face, fierce determined gaze. Worn leather-and-fur armor with metal '
        'pauldrons, a fur cloak over the shoulders. Masculine warrior hair (short or '
        'tied back), optional braided beard, subtle war-paint streaks. Dramatic misty '
        'battlefield / cold mountain background, cinematic moody light. Heroic, '
        'masculine, hyperreal fantasy. NO earrings, NO makeup, NO feminine elements.'
    ),
    'skull_saint': (
        'Dark gothic-occult hyperreal portrait of a MAN. Calm severe expression, '
        'closed lips. Short-to-medium dark masculine hair — NO long braids, NO '
        'feminine hair. Pale freckled skin. An enormous spiked headdress of golden '
        'human skulls and tall gold spikes arches behind and above his head like a '
        'macabre halo-crown. Black hooded religious robe with embroidered golden '
        'crosses, a heavy silver studded collar with dangling crosses on the chest. A '
        'stylistic patch of rough STONE / petrified texture covers the left side of '
        'his face and neck (decorative stone-saint effect, NOT a wound, no blood). '
        'Masculine — NO lipstick, NO long feminine nails, NO earrings. Dark catholic '
        'gothic, hyperreal painterly.'
    ),
}


def _male_adapt(preset_key: str | None, prompt: str) -> str:
    """Промпт для мужчины: если есть мужской оверрайд — берём его (он уже мужской),
    иначе оригинал + усиленный мужской суффикс."""
    ov = _MALE_PRESET_OVERRIDES.get(preset_key or '')
    return ov if ov else (prompt + _MALE_STYLE_SUFFIX)


# Мужские названия для гендерных пресетов (Валькирия→Викинг, святая→святой).
# Оверрайды названий под пол. Базовый лейбл (EDIT_PRESETS[key][0]) бывает
# female-дефолтным (тогда нужен мужской вариант) ИЛИ male-дефолтным (нужен
# женский). Правим ТОЛЬКО там, где имя привязано к ЧЕЛОВЕКУ (Бунтарка/Викинг),
# а не к стилю/предмету (Мраморный панк — нейтрально для обоих).
_MALE_LABEL_OVERRIDES: dict[str, str] = {
    'warrior': '🛡 Викинг',
    'skull_saint': '💀 Череп-святой',
    'bold_red_studio': '🥊 Бунтарь в красном',   # жен-дефолт «Бунтарка»
    'fashion_doll': '🪆 Гламурный образ',          # жен-дефолт «Гламурная кукла»
}

_FEMALE_LABEL_OVERRIDES: dict[str, str] = {
    'gold_coffee': '☕ Кофе по-светски',           # муж-дефолт «по-джентльменски»
    'rebel_poster': '🔴 Постер бунтарки',          # муж-дефолт «Постер бунтаря»
}


def _gender_label(preset_key: str, is_male: bool) -> str:
    """Базовое название пресета под пол (БЕЗ 🆕-бейджа). Учитывает оба оверрайда:
    мужской (для female-дефолтных лейблов) и женский (для male-дефолтных) — чтобы
    ни «Бунтарка» не смотрела на мужчин, ни «по-джентльменски» на женщин."""
    if is_male and preset_key in _MALE_LABEL_OVERRIDES:
        return _MALE_LABEL_OVERRIDES[preset_key]
    if not is_male and preset_key in _FEMALE_LABEL_OVERRIDES:
        return _FEMALE_LABEL_OVERRIDES[preset_key]
    return EDIT_PRESETS.get(preset_key, (preset_key, ''))[0]


def _label_for(preset_key: str, is_male: bool) -> str:
    """Название пресета с учётом пола + бейдж «🆕» для свежих (внутри окна
    новизны); бейдж виден и в тематических категориях, затухает сам когда
    пресет «выпускается» из Новинок (_BADGE_KEYS пересчитывается по TTL)."""
    base = _gender_label(preset_key, is_male)
    return f'{base} 🆕' if preset_key in _BADGE_KEYS else base

# Жёстко-женские пресеты — мужчинам прячем из каталога (платья/мех/балет/дамский
# образ; мужской суффикс их не спасает). Остальные считаем unisex.
_FEMALE_ONLY_PRESETS = {
    'vogue', 'vintage_car', 'lion_steps', 'subway_bouquet',
    'paris_night', 'doll_porcelain', 'korean_red_wall',
}


EDIT_PRESETS: dict[str, tuple[str, str | None]] = {
    # ── Новинки 01.06 (из мудборда) ──
    'suit_smoke': (
        '🖤 Костюм и сигарета',
        'Restyle the input photo as a moody high-fashion editorial portrait. The subject stands '
        'against a distressed grey concrete wall in an abandoned industrial space, three-quarter '
        'body framing. She wears an oversized navy-black mens tailored overcoat with wide lapels '
        'over a crisp white shirt and a thin black necktie; folded cuffs reveal a subtle pinstripe '
        'lining; a black leather belt with a silver buckle and dark trousers — borrowed-from-the-boys '
        'oversized tailoring. One hand is raised to her lips holding a thin lit cigarette with a faint '
        'wisp of smoke; long almond near-black manicure. Blonde hair in a loose low bun with '
        'face-framing strands, bold defined brows, smoky eye makeup, dark wine lipstick, lips '
        'slightly parted, gaze turned coolly off-camera.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact same face — '
        'eye shape and colour, nose, lips, jawline, skin tone. Only the styling, wardrobe, pose '
        'and setting change.\n\n'
        'Soft diffused overcast daylight, low contrast, desaturated cool palette, 50-85mm lens, '
        'shallow depth of field, fine film grain, quiet-luxury grunge editorial mood.'
    ),
    'trunk_pov': (
        '🚗 Кадр из багажника',
        'Restyle the input photo into a cinematic wide-angle scene shot from INSIDE an open car '
        'trunk, looking up and out. The trunks burgundy carpet lining fills the foreground. The '
        'subject — a stylish woman in a burgundy 1970s blazer over a small-patterned paisley shirt — '
        'leans over the open trunk: one hand raised gripping the open trunk lid at the top of the '
        'frame, the other reaching down toward the camera, looming large in exaggerated fisheye '
        'perspective. Intense, slightly startled expression looking down into the trunk. Behind her: '
        'bright blue sky with wispy clouds, an American strip-mall with a red "RETAIL OUTLET" sign '
        'and street lamps; a second woman in a bright orange dress stands by the storefront with her '
        'hands raised to her head in shock.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact same face — '
        'eye shape and colour, nose, lips, jawline, skin tone. Only the scene, wardrobe and '
        'composition change.\n\n'
        'Ultra-wide fisheye lens, strong barrel distortion, harsh bright midday sun, highly '
        'saturated colour, 1970s crime-comedy film aesthetic, photorealistic, slight film grain.'
    ),
    # ── Стилизации ──── (one-click, длинные качественные промпты)
    'cinema': (
        '🎬 Кино-нуар',
        'Restyle the input photo as a dark cinematic editorial close-up portrait using chiaroscuro '
        'lighting. A sharp diagonal beam of natural light cuts through deep blackness, illuminating '
        'only the eyes, cheekbones and glossy lips while the rest of the face fades into rich black '
        'shadow. Hair partially covers one eye, chin slightly raised, piercing gaze upward. Background '
        'completely absorbed by deep darkness, focus exclusively on the subject.\n\n'
        'CRITICAL: Keep the exact same face from the input — same eye shape and colour, same nose, '
        'same lips, same jawline, same skin tone. Only the lighting, mood and atmosphere change. '
        'The output must clearly be the SAME person from the input photo.\n\n'
        '8k, ultrarealistic, 85mm lens at f/1.8, magazine cover aesthetic, deep shadows.'
    ),
    'vogue': (
        '✨ Высокая мода',
        'Restyle the input photo as a high fashion Vogue cover portrait. The subject in luxurious '
        'haute couture clothing, flawless studio makeup, controlled lighting with a soft beauty dish. '
        'Pristine gradient background (soft cream or cool grey), sharp focus on face, magazine-quality '
        'retouching. Confident pose, slight chin elevation, direct gaze into camera.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact same face — '
        'same eye shape and colour, nose, lips, jawline, skin tone, hair colour. Apply the styling '
        '(clothes, makeup, hair, lighting, background) without changing the face.\n\n'
        '8k, 85mm portrait lens, fashion editorial mood, premium beauty photography, film grain.'
    ),
    'anime': (
        '🌸 Японская сказка',
        'Restyle the input photo as a beautiful hand-drawn anime portrait in the style of Studio '
        'Ghibli and modern Japanese animation. Soft watercolor textures, warm golden hour lighting, '
        'expressive large detailed eyes, delicate clean line work, dreamy painterly atmosphere. '
        'Painterly background with gentle bokeh, subtle wind in hair.\n\n'
        'CRITICAL: The anime portrait must clearly look like the SAME person from the input photo — '
        'preserve their face structure, distinctive eye shape and colour, hair colour and length, '
        'skin tone, and any defining features (freckles, dimples). Stylize, do not invent a new character.\n\n'
        'Studio Ghibli colour palette (warm honey + soft sky blue), Mamoru Hosoda film-still quality.'
    ),
    'cyberpunk': (
        '🌃 Неон будущего',
        'Restyle the input photo as a cyberpunk neon-lit night portrait. The subject bathed in vibrant '
        'pink and cyan neon glow from holographic city signs in the rain-slicked background. Wet asphalt '
        'reflections, atmospheric mist, futuristic edgy mood reminiscent of Blade Runner 2049. Sharp '
        'focus on face with dramatic neon rim lighting outlining cheekbones and hair. Subtle cyberpunk '
        'accessories (glowing earring or tech accents).\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape and '
        'colour, nose, lips, jawline, skin tone. Only the lighting, background and styling change.\n\n'
        '8k, anamorphic lens flares, vibrant teal-magenta colour grading, cinematic cyberpunk aesthetic.'
    ),
    'golden_hour': (
        '🌅 Магия заката',
        'Restyle the input photo as a romantic golden hour outdoor portrait. The subject backlit by '
        'warm setting sun, hair catching the light and glowing, soft natural lens flare. Warm amber '
        'and orange tones throughout, dreamy creamy bokeh of a wildflower meadow or wheat field in '
        'the background.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Preserve their face exactly — same '
        'eye shape and colour, nose, lips, jawline, skin tone, hair colour and length. Only the '
        'lighting, atmosphere and background change.\n\n'
        '85mm portrait lens at f/1.4, sun behind subject, warm cinematic grading, gentle light leaks, '
        'magazine-quality natural light photography.'
    ),
    'retro_90s': (
        '📷 Плёнка 90-х',
        'Restyle the input photo as an authentic 90s film photography portrait. Real 35mm film grain, '
        'warm faded colours, slight overexposure with light leaks. Casual candid pose, retro styling '
        '(denim, leather, layered chains, slip dresses, pearl chokers). Background suggests vintage '
        'interior or street scene. Feels nostalgic, raw, unretouched — a found photograph from a '
        'shoebox.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape and '
        'colour, nose, lips, jawline, skin tone, hair colour. Only the clothing, background, lighting '
        'and film aesthetic change.\n\n'
        'Kodak Portra 400 film emulation, period colour palette, soft analog focus, on-camera flash.'
    ),
    # ── Полнокадровые сцены ───── (новый образ + локация, identity сохраняем)
    'paris_night': (
        '🌹 Полночь в Париже',
        'Restyle the input photo as a full-body editorial night shot on a Parisian street corner. '
        'Subject in an elegant editorial pose on the corner of a classical Haussmannian beige stone '
        'building, tall windows and wrought-iron balconies behind them, a vintage wall lantern '
        'overhead. A corner brasserie glows with vivid red-pink neon, blurred pedestrians and parked '
        'cars further down the street. Long flowing hair slightly tousled by the wind, sultry '
        'confident expression looking back over the shoulder into the camera, lips softly parted.\n\n'
        'Outfit: floor-length backless gold metallic liquid-satin gown with delicate side '
        'embellishments, fabric pooling around the feet; nude pointed-toe stiletto heels.\n\n'
        'Warm cinematic lighting with red neon glow reflecting on the gown and skin, moody night-photo '
        'aesthetic, slight grain, low camera angle, photorealistic, high detail.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, outfit and '
        'lighting are built around them.'
    ),
    'rooftop_wine': (
        '🥂 Вечер на крыше',
        'Restyle the input photo as a full-body candid night shot on a rooftop terrace. POSE: subject '
        'sits relaxed on a small wooden bench, BOTH FEET resting flat on the floor side by side, knees '
        'together, leaning slightly forward with ONE elbow resting on a knee, ONE hand holding a single '
        'wine glass at chest height, the OTHER hand resting calmly on the thigh. Head softly tilted '
        'down toward the wine glass with a serene contented expression.\n\n'
        'ANATOMY (must be anatomically correct): exactly ONE human body — ONE head, ONE neck, TWO '
        'arms, TWO hands (one holding the wine glass, one resting on the thigh), TWO legs, TWO feet. '
        'NO duplicated or extra limbs, NO additional legs or arms, NO merged anatomy, NO floating '
        'extra wine glasses, NO extra hands. Just a normal seated person.\n\n'
        'Outfit: cream off-white wool overshirt jacket with collar and front buttons, worn open over '
        'a plain white tee; loose black trousers with a black leather belt and silver buckle; white '
        'classic sneakers. Hair: tousled natural with a soft side-parted fringe on the forehead.\n\n'
        'Background: nighttime city rooftop with a low stone wall and metal railing, blurred '
        'Parisian-style buildings with warm glowing windows in the distance, dark sky. A single bottle '
        'of wine stands on the bench beside the subject (no extra glasses, no other props). Warm '
        'ambient string-light glow above creates a cozy golden tone, candid film aesthetic, slight '
        'grain, cinematic mood, photorealistic, high detail.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone, hairline, hair colour. Only the scene, '
        'outfit and lighting change.'
    ),
    'vintage_car': (
        '🎩 Дама в старинном авто',
        'Restyle the input photo as an editorial fashion film shot. Subject seated inside the open '
        'door of a vintage classic black car, legs crossed elegantly extending out onto a cobblestone '
        'street, hands resting in the lap holding a small golden harp. Long wavy hair with soft '
        'curls falling on the shoulders, soft glamorous makeup, slight smile, head tilted slightly.\n\n'
        'Outfit: fluffy white fur hat (Russian-style), luxurious white fur coat with oversized collar '
        'worn over a delicate white lace slip dress, multi-strand pearl necklace, white patterned '
        'lace tights, white pointed-toe heels with bow detail.\n\n'
        'Background: vintage black retro car interior with grey upholstered seats visible through '
        'the open door, cobblestone street in soft daylight. Warm cinematic tones, soft film grain, '
        'editorial fashion aesthetic, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone. The scene, outfit, hair styling and '
        'lighting are added around them.'
    ),
    'lion_steps': (
        '🩰 Балет на ступенях',
        'Restyle the input photo as a high-fashion editorial outdoor shot. Subject in profile, '
        'leaning against a beige stone ledge of a classical monument, head turned to the side, '
        'lips slightly parted. Hair styled in a messy elegant updo bun with loose strands framing '
        'the face.\n\n'
        'Accessories: black cat-eye sunglasses, black velvet choker with silver pendant, long black '
        'leather opera gloves up to the elbows.\n\n'
        'Outfit: strapless nude/beige corset ballet-style mini dress with a structured bodice, '
        'black velvet waistband, voluminous tulle skirt.\n\n'
        'Background: classical stone staircase with a sculpted lion statue at the top, beige '
        'limestone steps, soft daylight outdoor setting. Warm cinematic tones, editorial fashion '
        'photo aesthetic, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, outfit, '
        'hair styling and lighting change.'
    ),
    'tennis_70s': (
        '🎾 Корт 70-х',
        'Restyle the input photo as a retro 70s fashion film photo. Subject standing in profile, '
        'head turned to the side, long wavy hair flowing in the wind to one side, soft red lipstick, '
        'a small pearl drop earring.\n\n'
        'Outfit: vintage white short-sleeve polo shirt with a collar and small buttons, tucked into '
        'a high-waisted white pleated tennis skirt; one hand holding a white wooden tennis racket.\n\n'
        'Background: clean teal-turquoise sky, low camera angle shot from below. Retro 70s film '
        'photo aesthetic, warm cinematic tones, soft grain, golden sunlight.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, outfit, '
        'lighting and composition change.'
    ),
    'warrior': (
        '🛡 Валькирия',
        'Restyle the input photo as a cinematic vintage-film fantasy shot. Subject lying on their '
        'back on dark green grass, hair spread softly on the grass around the head, head tilted, '
        'lips slightly parted, calm sensual expression.\n\n'
        'Outfit: silver metallic medieval-style fantasy armor — chest plate, shoulder pauldron, '
        'articulated arm gauntlet — paired with a brown leather belt and harness straps.\n\n'
        'Background: dark green grass field, warm natural sunlight beam illuminating the face and '
        'armor, cinematic side shadow. Vintage film photo aesthetic, soft grain, warm tones, '
        'photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, armor, '
        'lighting and composition change.'
    ),
    # ── Городские/ночные сцены ─── gender-neutral, identity-locked
    'subway_bouquet': (
        '💐 Метро с букетом',
        'Restyle the input photo as a moody cinematic editorial portrait on a subway platform. '
        'The subject standing facing the camera, holding a vibrant bouquet of red gerbera flowers '
        'close to the chest with both hands. Behind them, a yellow subway train passes in strong '
        'horizontal motion blur, painting warm golden streaks across the background. Hair softly '
        'tousled by the rush of train wind, slight melancholy expression, lips softly parted, eyes '
        'looking calmly into the camera.\n\n'
        'Outfit: dark navy or black oversized coat with high collar, minimalist styling — adapts '
        'naturally to any subject (works for both feminine and masculine looks).\n\n'
        'Background: industrial subway platform, dim tunnel ambient lights, blurred yellow train '
        'as the warm color anchor. Cinematic shallow depth of field on subject, motion blur only '
        'on the background. 35mm film aesthetic, slight grain, warm-yellow vs cool-shadow contrast, '
        'photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — same eye '
        'shape and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, outfit '
        'and lighting are added around them.'
    ),
    'rainy_taxi': (
        '🌧 Такси под дождём',
        'Restyle the input photo as a cinematic neon-noir night portrait inside a car. The subject '
        'sitting in the back seat, looking pensively out the rain-streaked side window. Hair styled '
        'naturally, slightly tousled. Contemplative expression, eyes catching the city lights '
        'through the wet glass.\n\n'
        'Lighting: dramatic cyberpunk neon glow from city signs outside — warm red illuminating one '
        'side of the face, cool cyan/teal on the other. Heavy rain on the window creates bokeh '
        'light flares from passing headlights.\n\n'
        'Outfit: dark casual jacket or coat, simple modern styling — adapts to any subject.\n\n'
        'Background: blurred neon-lit city street at night seen through the wet window — glowing '
        'storefront signs, distant headlights, street lamps creating cinematic bokeh.\n\n'
        'Blade Runner / film noir aesthetic, 35mm grain, deep contrast, high-saturation teal-orange '
        'colour grade, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape '
        'and colour, nose, lips, jawline, skin tone, hair colour. Only the scene and lighting change.'
    ),
    'vintage_rain': (
        '🍒 Дождь в красной машине',
        'Restyle the input photo as a vintage cinematic portrait inside a classic retro red car. '
        'The subject seated in the back, head resting softly against the deep red leather '
        'upholstery, gazing calmly out the rain-streaked side window. Pensive, slightly dreamy '
        'expression, lips softly closed.\n\n'
        'Lighting: soft moody daylight diffused through clouds, warm red glow from the leather '
        'interior reflecting onto the skin. Raindrops on the glass create gentle bokeh.\n\n'
        'Background: vintage red car interior with deep red leather seats and chrome details, '
        'rain-streaked window showing blurred shopfront signs and a quiet street outside. '
        'Nostalgic introspective mood.\n\n'
        'Outfit: dark coat or jacket, simple elegant styling that fits any subject.\n\n'
        'Vintage 35mm film photo aesthetic, warm muted tones, soft grain, melancholy cinematic '
        'mood, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape '
        'and colour, nose, lips, jawline, skin tone, hair colour. Only the scene and lighting change.'
    ),
    'berlin_metro': (
        '🟡 Жёлтый перрон',
        'Restyle the input photo as an editorial street-style portrait at an underground subway '
        'station. The subject standing on the platform, body angled slightly toward the camera, '
        'head turned with a confident calm gaze into the lens. A bright yellow subway train passes '
        'in strong horizontal motion blur behind, filling the background with warm golden movement.\n\n'
        'Outfit: long oversized black wool coat with sharp shoulders, simple minimalist styling — '
        'works for any subject. Optional small silver or gold stud earring.\n\n'
        'Background: tiled subway station with curved arched ceiling, clean white-and-yellow palette. '
        'The blurred yellow train creates a dramatic warm-cool color contrast against the cool tiles.\n\n'
        '35mm photo aesthetic, slight grain, sharp focus on the subject, motion blur only on the '
        'train. Editorial cinematic mood, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape '
        'and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, outfit and '
        'lighting change.'
    ),
    'giant_city': (
        '🏙 Гулливер в городе',
        'Restyle the input photo as a surreal forced-perspective composite scene. The subject '
        'appearing as a giant figure striding through a miniature city — towering modern skyscrapers '
        'at full normal scale on the horizon, but the streets, signs and traffic below shrunk to '
        'tiny ant-sized scale. The giant subject mid-stride, one foot lifted high above tiny '
        'crosswalks, taxis and pedestrians.\n\n'
        'Lighting: bright sunny daytime, clear blue sky, sharp clean shadows.\n\n'
        'Outfit: casual modern streetwear — oversized graphic tee, loose cargo pants, chunky '
        'white sneakers. Adapts naturally to any subject.\n\n'
        'Background: New York-style downtown — classical limestone facades mixed with glass '
        'skyscrapers, street signs (ONE WAY visible), yellow taxis and small cars on the asphalt '
        'below, crosswalks seen from a high angle. Strong forced-perspective: subject huge in the '
        'frame, city tiny below.\n\n'
        'Surreal pop-editorial aesthetic, vivid saturated colors, sharp focus, high contrast, '
        'photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape '
        'and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, outfit and '
        'composition change.'
    ),
    'fisheye_paris': (
        '🐟 Париж в кривом стекле',
        'Restyle the input photo as a surreal black-and-white fisheye street photo on a Paris '
        'intersection. The subject standing on a cobblestone crosswalk under an extreme fisheye '
        'lens — wide barrel distortion making the Haussmannian buildings curve dramatically around '
        'the edges of the frame and the subject appearing slightly stretched yet prominent in the '
        'centre. The subject leaning slightly into the lens with a calm confident expression.\n\n'
        'Lighting: overcast soft daylight, classic monochrome black-and-white tones.\n\n'
        'Outfit: dark casual jacket or coat with simple trousers — adapts to any subject.\n\n'
        'Background: classical Haussmannian Paris architecture curving with the fisheye distortion '
        'at the edges, wrought-iron balconies, distant pedestrians and parked cars at the warped '
        'horizon, bold zebra-crossing lines radiating from the centre of the frame.\n\n'
        'Documentary street-photography aesthetic, fisheye 8mm lens look, grainy black-and-white '
        'film, candid editorial mood, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape '
        'and colour, nose, lips, jawline, skin tone, hair colour. Only the scene and lens '
        'distortion change.'
    ),
    'y2k_attitude': (
        '💋 Дерзкие нулевые',
        'Restyle the input photo as a Y2K-aesthetic fisheye close-up portrait inside a brightly-lit '
        'convenience store. The subject leaning into a wide-angle fisheye lens so the head and '
        'shoulders dominate the frame, the surroundings curving outward dramatically. Playful '
        'Y2K hair styling with small colourful plastic clips throughout (adapt to subject\'s hair '
        'length). Subtle natural freckles across the cheeks and nose. Confident playful Gen-Z '
        'expression, lips softly parted.\n\n'
        'Outfit: layered thin gold necklaces, casual graphic top — preserved from the input where '
        'possible, otherwise adapted to a Y2K casual vibe (works for any subject).\n\n'
        'Background: brightly-lit convenience store aisle filled with colourful snack bags in pink, '
        'purple, blue and yellow stacked on shelves on either side, curving out-of-focus due to '
        'fisheye distortion. Bright fluorescent ceiling lights.\n\n'
        'Y2K nostalgia aesthetic, retro early-2000s digital camera feel, slight grain, vivid '
        'saturated colors, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — eye shape '
        'and colour, nose, lips, jawline, skin tone, hair colour. Only the scene, lens distortion '
        'and lighting change.'
    ),
    # ── Y2K / Fisheye / Bold-Red серия (по референсам редактора) ──
    'fisheye_porsche': (
        '🏎 На капоте купе',
        'Restyle the input photo as a fisheye wide-angle attitude shot. Subject sits '
        'confidently on the bonnet of a classic vintage red sports coupe (911-style, no '
        'brand text visible), one hand reaching toward the camera in a bold gesture, the '
        'other resting on the car hood, body angled toward the lens with relaxed power.\n\n'
        'Outfit: black turtleneck crop-top, black leather pants with thin silver chain '
        'belt, mirror-lens aviator sunglasses, hair in messy high bun (adapts to any '
        'hair length and gender), layered silver necklaces.\n\n'
        'Background: empty urban highway curving dramatically upward due to fisheye '
        'barrel distortion, blurred modern city skyline in distance, sharp midday '
        'daylight, clean blue sky, road markings warped by the wide-angle lens.\n\n'
        'Y2K retro-street photography aesthetic, vivid saturated colors, sharp focus on '
        'subject, photorealistic, high detail, magazine-quality editorial.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — '
        'same eye shape and colour, nose, lips, jawline, skin tone, hair colour. Only '
        'the scene, pose, outfit and lens distortion are added around them.'
    ),
    'y2k_fridge': (
        '🍿 Ночной перекус',
        'Restyle the input photo as a Y2K-aesthetic fisheye close-up portrait in front '
        'of an open refrigerator at night. Subject holds an open box of colourful '
        'sushi/snacks with a playful surprised expression, mouth slightly open as if '
        'mid-bite, eyes wide and amused, frozen in a candid moment.\n\n'
        'Outfit: white tank top or thin baby tee, layered thin gold chain necklaces. '
        'Hair styled in playful pigtails with small colourful plastic clips (adapts to '
        'any hair length).\n\n'
        'Background: brightly-lit refrigerator interior packed with colourful soda cans, '
        'snack bags, fruit and condiments — curving outward at the edges due to fisheye '
        'distortion. Cozy late-night kitchen vibe with warm interior glow.\n\n'
        'Retro early-2000s digital camera feel, slight grain, vivid saturated pink/'
        'yellow/blue palette, on-camera flash light, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — '
        'eye shape and colour, nose, lips, jawline, skin tone, hair colour. Only the '
        'scene, hair styling, lighting and lens distortion change.'
    ),
    'macro_watermelon': (
        '🍉 Сквозь арбузное стекло',
        'Restyle the input photo as a surreal commercial macro close-up portrait. '
        'Subject is drinking through a blue plastic straw, viewed through a circular '
        'glass surface covered with vivid pink-coral watermelon flesh and dark seeds. '
        'The face is centered behind the glass — eyes looking directly at the camera '
        'with playful intensity, lips bold red.\n\n'
        'The translucent glass reveals bright sunny daylight outside, vibrant blue sky '
        'visible at the curved edges. Watermelon pulp frames the face like a vivid '
        'pink halo of fruit, droplets of juice glisten on the glass.\n\n'
        'Hyper-realistic, ultra-sharp focus on the eyes and the straw, surreal '
        'commercial advertising photography aesthetic, vivid saturated pink/red/blue '
        'palette, photorealistic, magazine-quality.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Preserve the exact '
        'face — eye shape and colour, nose, jawline, skin tone, hair colour. Only the '
        'lens framing (watermelon-glass overlay) and lighting are added.'
    ),
    'night_racing': (
        '🌃 После клуба',
        'Restyle the input photo as a moody candid night street photo. Subject walks '
        'confidently down an urban road at night, body slightly turned, mouth softly '
        'open mid-stride, eyes catching neon light. The shot feels stolen, intimate.\n\n'
        'Outfit: oversized red-and-black varsity racing jacket worn over a fitted '
        'black crop top and biker shorts, layered silver chains, slim red cat-eye '
        'sunglasses pushed up on the head. One hand holds a red plastic party cup. '
        'Hair messy and windblown.\n\n'
        'Background: dark urban night street, blurred warm headlights forming bokeh '
        'in distance, faint neon glow reflecting on wet asphalt, subtle atmospheric '
        'haze.\n\n'
        '35mm film grain, high contrast, intentionally slightly underexposed, Y2K '
        'party-street aesthetic, candid composition, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — '
        'eye shape and colour, nose, lips, jawline, skin tone, hair colour. Only the '
        'scene, outfit, mood and lighting change.'
    ),
    'bold_red_studio': (
        '🥊 Бунтарка в красном',
        'Restyle the input photo as a bold editorial studio portrait shot from above '
        'with mild fisheye distortion. Subject crouches in a dynamic playful pose, '
        'hands wearing oversized glossy red boxing gloves reaching upward toward the '
        'camera and dominating the foreground. Tongue playfully out, head slightly '
        'tilted up.\n\n'
        'Outfit: glossy red leather cropped jacket, red corduroy baseball cap (no '
        'logos), dark wayfarer sunglasses. Hair messy and natural.\n\n'
        'Background: clean white studio backdrop, controlled studio lighting with soft '
        'shadow under subject, professional fashion photography setup feel.\n\n'
        'Strong vivid red colour palette throughout, sharp focus, high-fashion '
        'editorial aesthetic, magazine cover quality, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — '
        'eye shape and colour, nose, lips, jawline, skin tone, hair colour. Only the '
        'scene, outfit and lighting change.'
    ),
    'magazine_vibe': (
        '📰 Обложка глянца',
        'Restyle the input photo as a mock magazine cover layout in Y2K aesthetic. '
        'Hero image fills the cover: fisheye close-up portrait of the subject with '
        'playful pigtail hairstyling and small colourful hair clips, looking up into '
        'the camera with bold Gen-Z expression. Layered thin gold chain necklaces.\n\n'
        'Background of the hero shot: brightly-lit convenience-store aisle filled with '
        'vivid snack packages and beverages, curving outward dramatically due to '
        'fisheye distortion.\n\n'
        'Magazine cover text overlay (rendered as if printed on a real magazine):\n'
        '— Large bold red sans-serif "VIBE" wordmark at the top\n'
        '— Vertical "2000\'S" running down the left margin\n'
        '— Small "RAW & REAL" tagline in top-right corner\n'
        '— Bottom footer "NO RULES — NO LIMITS · ISSUE 2026"\n\n'
        'Retro magazine cover aesthetic, slight scratch and dust film grain, vintage '
        'paper texture overlay, photorealistic.\n\n'
        'CRITICAL: This is the SAME person from the input photo. Keep the exact face — '
        'eye shape and colour, nose, lips, jawline, skin tone, hair colour. Only the '
        'scene, hair styling, lens distortion and magazine cover overlay are added.'
    ),
    # ── Действия с уточнением (юзер вводит детали) ──
    'change_clothes': (
        '👗 Переодеться',
        None,
    ),
    'change_bg': (
        '🖼 Сменить локацию',
        None,
    ),
    'remove_bg': (
        '✂️ Вырезать фон',
        'Remove the background completely. Leave only the main subject on a clean transparent or pure '
        'white background. Keep the subject sharp, detailed and natural; preserve identity and styling.',
    ),
    # ── Арт-серия (10 шт., добавлено 21.05.2026 по референсам с Pinterest) ──
    'doll_porcelain': (
        '🎀 Фарфоровая кукла',
        'Repaint as a HYPERREAL 3D CGI DOLL beauty close-up portrait — porcelain-doll aesthetic with '
        'immaculate dewy glossy skin. EXTREME CLOSE-UP: only forehead, eyes, nose, mouth and upper neck '
        'fill the frame. SKIN: pale porcelain skin with fine pores, light freckles across nose and upper '
        'cheeks, dewy GLOSSY highlights on cheekbones, nose and chin. EYES: huge glossy doe-eyes, dark '
        'amber-brown irises with crisp catchlights, thick fanned black lashes. LIPS: super PLUMP GLOSSY '
        'peach-pink lips, slightly parted, ultra-shiny highlight. HAIR: dark messy WET-LOOK bangs across '
        'the forehead, dark hair framing the face. ACCESSORIES: small pink dangly STAR-OR-HEART earring; '
        'BLACK PVC LEATHER BOW CHOKER with silver O-rings tied at the centre front. LIGHTING: warm soft '
        'orange key light from the left, soft shadow on the right, immaculate highlights. BACKGROUND: '
        'blurred soft PINK / PEACH faux fur or fluffy plush, dreamy bokeh. Aesthetic: hyperreal kawaii '
        'dark-academia 3D doll-art.'
    ),
    'clockwork_text': (
        '🍊 Огненная корона',
        'Repaint as a hyperreal high-fashion editorial PROFILE portrait. The KEY ELEMENT: GLOWING ORANGE '
        'HANDWRITTEN GRAFFITI TEXT projected and painted across the entire visible face, neck and jacket '
        '— chaotic spray-paint English LETTERING glowing like warm orange neon ink. PROMINENT WORDS '
        'clearly readable: "CLOCKWORK" boldly across the cheek, "ORANGE" across the lower jaw, plus '
        'smaller scribbles "OUT", "RANCK", "MNYW", "OneDFTA", "RbiNcwn" on forehead, neck and shoulder. '
        'Text is hand-scrawled, slightly messy, overlapping, all GLOWING WARM ORANGE-AMBER with soft '
        'halo around each letter. CROWN: on the head a tall ornate REGAL CROWN encrusted with ORANGE-'
        'AMBER RHINESTONES, with a bright orange neon glowing underband at the base lighting the '
        'forehead. EARRING: small dangling jewel earring catching orange light. OUTFIT: dark heavy '
        'collared jacket covered in the same glowing orange graffiti text and neon-line patterns. '
        'LIGHTING: nearly black background, single warm orange illumination only from the glowing text '
        'and crown — dramatic chiaroscuro. Aesthetic: high-fashion editorial / art project / magazine '
        'cover — bold, regal, conceptual.'
    ),
    'red_lust': (
        '🌹 Красный жар',
        'Repaint as a luxurious cinematic BEAUTY close-up drenched in MONOCHROMATIC DEEP RED LIGHT — '
        'entire image bathed in saturated cherry-red, as if shot under a red gel; only reds, deep '
        'crimsons, dark wine and near-black tones, no other colours visible. FRAMING: extreme close-up '
        'of the lower two-thirds of the face. POSE: head tilted slightly UP and BACK toward the light, '
        'sensual languid expression, lips slightly parted with the lower teeth barely peeking, eyes '
        'half-lidded looking up. SKIN: pale skin shifted to red, dewy glossy texture with visible '
        'natural FRECKLES across the nose and cheeks, dramatic shadow from the brow-bone above the eye. '
        'LIPS: super GLOSSY PLUMP RED-PINK lips, ultra-shiny highlight on the lower lip. HAIR: dark '
        'wavy strands wispy across the brow/cheek, slightly damp look. FOREGROUND: a few dark thin DRY '
        'BRANCHES / TWIGS crossing diagonally across the lower face and shoulder, soft out of focus, '
        'tinted red. BACKGROUND: deeper red gradient softly out of focus. LIGHTING: single warm red key '
        'light from above-right, deep dark red shadows. Aesthetic: cinematic luxury beauty photography, '
        'monochromatic red, sensual, painterly.'
    ),
    'cyber_neon': (
        '💋 Кибер-неон',
        'Repaint as a gritty cinematic CYBERPUNK concept-art portrait, painterly digital art with raw '
        'edgy mood. LOW-ANGLE close-up — camera looks UP at the subject. POSE & EXPRESSION: subject '
        'leans down looking straight into camera with a wide MISCHIEVOUS GRIN, mouth OPEN showing front '
        'TEETH and a pink TONGUE STUCK OUT to one side, both INDEX FINGERS raised and touching the '
        'corners of the mouth, hands framing the lower face in a playful provocative gesture. HAIR: '
        'dark almost-black hair pulled into TWO SMALL MESSY TWIN SPACE-BUNS with stray braided strands '
        'on each side; choppy blunt BANGS across the forehead. FACE: heavy black SMOKY EYESHADOW with '
        'sharp liner; pale skin; multiple SILVER LIP-PIERCINGS along the lower lip (spider-bite style); '
        'thin silver chain dangling from one piercing; large silver hoop earrings. ACCESSORIES: thin '
        'BLACK LEATHER CHOKER, layered silver chain necklaces with tiny PADLOCK pendant, many SILVER '
        'RINGS on every finger including pointed claw-style. OUTFIT: oversized OLIVE-GREY NYLON wind-'
        'jacket unzipped over a black crop top. BACKGROUND: blurred industrial CYBERPUNK scene in '
        'VIVID PURPLE AND MAGENTA NEON, blown-out vertical light source behind the head like an open '
        'lit doorway, speakers/equipment racks. Aesthetic: cyber-anarchist / club kid, gritty digital '
        'concept art, defiant.'
    ),
    'marble_punk': (
        '🗿 Мраморный панк',
        'Repaint as a stylized 3D CGI SCULPTURE / pop-art editorial figure — MARBLE-DOLL aesthetic: '
        'the entire person rendered as a smooth carved MATTE WHITE MARBLE STATUE (skin, blouse and '
        'hair are sculpted white stone with subtle veining). POSE & EXPRESSION: facing forward, '
        'AGGRESSIVE SNARL — eyebrows furrowed, deep wrinkles on the nose-bridge from snarling, top '
        'lip curled up exposing FRONT TEETH BARED, lower jaw forward; ONE HAND raised showing the '
        'MIDDLE FINGER fully extended; OTHER HAND holds a clear PLASTIC CUP with vivid RED SLUSHIE '
        'and white paper straw. HAIR: two TOP-OF-HEAD SPACE BUNS sculpted from layered WHITE MARBLE '
        'ROSE PETALS, centre-parted slick-back hairline. EYEWEAR: large CURVED RED CAT-EYE SUNGLASSES '
        '(60s vintage). EARRINGS: round RED CHERRY-BALL drop earrings. OUTFIT: high-collar WHITE '
        'VICTORIAN FRILLED BLOUSE, sculpted marble texture. BACKGROUND: vivid flat saturated RED '
        'studio backdrop. PALETTE: pure marble white + vivid bright red. Aesthetic: surreal stylized '
        '3D sculpture pop-art, polished CGI, satirical, bold.'
    ),
    'graffiti_zine': (
        '✍️ Гранж-зин',
        'Repaint as a GRITTY VINTAGE BLACK-AND-WHITE photograph portrait with heavy WHITE STREET-ART '
        'GRAFFITI OVERLAY — old-school punk zine collage. Completely MONOCHROME — no colour, only '
        'blacks/whites/greys, vintage film grain. POSE & EXPRESSION (manic provocative): head tilted '
        'slightly UP and BACK, MOUTH WIDE OPEN, long pink TONGUE STICKING OUT FAR pointing downward '
        'past the chin, EYES ROLLED BACK so only the WHITES OF THE EYES (white sclera) are visible — '
        'no irises, manic ecstatic expression; eyebrows raised. HAIR: short-to-mid blonde-toned hair '
        '(reads pale grey in BW), slightly wavy. HAND: a hand intrudes from BELOW, index finger and '
        'thumb framing the chin area. CLOTHING: simple light shirt with PIN-DOT pattern. GRAFFITI '
        'OVERLAY: thick aggressive WHITE SPRAY-PAINT and bold gothic CALLIGRAPHY letterforms swirling '
        'around the face like a jagged crown/halo/frame — sharp pointy ornamental letters, splatter '
        'dots, scratchy scribble lines, all SOLID WHITE PAINT painted on top of the photo, drippy. '
        'BACKGROUND: light grey paper-grain texture. Aesthetic: punk-zine collage, raw street-art, '
        'irreverent.'
    ),
    'skull_saint': (
        '💀 Череп-святая',
        'Repaint as a dark HYPERREAL gothic-occult portrait, photo-realistic painterly fantasy. POSE: '
        'facing camera, calm severe expression, lips closed; ONE HAND raised near the cheek with LONG '
        'SHARP CLAWLIKE NAILS painted DARK RED. HAIR: long BRIGHT RED-AUBURN hair partially braided '
        'with thin braids and small metal clips, framing the face. FACE: pale freckle-covered skin, '
        'soft natural pink lips, SILVER SEPTUM RING, multiple ear piercings with silver hoops and '
        'crosses. CROWN/HALO: enormous ornate spiked HEADDRESS made of golden human SKULLS and tall '
        'sharp GOLD SPIKES arranged in a wide arc behind and above the head — multiple yellow-gold '
        'skulls interlocked with sharp pointed gold spikes radiating outward, baroque ornate metalwork. '
        'NECK: heavy SILVER STUDDED LEATHER COLLAR with chains, multiple silver CROSSES dangling on '
        'layered chains. ROBE: black HOODED religious-style ROBE with multiple embroidered GOLDEN '
        'CROSS emblems on the sleeves and chest, white tunic visible beneath. KEY STYLISTIC DETAIL '
        '(not a wound): a textured patch of ROUGH STONE / PETRIFIED / sculpted MOSS-stone surface '
        'covers part of the LEFT SIDE of the face (left cheek down to jawline) and continues onto the '
        'left side of the neck/shoulder — decorative STONE-SAINT effect, NO blood, NO redness, NO '
        'glow, NO injury. BACKGROUND: dark almost black. Aesthetic: dark catholic gothic occult dark-'
        'fantasy saint, hyperreal painterly.'
    ),
    'underwater_red': (
        '🌊 Под водой в красном свете',
        'Repaint as a PHOTO-REALISTIC ethereal UNDERWATER portrait. Subject submerged just BELOW the '
        'WATER SURFACE, head and bare shoulders visible. POSE: face turned slightly upward, EYES '
        'CLOSED PEACEFULLY in deep serene meditative expression, lips closed and relaxed. LIGHTING '
        '(key element): bright RED LIGHT from above filters through the rippling water surface, '
        'casting vivid RED CAUSTIC WAVE-RIPPLE PATTERNS dancing across the face, forehead, cheeks, '
        'chin, neck and bare shoulders — wavy red bands of light shimmer over the skin like liquid '
        'red netting. The water surface above is visible at top of frame with the same red rippling. '
        'Below the surface light, the surrounding water reads as deep TEAL-BLACK GREENISH-BLUE, '
        'intense contrast between red light on skin and cool dark water. HAIR: medium dark hair floats '
        'gently in the water around the face. SHOULDERS: bare wet glistening, modest framing (no '
        'nudity). BUBBLES: subtle small bubbles. PALETTE: blazing rippling red + deep teal-black + '
        'pale skin under red. Aesthetic: ethereal cinematic underwater photography, meditative.'
    ),
    'goldfish_dream': (
        '🐠 Сон с рыбками',
        'Repaint as a DREAMY PHOTO-REALISTIC UNDERWATER fashion portrait. Subject submerged JUST '
        'BELOW the WATER SURFACE, head and bare shoulders visible. POSE: face turned slightly to one '
        'side and gently upward, EYES OPEN with a calm DREAMY contemplative gaze, lips softly closed. '
        'HAIR: dark medium-length hair flowing softly in the water around the face. AROUND THE FACE: '
        '6-8 GOLDFISH / fancy comet KOI — ORANGE-AND-WHITE bicolour with long flowing fins and tails, '
        'some in sharp focus close to the face, some out-of-focus in the foreground and background, '
        'swimming in different directions; one fish near the top of the head, one or two in front of '
        'the chest. BUBBLES: scattered fine air bubbles drifting upward, sparkle effect. LIGHTING: '
        'soft natural DAYLIGHT from above the water, gentle wavy caustic highlights on the face. '
        'Water surface visible at top with warm rippling reflections. BACKGROUND: deep teal-green '
        'water with soft light beams. SHOULDERS: bare, modest framing (no nudity). Aesthetic: dreamy '
        'ethereal magical-realism underwater photography, calm wistful.'
    ),
    'korean_red_wall': (
        '🍒 Корея, красный',
        'Repaint as a MINIMALIST KOREAN FASHION editorial photograph, photo-realistic studio portrait '
        '— FULL BODY shot. POSE: subject sits on the studio floor, BOTH KNEES bent and drawn up to '
        'the chest, ankles crossed; ONE HAND rests softly under the CHIN in a contemplative pose with '
        'the elbow on the knee; calm thoughtful expression looking directly at camera, lips softly '
        'closed. HAIR: dark almost-black hair pulled into a casual low ponytail / messy updo with '
        'soft loose face-framing strands. MAKEUP: natural light Korean makeup, soft pink-rose lipstick, '
        'dewy clean skin, light blush. OUTFIT: oversized DARK GREY CORDUROY blazer worn open over a '
        'dark inner top, subtle red plaid shorts at the hips; WHITE COTTON KNEE-HIGH socks visible '
        'above the sneakers; chunky BLUE-AND-CREAM GUCCI sneakers. ACCESSORIES: a WHITE LEATHER '
        'GUCCI HANDBAG with gold buckle on the floor to her right; CHERRY BRANCHES with bright RED '
        'CHERRIES and green leaves draped artfully around the legs/lap. BACKGROUND: vivid SOLID RED '
        'studio backdrop with a soft cast shadow. LIGHTING: soft even studio key from above-front, '
        'clean fashion-editorial light. Aesthetic: minimalist Korean fashion editorial, polished, '
        'magazine-cover quality.'
    ),
}

_PRESET_PROMPT_TEMPLATES = {
    'change_clothes': (
        'Change the clothing of the person in this photo to: {user}. '
        'CRITICAL: Keep the SAME person — same face, same identity, same skin tone, same body proportions, '
        'same hair, same pose, and the same background. Only the clothes change.'
    ),
    'change_bg': (
        'Replace the background of this photo with: {user}. '
        'CRITICAL: Keep the main subject completely untouched — same face, identity, body, pose, '
        'and lighting on the subject. Only the background behind them changes.'
    ),
}

# ── Custom-пресеты из дашборда (БД) ─────────────────────────────────────────
# Категория-контейнер для пресетов, которым не задали явную категорию.
_CUSTOM_CATEGORY_TITLE = '🆕 Новинки'

# Ключи всех активных custom-пресетов (наполняется в reload_custom_presets).
# Нужен чтобы в «Показать все» и внутри категорий новые сцены шли ПЕРВЫМИ.
_CUSTOM_KEYS: set[str] = set()

# Снапшот хардкод-структур делается ОДИН раз при первом reload. Нужен чтобы
# overrides из БД могли перетирать хардкод — а при delete/disable хардкод
# чисто восстанавливался обратно. До снапшота EDIT_PRESETS / PRESET_PAGES /
# овeрайды считаются «истинным хардкодом».
_HARDCODED_SNAPSHOT: dict | None = None


def _snapshot_hardcoded_once() -> None:
    global _HARDCODED_SNAPSHOT
    if _HARDCODED_SNAPSHOT is not None:
        return
    _HARDCODED_SNAPSHOT = {
        'edit_presets':   dict(EDIT_PRESETS),
        'quality_map':    dict(PRESET_QUALITY_MAP),
        'male_overrides': dict(_MALE_PRESET_OVERRIDES),
        'female_only':    set(_FEMALE_ONLY_PRESETS),
        'pages': [{
            'title': p['title'], 'subtitle': p.get('subtitle', ''),
            'price_hint': p.get('price_hint', ''),
            'items': list(p['items']),
        } for p in PRESET_PAGES],
    }


async def reload_custom_presets() -> int:
    """Восстанавливает хардкод-структуры из снапшота, затем накатывает custom
    из БД поверх (с правом override любого хардкод-ключа — иначе нельзя
    редактировать встроенные промпты через дашборд). Возвращает число
    активных custom-пресетов."""
    from app.database.repo.custom_presets import list_custom_presets
    _snapshot_hardcoded_once()
    snap = _HARDCODED_SNAPSHOT  # type: ignore[assignment]

    # 24.05: сначала тянем БД (await), и только ПОТОМ синхронно собираем новое
    # состояние в локалах + атомарный swap в конце. Раньше globals чистились ДО
    # await → конкурентный читатель (_build_gallery_list) видел частичное
    # состояние (custom-пресеты исчезали на время запроса). Теперь во время
    # await globals держат прежнее полное состояние.
    try:
        async with AsyncSessionFactory() as session:
            rows = await list_custom_presets(session, only_enabled=True)
    except Exception as exc:
        logger.warning('reload_custom_presets failed', error=str(exc)[:200])
        return 0

    # Сборка нового состояния в ЛОКАЛАХ (синхронно, без await).
    new_presets = dict(snap['edit_presets'])
    new_quality = dict(snap['quality_map'])
    new_male = dict(snap['male_overrides'])
    new_female = set(snap['female_only'])
    new_pages = [{
        'title': p['title'], 'subtitle': p['subtitle'],
        'price_hint': p['price_hint'], 'items': list(p['items']),
    } for p in snap['pages']]
    new_custom_keys: set[str] = set()

    titles = {p['title']: p for p in new_pages}
    custom_rows = []
    for r in rows:
        new_presets[r.key] = (r.label, r.prompt)
        new_quality[r.key] = r.quality_mode or 'quality'
        if r.male_prompt:
            new_male[r.key] = r.male_prompt
        else:
            new_male.pop(r.key, None)
        if r.female_only:
            new_female.add(r.key)
        else:
            new_female.discard(r.key)
        if r.key not in snap['edit_presets']:
            custom_rows.append(r)
            new_custom_keys.add(r.key)

    # Новые custom → в их тематическую категорию, вставкой в НАЧАЛО (новейшие
    # сверху). Дефолт без category — первая тематическая страница.
    custom_rows.sort(key=lambda r: (not r.featured, -r.id))
    per_page: dict[str, list[str]] = {}
    fallback_title = new_pages[0]['title'] if new_pages else None
    for r in custom_rows:
        page = titles.get(r.category) if r.category else None
        title = page['title'] if page else fallback_title
        if title is None:
            continue
        per_page.setdefault(title, []).append(r.key)
    for title, new_keys in per_page.items():
        page = titles[title]
        rest = [k for k in page['items'] if k not in new_keys]
        page['items'] = new_keys + rest

    # АТОМАРНЫЙ swap (синхронно, без await между присвоениями).
    EDIT_PRESETS.clear(); EDIT_PRESETS.update(new_presets)
    PRESET_QUALITY_MAP.clear(); PRESET_QUALITY_MAP.update(new_quality)
    _MALE_PRESET_OVERRIDES.clear(); _MALE_PRESET_OVERRIDES.update(new_male)
    _FEMALE_ONLY_PRESETS.clear(); _FEMALE_ONLY_PRESETS.update(new_female)
    PRESET_PAGES[:] = new_pages
    _CUSTOM_KEYS.clear(); _CUSTOM_KEYS.update(new_custom_keys)

    # Реестр даты запуска: регистрируем ВСЕ актуальные ключи (и код-, и DB-).
    # Новые получают дату: DB → created_at, хардкод → now() (первое появление).
    # Существующие не трогаются (ON CONFLICT DO NOTHING). Так «новизна» больше
    # не зависит от регенерации превью. Падение не должно ломать reload.
    try:
        from app.database.repo.preset_launch import ensure_launch_rows
        created_at_map = {r.key: r.created_at for r in rows if r.created_at}
        async with AsyncSessionFactory() as session:
            await ensure_launch_rows(session, list(EDIT_PRESETS.keys()), created_at_map)
    except Exception as exc:
        logger.warning('ensure_launch_rows failed', error=str(exc)[:200])

    # Свежий набор зависит от EDIT_PRESETS + реестра дат → форс-пересчёт.
    await refresh_fresh_presets(force=True)
    return len(rows)


_BUY_KB = buy_or_invite_kb()

_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton.model_construct(text='Отмена', callback_data='edit_cancel', icon_custom_emoji_id=PE_CROSS, style='danger')],
])


def _preset_btn(key: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=EDIT_PRESETS[key][0], callback_data=f'edit_preset:{key}')


def _nav_row(page_idx: int) -> list[InlineKeyboardButton]:
    """Симметричный нижний ряд пагинации: всегда обе стрелки + центральный индикатор.
    Когда стрелка «выходит» за края — показываем её как заглушку (◁ / ▷), чтобы не
    разъезжалась геометрия ряда. Тап по заглушке — silent ack."""
    total = len(PRESET_PAGES)
    # Premium стрелки на всех позициях (на крайних — callback=noop, silent ack).
    if page_idx > 0:
        left = InlineKeyboardButton.model_construct(
            text='Назад', callback_data=f'edit_page:{page_idx - 1}',
            icon_custom_emoji_id=PE_LEFT,
        )
    else:
        left = InlineKeyboardButton.model_construct(
            text='Назад', callback_data='edit_page_noop',
            icon_custom_emoji_id=PE_LEFT,
        )
    if page_idx < total - 1:
        right = InlineKeyboardButton.model_construct(
            text='Вперёд', callback_data=f'edit_page:{page_idx + 1}',
            icon_custom_emoji_id=PE_RIGHT,
        )
    else:
        right = InlineKeyboardButton.model_construct(
            text='Вперёд', callback_data='edit_page_noop',
            icon_custom_emoji_id=PE_RIGHT,
        )
    center = InlineKeyboardButton(
        text=f'· {page_idx + 1} / {total} ·',
        callback_data='edit_page_noop',
    )
    return [left, center, right]


def _presets_kb(page_idx: int = 0) -> InlineKeyboardMarkup:
    """Сухое меню пресетов с пагинацией (без спец-действий photo_action).
    Используется когда юзер пришёл из /edit без свежего фото в state."""
    page_idx = max(0, min(page_idx, len(PRESET_PAGES) - 1))
    page = PRESET_PAGES[page_idx]
    rows: list[list[InlineKeyboardButton]] = []
    items = page['items']
    for i in range(0, len(items), 2):
        row = [_preset_btn(items[i])]
        if i + 1 < len(items):
            row.append(_preset_btn(items[i + 1]))
        rows.append(row)
    rows.append(_nav_row(page_idx))
    rows.append([InlineKeyboardButton.model_construct(
        text='Отмена', callback_data='edit_cancel',
        icon_custom_emoji_id=PE_CROSS, style='danger',
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _presets_header(page_idx: int = 0) -> str:
    """Шапка сообщения над клавиатурой пресетов — описание текущей страницы + цена."""
    page_idx = max(0, min(page_idx, len(PRESET_PAGES) - 1))
    page = PRESET_PAGES[page_idx]
    return (
        f'🪄 <b>{page["title"]}</b> · <i>стр. {page_idx + 1}/{len(PRESET_PAGES)}</i>\n\n'
        f'{page["subtitle"]}\n\n'
        f'<i>{page["price_hint"]}</i> · или напиши свой запрос текстом.'
    )


# Старые места используют _PRESETS_HEADER как первую страницу — оставляем алиас
_PRESETS_HEADER = _presets_header(0)


# ────────────────────────────────────────────────────────────
# Универсальная замена контента сообщения. Нужна потому что callback может
# прилетать от любого типа источника:
#   - текстовое сообщение (главное меню) → нужен edit_text
#   - фото с подписью (результат генерации) → нужен edit_caption
# Если оба не сработают (например, сообщение слишком старое) — посылаем новое.
# ────────────────────────────────────────────────────────────
async def _switch_message(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    msg = callback.message
    has_media = bool(msg.photo or msg.document or msg.video or msg.animation)
    try:
        if has_media:
            await msg.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await msg.edit_text(text, reply_markup=reply_markup)
        return
    except Exception as exc:
        logger.info('Edit failed, sending new message',
                   has_media=has_media, error=str(exc)[:200])
    try:
        await msg.answer(text, reply_markup=reply_markup)
    except Exception as exc:
        logger.warning('Could not send new message either', error=str(exc)[:200])


# ────────────────────────────────────────────────────────────
# Точки входа
# ────────────────────────────────────────────────────────────
@router.callback_query(F.data == 'edit_start')
async def cb_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню → 'Изменить фото'. Один путь: пришли фото → выбери стиль."""
    if not await _check_balance(callback, await effective_cost_for('quality')):
        return
    # Пол ещё не задан → сперва спросим (через каталог), иначе генерации
    # уйдут в женскую стилизацию для парня. Путь «сразу селфи» без пола ломал
    # гендер — фикс 26.05.
    if await _get_gender(callback.from_user.id) is None:
        await _open_catalog(callback)
        return
    await state.set_state(ImageEditState.waiting_photo)
    body = (
        '🪄 <b>Редактор фото</b>\n\n'
        '📎 <b>Пришли селфи или портрет</b> — открою большую коллекцию готовых настроений.\n\n'
        '✨ Стиль и портрет · 🌃 Сцены и города · 🔥 Дерзкие и яркие\n\n'
        '<i>Или просто опиши что хочешь — пойму.\n'
        'Лучше прислать файлом (без сжатия) — выше качество результата.</i>'
    )
    await _switch_message(callback, body, reply_markup=_CANCEL_KB)
    await callback.answer()


@router.callback_query(F.data.startswith('edit_again:'))
async def cb_edit_again(callback: CallbackQuery, state: FSMContext) -> None:
    """«Применить другой стиль к этому фото» — тот же оригинал, новый пресет/промпт."""
    import json as _json
    from redis.asyncio import Redis
    from app.config import settings as _settings

    key = callback.data.split(':', 1)[1]
    try:
        _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
        raw = await _redis.get(f'refine:{key}')
    except Exception:
        raw = None
    if not raw:
        await callback.answer('Сессия истекла — пришли фото заново', show_alert=True)
        return
    try:
        data = _json.loads(raw)
    except Exception:
        await callback.answer('Ошибка сессии', show_alert=True)
        return

    orig_file_id = data.get('orig')
    if not orig_file_id:
        await callback.answer('Оригинал фото не найден', show_alert=True)
        return

    await callback.answer()
    # Открываем НОВЫЙ каталог (категории → карусель), а не легаси-список пресетов.
    # Запоминаем фото в state как reuse_file_id — при выборе сцены из карточки
    # пресет применится к нему сразу, без повторной загрузки селфи.
    await state.set_state(None)
    await state.update_data(reuse_file_id=orig_file_id, quality_mode=None,
                            face_photos=[], mface_status_msg_id=None,
                            pending_gallery_preset=None)
    gender = await _get_gender(callback.from_user.id)
    kb = await _categories_kb(is_male=(gender == 'male'))
    await callback.message.answer(
        premium_text(
            '🎨 <b>Ещё стиль для этого фото</b>\n\n'
            '<i>Выбери настроение — применю к тому же фото, '
            'заново присылать не нужно 👇</i>'
        ),
        parse_mode='HTML', reply_markup=kb,
    )


@router.callback_query(F.data.startswith('refine_face:'))
async def cb_refine_face(callback: CallbackQuery, state: FSMContext) -> None:
    """ОТКЛЮЧЕНО 22.05.2026: face-swap-проход убран (давал лесенку, JPEG 4:2:0).
    Кнопка больше не показывается; обработчик оставлен, чтобы старые сообщения
    (в пределах 1ч Redis-TTL) не запускали face-swap, а мягко сообщали об этом."""
    await callback.answer(
        'Лицо уже переносится в высоком качестве при генерации — '
        'отдельный шаг больше не нужен.',
        show_alert=True,
    )
    return

    import json as _json  # noqa: код ниже мёртвый, оставлен для истории
    from redis.asyncio import Redis
    from app.config import settings as _settings

    key = callback.data.split(':', 1)[1]
    try:
        _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
        raw = await _redis.get(f'refine:{key}')
    except Exception:
        raw = None
    if not raw:
        await callback.answer('Сессия истекла — пришли фото заново', show_alert=True)
        return
    try:
        data = _json.loads(raw)
    except Exception:
        await callback.answer('Ошибка сессии', show_alert=True)
        return

    orig_file_id = data.get('orig')
    result_file_id = data.get('result')
    if not orig_file_id or not result_file_id:
        await callback.answer('Не хватает данных для refine', show_alert=True)
        return

    # Списание + проверка баланса
    eff_swap = await effective_face_swap_cost()
    if not await _check_balance(callback, eff_swap):
        return

    # Запускаем face_swap: scene = result (стилизованное), face = orig
    await state.set_state(ImageEditState.waiting_face_source)
    await state.update_data(
        base_file_id=result_file_id,   # сцена = результат edit'а
        face_file_id=orig_file_id,     # лицо = оригинальное фото юзера
    )
    await callback.answer('Применяю точное лицо…')

    # Сразу запускаем — не ждём от юзера ввода (мы уже имеем оба фото)
    await _run_face_swap(
        callback.message, state,
        extra_prompt='Place the face exactly as in the second image; preserve the styling and scene of the first image.',
        source_user_id=callback.from_user.id,
    )


@router.callback_query(F.data.startswith('tpl_face_swap:'))
async def cb_template_face_swap(callback: CallbackQuery, state: FSMContext) -> None:
    """Виральный шаблон: юзер тыкнул 'Сделать с моим лицом' под фото-шаблоном.
    Сразу ставим waiting_face_source с шаблоном как base — просим только фото лица."""
    from sqlalchemy import select as _select
    from app.database.models import PhotoTemplate

    try:
        tpl_id = int(callback.data.split(':', 1)[1])
    except (IndexError, ValueError):
        await callback.answer('Шаблон не найден', show_alert=True)
        return

    eff_cost = await effective_face_swap_cost()
    if not await _check_balance(callback, eff_cost):
        return

    async with AsyncSessionFactory() as session:
        tpl = await session.scalar(_select(PhotoTemplate).where(PhotoTemplate.id == tpl_id))
        if not tpl:
            await callback.answer('Шаблон не найден или удалён', show_alert=True)
            return
        # Инкрементируем click_count
        tpl.click_count += 1
        await session.commit()

    await state.set_state(ImageEditState.waiting_face_source)
    await state.update_data(base_file_id=tpl.file_id, photo_template_id=tpl_id)

    await callback.message.answer(
        '🎭 <b>Подставить твоё лицо в этот кадр</b>\n\n'
        f'<i>Стоимость:</i> <b>{eff_cost} кр</b>\n\n'
        '📎 Пришли <b>фото с твоим лицом</b> — то лицо, которое подставлю в шаблон.\n\n'
        '<i>Лучше всего: чёткий портрет анфас, при хорошем освещении.</i>',
        reply_markup=_CANCEL_KB,
    )
    await callback.answer('Шаблон выбран')


@router.callback_query(F.data == 'face_swap_start')
async def cb_face_swap_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Главное меню → 'Подставить лицо'."""
    eff_cost = await effective_face_swap_cost()
    if not await _check_balance(callback, eff_cost):
        return
    await state.set_state(ImageEditState.waiting_face_base)
    await _switch_message(
        callback,
        '🎭 <b>Подставить твоё лицо в чужое фото</b>\n\n'
        f'Стоимость: <b>{eff_cost} кредитов</b>.\n\n'
        '<b>Шаг 1 из 2 — пришли фото, где нужно заменить лицо</b>\n'
        '📎 Любая картинка с человеком или персонажем — на его лицо я подставлю твоё.\n\n'
        '<i>Примеры: кадр из фильма, обложка журнала, картина, фото знаменитости, '
        'персонаж аниме или игры.</i>\n\n'
        '⚠️ <i>Только своё лицо или с явного согласия человека. Без детей и откровенного контента.</i>',
        reply_markup=_CANCEL_KB,
    )
    await callback.answer()


@router.callback_query(F.data.in_({
    'photo_action:edit', 'photo_action:edit_fast', 'photo_action:edit_quality',
    'photo_action:face_swap',
}))
async def cb_photo_action(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопки 'Изменить / Быстро / Качество / Подставить лицо' после загрузки фото.
    file_id берём из FSM state (в callback_data не лезет — лимит Telegram 64 байта)."""
    action = callback.data.split(':', 1)[1]

    data = await state.get_data()
    file_id = data.get('file_id') or data.get('last_photo_file_id')
    if not file_id:
        await callback.answer('Фото не найдено в сессии. Пришли заново.', show_alert=True)
        return

    if action in ('edit', 'edit_fast', 'edit_quality'):
        # Единый режим quality. Старые сообщения юзеров могут содержать кнопку
        # «Быстро/Изменить» — ведём в НОВЫЙ каталог (категории/Новинки), а не в
        # легаси _presets_kb. Фикс 26.05.
        if not await _check_balance(callback, await effective_cost_for('quality')):
            return
        await callback.answer()
        await state.set_state(None)
        await state.update_data(reuse_file_id=file_id, file_id=file_id,
                                quality_mode='quality', face_photos=[],
                                pending_gallery_preset=None)
        await open_catalog_from_message(
            callback.message, callback.from_user.id,
            intro_text=('✅ <b>Фото принял!</b>\n\n'
                        '<i>Выбери сцену — применю к нему 👇</i>'),
        )
        return

    elif action == 'face_swap':
        eff_swap = await effective_face_swap_cost()
        if not await _check_balance(callback, eff_swap):
            return
        # Юзер прислал ЦЕЛЕВОЕ фото (где надо заменить лицо), просим фото с его лицом
        await state.set_state(ImageEditState.waiting_face_source)
        await state.update_data(base_file_id=file_id)
        await _switch_message(
            callback,
            '🎭 <b>Подставить лицо — Шаг 2 из 2</b>\n\n'
            f'<i>Стоимость:</i> <b>{eff_swap} кр</b>\n\n'
            '📎 Теперь пришли <b>фото с твоим лицом</b> — то лицо, которое я подставлю на первое фото.\n\n'
            '<i>Лучше всего работает чёткий портрет анфас, при хорошем освещении.</i>\n\n'
            '⚠️ <i>Только своё лицо или с явного согласия человека. Без детей и откровенного контента.</i>',
            reply_markup=_CANCEL_KB,
        )
        await callback.answer()


@router.message(Command('edit'))
async def cmd_edit(message: Message, state: FSMContext) -> None:
    """Юзер набрал /edit — запускает редактор."""
    user = await _get_user_or_start(message)
    if not user:
        return
    eff_q = await effective_cost_for('quality')
    # 24.05: было user.credits < cost — игнорировало burn-кредиты юзера.
    # Если у юзера 0 обычных + 200 burn, пресет за 100 кр должен пройти
    # (deduct_credits умеет тратить burn первым), а pre-check рубил его.
    from app.database.repo.credits import total_balance
    user_total = total_balance(user)
    if user_total < eff_q:
        await message.answer(
            f'🪄 Для редактирования фото нужно <b>{eff_q} кр</b>.\n'
            f'У тебя сейчас: <b>{user_total} кр</b>',
            reply_markup=_BUY_KB,
        )
        return
    await state.set_state(ImageEditState.waiting_photo)
    await message.answer(
        '🪄 <b>Редактор фото</b>\n\n'
        f'💎 Одна правка — <b>{eff_q} кр</b> (сцены, портреты, стиль, одежда, фон)\n\n'
        '📎 Пришли фото — потом выберешь сцену или скажешь что изменить.',
        reply_markup=_CANCEL_KB,
    )


@router.message(Command('faceswap'))
async def cmd_face_swap(message: Message, state: FSMContext) -> None:
    user = await _get_user_or_start(message)
    if not user:
        return
    eff_swap = await effective_face_swap_cost()
    from app.database.repo.credits import total_balance
    user_total = total_balance(user)
    if user_total < eff_swap:
        await message.answer(
            f'🎭 Для подстановки лица нужно <b>{eff_swap} кредитов</b>.\n'
            f'У тебя сейчас: <b>{user_total} кр</b>',
            reply_markup=_BUY_KB,
        )
        return
    await state.set_state(ImageEditState.waiting_face_base)
    await message.answer(
        '🎭 <b>Подставить твоё лицо в чужое фото — Шаг 1 из 2</b>\n\n'
        f'Стоимость: <b>{eff_swap} кредитов</b>.\n\n'
        '📎 Пришли <b>фото, где нужно заменить лицо</b> — любой человек, '
        'персонаж, картина или кадр.\n\n'
        '<i>На втором шаге попрошу фото с твоим лицом.</i>\n\n'
        '⚠️ <i>Только своё лицо или с согласия человека. Без детей и откровенного контента.</i>',
        reply_markup=_CANCEL_KB,
    )


def _nav_cb(idx: int, page_idx, is_male: bool = False) -> str:
    """callback_data навигации: gcat:<page|all|top>:<idx>:<m|f>.

    page_idx может быть int, None ('all') или 'top' (динамическая категория
    «Что выбирают сейчас» — топ по image_edited за 7д)."""
    if page_idx is None:
        p = 'all'
    elif page_idx == 'top':
        p = 'top'
    else:
        p = page_idx
    g = 'm' if is_male else 'f'
    return f'gcat:{p}:{idx}:{g}'


def _gallery_kb(idx: int, total: int, preset_key: str,
                 page_idx: int | None = None,
                 is_male: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура карусели. page_idx — внутри категории; is_male — мужской набор
    (несётся в callback навигации, чтобы листание не сбрасывало пол)."""
    rows: list[list[InlineKeyboardButton]] = []
    # Главное действие — попробовать этот пресет.
    rows.append([InlineKeyboardButton.model_construct(
        text='Хочу такое со своим лицом',
        callback_data=f'gallery_try:{preset_key}',
        icon_custom_emoji_id=PE_SPARKLES, style='success',
    )])
    # Навигация
    nav: list[InlineKeyboardButton] = []
    if idx > 0:
        nav.append(InlineKeyboardButton.model_construct(
            text=' ', callback_data=_nav_cb(idx - 1, page_idx, is_male),
            icon_custom_emoji_id=PE_LEFT,
        ))
    else:
        nav.append(InlineKeyboardButton.model_construct(
            text=' ', callback_data='gallery_noop', icon_custom_emoji_id=PE_LEFT,
        ))
    nav.append(InlineKeyboardButton(text=f'· {idx + 1} / {total} ·',
                                     callback_data='gallery_noop'))
    if idx < total - 1:
        nav.append(InlineKeyboardButton.model_construct(
            text=' ', callback_data=_nav_cb(idx + 1, page_idx, is_male),
            icon_custom_emoji_id=PE_RIGHT,
        ))
    else:
        nav.append(InlineKeyboardButton.model_construct(
            text=' ', callback_data='gallery_noop', icon_custom_emoji_id=PE_RIGHT,
        ))
    rows.append(nav)
    # «Назад»: внутри категории — к категориям, иначе — в главное меню.
    if page_idx is not None:
        rows.append([InlineKeyboardButton.model_construct(
            text='К категориям', callback_data='gallery_cats',
            icon_custom_emoji_id=PE_BACK, style='danger',
        )])
    else:
        rows.append([InlineKeyboardButton.model_construct(
            text='В главное меню', callback_data='main_menu',
            icon_custom_emoji_id=PE_BACK, style='danger',
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_top_preset_keys(limit: int = 7) -> list[str]:
    """Топ-N пресетов по image_edited за последние 7 дней (без тех-юзеров).
    Возвращает [] если данных мало (preset_key пишется только с 22.05). При
    наполнении автоматически появится категория «🔥 Что выбирают сейчас»."""
    from sqlalchemy import select as _sel, func
    from datetime import datetime, timezone, timedelta
    from app.database.models import BotEvent
    from app.database.repo.events import get_excluded_user_ids
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        async with AsyncSessionFactory() as session:
            exclude = await get_excluded_user_ids(session)
            expr = BotEvent.extra.op('->>')('preset_key')
            rows = (await session.execute(
                _sel(expr.label('k'), func.count().label('cnt'))
                .where(BotEvent.event_type == 'image_edited',
                       BotEvent.created_at >= cutoff,
                       expr.isnot(None),
                       BotEvent.user_id.notin_(exclude or [-1]))
                .group_by(expr)
                .order_by(func.count().desc())
                .limit(limit)
            )).all()
        real = [r.k for r in rows if r.k in EDIT_PRESETS]
    except Exception as exc:
        logger.warning('top preset query failed', error=str(exc)[:200])
        real = []

    # Реальных данных по выборам пока мало (бот молодой) → «Что выбирают сейчас»
    # наполняем стабильно-случайной подборкой пресетов с превью, которая
    # ротируется каждый день (seed = дата). Когда наберётся ≥5 реальных — они
    # идут вперёд, рандом лишь добивает до limit.
    if len(real) >= 5:
        return real[:limit]
    try:
        import random as _rnd
        from datetime import date as _date
        from app.database.repo.preset_previews import list_presets_with_previews
        async with AsyncSessionFactory() as session:
            pool = [k for k in await list_presets_with_previews(session) if k in EDIT_PRESETS]
        r = _rnd.Random(_date.today().toordinal())
        r.shuffle(pool)
        merged = real + [k for k in pool if k not in real]
        return merged[:limit]
    except Exception as exc:
        logger.warning('top preset random fill failed', error=str(exc)[:150])
        return real[:limit]


# ── Раздел «🆕 Новинки»: окно свежести + graduation ─────────────────────────
# Пресет «новый» ОГРАНИЧЕННОЕ время (NEW_WINDOW_DAYS), затем сам «выпускается»
# из Новинок в свою тематическую категорию. Иначе раздел = «N самых свежих за
# всё время», и пресет 2-месячной давности вечно числится новым. Параметры —
# в bot_config (крутятся без передеплоя). Свежесть — launched_at из реестра
# preset_launch (единая дата запуска для код- и DB-пресетов, не зависит от
# регенерации превью).
_NEW_DEFAULT_WINDOW_DAYS = 14
_NEW_DEFAULT_MIN = 4    # пол: добиваем до этого числа, чтобы раздел не «умирал»
_NEW_DEFAULT_MAX = 12   # потолок раздела

# Кэш — «Новинки» и бейдж «🆕» это ОДНО множество (состав раздела), чтобы
# бейдж был ограничен потолком (≤ MAX) и осмыслен. Иначе при массовой
# генерации превью одним днём «внутри окна» оказываются почти все пресеты, и
# бейдж лепится на всё = шум. Пересчёт: при reload_custom_presets и лениво по
# TTL (чтобы пресеты «выпускались» без рестарта).
_NEW_SECTION: list[str] = []            # состав раздела «Новинки», новейшие сверху
_BADGE_KEYS: set[str] = set()           # = set(_NEW_SECTION); для _label_for (sync)
_FRESH_CACHE_TS: float = 0.0
_FRESH_CACHE_TTL = 3600                  # 1ч


async def _read_int_config(key: str, default: int) -> int:
    from app.database.repo.bot_config import get_config_value
    try:
        async with AsyncSessionFactory() as session:
            raw = (await get_config_value(session, key) or '').strip()
        return max(0, int(raw)) if raw else default
    except Exception:
        return default


async def refresh_fresh_presets(force: bool = False) -> None:
    """Пересчитать состав раздела «Новинки» (окно свежести + пол/потолок).
    Логика graduation:
      • свежих (launched_at внутри окна NEW_WINDOW_DAYS) ≥ MIN → берём, срез MAX;
      • свежих мало → добиваем следующими по дате до MIN (раздел не пустеет);
      • всегда срез по MAX.
    Свежесть — launched_at из реестра preset_launch (единая дата запуска для
    код- и DB-пресетов, не зависит от регенерации превью).
    Дёшево. Лениво по TTL; force=True при reload/правках."""
    global _NEW_SECTION, _BADGE_KEYS, _FRESH_CACHE_TS
    if not force and _FRESH_CACHE_TS and (time.monotonic() - _FRESH_CACHE_TS) < _FRESH_CACHE_TTL:
        return
    from datetime import datetime, timezone, timedelta
    from app.database.repo.preset_launch import get_launch_map
    from app.database.repo.preset_previews import list_presets_with_previews
    try:
        window_days = await _read_int_config('new_window_days', _NEW_DEFAULT_WINDOW_DAYS)
        min_n = await _read_int_config('new_section_min', _NEW_DEFAULT_MIN)
        max_n = await _read_int_config('new_section_max', _NEW_DEFAULT_MAX)
        cap = max(1, max_n or _NEW_DEFAULT_MAX)
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        async with AsyncSessionFactory() as session:
            launch_map = await get_launch_map(session)
            preview_keys = set(await list_presets_with_previews(session))
        # Кандидаты: показываемы в каталоге (в EDIT_PRESETS) + есть превью +
        # известна дата запуска. Сортируем по launched_at (новейшие сверху).
        cand = [(k, launch_map[k]) for k in EDIT_PRESETS
                if k in preview_keys and launch_map.get(k) is not None]
        cand.sort(key=lambda kv: kv[1], reverse=True)
        recent = [k for k, _ in cand]
        fresh_in_order = [k for k, ts in cand if ts >= cutoff]
        if len(fresh_in_order) >= min_n:
            section = fresh_in_order[:cap]
        else:  # backfill до пола, но не выше потолка
            section = recent[:max(min_n, len(fresh_in_order))][:cap]
        _NEW_SECTION = section
        _BADGE_KEYS = set(section)
        _FRESH_CACHE_TS = time.monotonic()
    except Exception as exc:
        logger.warning('refresh_fresh_presets failed', error=str(exc)[:200])


async def _get_new_preset_keys(limit: int | None = None) -> list[str]:
    """Состав раздела «🆕 Новинки» (новейшие сверху). См. refresh_fresh_presets
    для логики окна/graduation. По умолчанию (limit=None) возвращает ВЕСЬ раздел
    (_NEW_SECTION уже ограничен new_section_max) — чтобы он совпадал с набором
    бейджей _BADGE_KEYS. limit задают только суженные surface'ы (напр. подсказки
    после результата), где показать меньше — ок."""
    try:
        await refresh_fresh_presets()
        return list(_NEW_SECTION) if limit is None else _NEW_SECTION[:limit]
    except Exception as exc:
        logger.warning('new preset query failed', error=str(exc)[:200])
        return []


async def _build_gallery_list(page_idx=None,
                               is_male: bool = False) -> list[tuple[str, str, str]]:
    """Возвращает [(preset_key, label, file_id), ...] для page_idx:
      None  — вся галерея в порядке PRESET_PAGES
      int   — конкретная категория
      'top' — динамический топ-7 за 7 дней (без тех-юзеров)

    is_male прячет _FEMALE_ONLY_PRESETS и берёт мужское превью (с фолбэком
    на женское если мужского ещё нет)."""
    from app.database.repo.preset_previews import list_presets_with_previews, get_preview_file_id
    await refresh_fresh_presets()  # обновить бейдж «🆕» для _label_for (лениво по TTL)
    if page_idx == 'top':
        top_keys = await _get_top_preset_keys()
        # Превращаем в pages-like структуру с одной страницей
        pages = [{'items': top_keys}]
    elif page_idx == 'new':
        pages = [{'items': await _get_new_preset_keys()}]
    elif page_idx is None:
        pages = PRESET_PAGES
    elif isinstance(page_idx, int) and 0 <= page_idx < len(PRESET_PAGES):
        pages = [PRESET_PAGES[page_idx]]
    else:
        pages = []
    position = 1 if is_male else 0
    async with AsyncSessionFactory() as session:
        keys_with = set(await list_presets_with_previews(session))
        items: list[tuple[str, str, str]] = []
        for page in pages:
            for k in page['items']:
                if is_male and k in _FEMALE_ONLY_PRESETS:
                    continue
                if k not in keys_with:
                    continue
                file_id = await get_preview_file_id(session, k, position=position)
                if not file_id and is_male:
                    file_id = await get_preview_file_id(session, k, position=0)
                if file_id:
                    items.append((k, _label_for(k, is_male), file_id))
    # «Показать все» (page_idx=None): новые (custom) сцены — первыми. Stable
    # sort сохраняет порядок внутри групп. В конкретной категории custom уже
    # впереди (front-insert при reload), доп. сортировка не нужна.
    if page_idx is None:
        items.sort(key=lambda it: it[0] not in _CUSTOM_KEYS)
    return items


async def _gallery_index_for_preset(preset_key: str) -> int | None:
    """Индекс пресета в галерее (порядок _build_gallery_list). None если пресета
    нет в каталоге (например без preview). Нужно чтобы deeplink из канала открывал
    карточку именно этого пресета, а не первую."""
    items = await _build_gallery_list()
    for i, (k, _, _) in enumerate(items):
        if k == preset_key:
            return i
    return None


async def _build_gallery_card(idx: int, page_idx: int | None = None,
                               is_male: bool = False):
    """Возвращает (file_id, caption, kb, total) для карточки idx внутри
    выбранной категории (page_idx) или всей галереи (None), с учётом пола.
    """
    items = await _build_gallery_list(page_idx, is_male)
    if not items:
        # Пусто (нет превью ИЛИ устаревший page_idx от старого меню с 7
        # категориями). Возвращаем kb с возвратом к категориям — иначе юзер
        # застревал на тексте без кнопок (баг 24.05).
        empty_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton.model_construct(
                text='К категориям', callback_data='gallery_cats',
                icon_custom_emoji_id=PE_BACK, style='danger',
            )
        ]])
        return None, '📭 <b>Тут пока пусто.</b>\n\nОткрой каталог заново 👇', empty_kb, 0
    idx = max(0, min(idx, len(items) - 1))
    key, label, file_id = items[idx]
    quality_mode = PRESET_QUALITY_MAP.get(key, 'quality')
    cost = await effective_cost_for(quality_mode)
    cat_hint = ''
    # page_idx может быть int (категория PRESET_PAGES), None ('Показать все
    # подряд') или 'top' (динамическая «Что выбирают сейчас»). isinstance-
    # проверка спасает от TypeError при сравнении 'top' с int — был баг 24.05
    # «кнопка популярных пресетов удаляет сообщение и ничего не показывает».
    if isinstance(page_idx, int) and 0 <= page_idx < len(PRESET_PAGES):
        cat_hint = f"\n<i>Категория: {PRESET_PAGES[page_idx]['title']}</i>"
    elif page_idx == 'top':
        cat_hint = f"\n<i>Категория: 🔥 Что выбирают сейчас</i>"
    elif page_idx == 'new':
        cat_hint = f"\n<i>Категория: 🆕 Новинки</i>"
    caption = (
        f'🪄 <b>{label}</b>  ·  <i>{cost} кр</i>{cat_hint}\n\n'
        f'<i>Так выглядит результат этого настроения. '
        f'Хочешь — пришлю фото с твоим лицом за минуту.</i>'
    )
    kb = _gallery_kb(idx, len(items), key, page_idx, is_male)
    return file_id, caption, kb, len(items)


async def _send_gallery_card(target_msg: Message, idx: int,
                              page_idx: int | None = None,
                              is_male: bool = False) -> None:
    """Отправляет НОВОЕ сообщение с карточкой. page_idx=None — общая галерея."""
    file_id, caption, kb, _ = await _build_gallery_card(idx, page_idx, is_male)
    if file_id is None:
        # kb теперь содержит «К категориям» — прокидываем, чтобы не застрять.
        await target_msg.answer(caption, parse_mode='HTML', reply_markup=kb)
        return
    try:
        await target_msg.answer_photo(photo=file_id, caption=caption,
                                       parse_mode='HTML', reply_markup=kb)
    except Exception as exc:
        logger.warning('gallery card send failed', idx=idx, error=str(exc)[:200])
        await target_msg.answer('⚠ Не удалось показать пример. Попробуй ещё раз.')


async def _edit_gallery_card(msg: Message, idx: int,
                              page_idx: int | None = None,
                              is_male: bool = False) -> bool:
    """Заменяет ФОТО+CAPTION+KB в существующем сообщении (для навигации без
    спама). Returns True при успехе."""
    from aiogram.types import InputMediaPhoto
    file_id, caption, kb, _ = await _build_gallery_card(idx, page_idx, is_male)
    if file_id is None:
        return False
    try:
        await msg.edit_media(
            InputMediaPhoto(media=file_id, caption=caption, parse_mode='HTML'),
            reply_markup=kb,
        )
        return True
    except Exception as exc:
        logger.warning('gallery card edit failed', idx=idx, error=str(exc)[:200])
        return False


# Премиум-иконки категорий — анимированные у premium-юзеров. Все ID
# верифицированы и определены в app/keyboards/main.py.
_CATEGORY_PE_ICONS: dict[str, str] = {
    '✨ Стиль и портрет':    PE_SPARKLES,
    '🌃 Сцены и города':     PE_CITY,        # 🌇 закатный город
    '🔥 Дерзкие и яркие':    PE_FIRE,
}


async def _categories_kb(is_male: bool = False) -> InlineKeyboardMarkup:
    """Меню категорий. Показывает страницы с видимыми превью; мужчинам не
    считаем жёстко-женские пресеты. Пол несётся в callback (gcat:...:m|f).

    Если за 7д набралось >=3 пресета с реальными генерациями (не от тех-юзеров)
    — добавляем динамическую категорию «🔥 Что выбирают сейчас» первой
    строкой. Это соц-доказательство и помощь paralysis-of-choice."""
    import re
    from app.database.repo.preset_previews import list_presets_with_previews
    async with AsyncSessionFactory() as session:
        keys_with = set(await list_presets_with_previews(session))
    g = 'm' if is_male else 'f'
    rows: list[list[InlineKeyboardButton]] = []

    def _page_button(i: int, page: dict):
        """Кнопка категории i (или None если нет видимых сцен для этого пола)."""
        n = sum(1 for k in page['items']
                if k in keys_with and not (is_male and k in _FEMALE_ONLY_PRESETS))
        if n == 0:
            return None
        title = page['title']
        icon = _CATEGORY_PE_ICONS.get(title)
        if icon:
            clean = re.sub(r'^\W+', '', title).strip()
            return InlineKeyboardButton.model_construct(
                text=f'{clean} · {n}', callback_data=f'gcat:{i}:0:{g}',
                icon_custom_emoji_id=icon)
        return InlineKeyboardButton.model_construct(
            text=f'{title} · {n}', callback_data=f'gcat:{i}:0:{g}')

    # 0. «🆕 Новинки» — свежедобавленные сцены ПЕРВОЙ строкой, новейшие сверху.
    #    Чтобы вернувшийся клиент сразу видел новое, а не думал «всё уже смотрел»
    #    (видя знакомые старые на входе). Весь раздел = набор бейджей (потолок —
    #    new_section_max в конфиге).
    new_keys = await _get_new_preset_keys()
    if is_male:
        new_keys = [k for k in new_keys if k not in _FEMALE_ONLY_PRESETS]
    if new_keys:
        rows.append([InlineKeyboardButton.model_construct(
            text=f'🆕 Новинки · {len(new_keys)}',
            callback_data=f'gcat:new:0:{g}', style='success',
        )])

    # 1. «Что выбирают сейчас» — динамический топ (соц-доказательство), если ≥3.
    top_keys = await _get_top_preset_keys(limit=7)
    if is_male:
        top_keys = [k for k in top_keys if k not in _FEMALE_ONLY_PRESETS]
    top_keys = [k for k in top_keys if k in keys_with]
    if len(top_keys) >= 3:
        rows.append([InlineKeyboardButton.model_construct(
            text=f'Что выбирают сейчас · {len(top_keys)}',
            callback_data=f'gcat:top:0:{g}',
            icon_custom_emoji_id=PE_FIRE, style='success',
        )])

    # 2. Тематические категории (внутри каждой новые пресеты уже первыми —
    #    front-insert в reload_custom_presets).
    for i, page in enumerate(PRESET_PAGES):
        b = _page_button(i, page)
        if b:
            rows.append([b])
    # «Показать все» — резерв для тех, кто хочет лента-режим как раньше.
    rows.append([InlineKeyboardButton.model_construct(
        text='Показать все сцены подряд', callback_data=f'gcat:all:0:{g}',
        icon_custom_emoji_id=PE_FRAME,
    )])
    rows.append([InlineKeyboardButton.model_construct(
        text='В главное меню', callback_data='main_menu',
        icon_custom_emoji_id=PE_BACK, style='danger',
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_gender(tg_id: int) -> str | None:
    """Пол юзера ('male'/'female'/'any') или None если ещё не спросили."""
    async with AsyncSessionFactory() as session:
        user = await get_user(session, tg_id)
        return user.gender if user else None


def _gender_kb() -> InlineKeyboardMarkup:
    """Одноразовый вопрос пола перед первым показом каталога."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Я девушка', callback_data='setgender:female',
            icon_custom_emoji_id=PE_SPARKLES, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Я парень', callback_data='setgender:male',
            icon_custom_emoji_id=PE_ROBOT, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='Не важно — покажи всё', callback_data='setgender:any',
        )],
    ])


@router.message(Command('gallery'))
async def cmd_gallery(message: Message) -> None:
    """Открыть каталог примеров — листай и выбирай настроение. Через
    open_catalog_from_message → если пол ещё не задан, спросим (иначе
    карусель «все подряд» захардкожена на женский набор и мужчинам
    показывает не то)."""
    await open_catalog_from_message(message, message.from_user.id)


async def _open_catalog(callback: CallbackQuery) -> None:
    """Показать каталог: если пол ещё не указан — спросить; иначе меню категорий."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    gender = await _get_gender(callback.from_user.id)
    if gender is None:
        await callback.message.answer(
            premium_text(
                '🖼 <b>Для кого делаем фото?</b>\n\n'
                '<i>Покажу подходящие сцены и подберу стиль под тебя. '
                'Сменить можно в любой момент — в разделе «Ещё».</i>'
            ),
            parse_mode='HTML', reply_markup=_gender_kb(),
        )
        return
    kb = await _categories_kb(is_male=(gender == 'male'))
    cap = premium_text(
        '🖼 <b>Выбери настроение</b>\n\n'
        '<i>В каждом разделе — готовые сцены с твоим лицом. Открой любой, '
        'листай примеры и пришли селфи 👇</i>'
    )
    await _send_catalog_header(callback.message, cap, kb)


async def _send_catalog_header(message: Message, caption: str, kb) -> None:
    """Шлёт меню категорий с визуальной шапкой-коллажем (showcase новинок;
    фолбэк — welcome-коллаж; фолбэк — чистый текст). Чтобы «Выбери настроение»
    не был скучным текстом без картинок."""
    fid = None
    try:
        from app.database.repo.bot_config import get_config_value as _gcv
        async with AsyncSessionFactory() as _s:
            fid = await _gcv(_s, 'showcase_collage_fid')
        if not fid:
            from app.services.welcome_collage import get_or_build_welcome_collage_fid
            fid = await get_or_build_welcome_collage_fid(message.bot)
    except Exception:
        fid = None
    if fid:
        try:
            await message.answer_photo(fid, caption=caption, parse_mode='HTML',
                                       reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(caption, parse_mode='HTML', reply_markup=kb)


async def open_catalog_from_message(message: Message, user_tg_id: int,
                                     intro_text: str | None = None) -> None:
    """Открыть НОВЫЙ каталог из обычного Message (не callback). Нужна, когда
    юзер из главного меню шлёт фото/файл — мы хотим показать категории, не
    легаси-список. Сначала спрашивает пол, если ещё не указан. Параметр
    intro_text — вступление под контекст («применю к этому фото»); если пол
    ещё не выбран, intro_text встраивается ПЕРЕД вопросом про пол, чтобы
    юзер видел подтверждение приёма фото (иначе он думает, что бот молчит)."""
    gender = await _get_gender(user_tg_id)
    if gender is None:
        prefix = (intro_text.strip() + '\n\n') if intro_text else ''
        await message.answer(
            premium_text(
                prefix +
                '🖼 <b>Сначала — для кого делаем фото?</b>\n\n'
                '<i>Подберу сцены и стиль под тебя. '
                'Сменить можно в любой момент — в разделе «Ещё».</i>'
            ),
            parse_mode='HTML', reply_markup=_gender_kb(),
        )
        return
    kb = await _categories_kb(is_male=(gender == 'male'))
    text = intro_text or (
        '🖼 <b>Выбери настроение</b>\n\n'
        '<i>Листай примеры свободно — селфи попрошу только когда выберешь '
        'конкретную сцену. Ничего не присылай заранее 👇</i>'
    )
    await _send_catalog_header(message, premium_text(text), kb)


# ── Посадочные под рекламные посевы (?start=seedN) ──────────────────────────
# Bare ?start=seedN не несёт промокода → раньше падал в дефолтный welcome, и
# обещание ролика терялось (воронка: 9 кликов → 1 селфи → 0 генераций). Теперь:
# hero под текст конкретного креатива → вопрос пола → ВЕСЬ каталог сцен,
# отфильтрованный под пол (в роликах сцен много — не фиксируем набор).
# Превью берутся из preset_previews — новых генераций fal НЕ требуется.
SEED_LANDING_HERO: dict[str, str] = {
    'seed2': (  # Ж-креатив «Эстетичная лента»
        '🔥 <b>Эстетичная лента — без единой фотосессии.</b>\n\n'
        'Кидаешь селфи → получаешь готовые кадры для сторис и профиля: '
        'глянец, Париж, неон. Лицо <b>в точности твоё</b>, не чужая модель. '
        'Минута на каждый.\n\n'
        '<b>Собери ленту, на которую подписываются 👇</b>'
    ),
    'seed3': (  # М+Ж-креатив «Окажись где угодно»
        '🐹 <b>Окажись где угодно — не вставая с дивана.</b>\n\n'
        'Крыша небоскрёба, неон ночного города, кино-кадр. Одно селфи — и ты там. '
        'Лицо <b>в точности твоё</b>, не чужая модель.\n\n'
        '<b>Выбери, где хочешь оказаться 👇</b>'
    ),
    'seed1': (  # М-креатив «Перестань выглядеть как все» (посев остановлен,
                # посадочная — на случай перезапуска с новым хуком)
        '⚠️ <b>Перестань выглядеть как все.</b>\n\n'
        'Селфи в зеркале проигрывает, даже когда ты круче. Нейра даёт фото '
        'уровня, который тебя реально продаёт: неон, авто, ночной город. '
        'Лицо <b>твоё</b>.\n\n'
        '<b>Подними планку 👇</b>'
    ),
}


def _seed_gender_kb(seed: str) -> InlineKeyboardMarkup:
    """Вопрос пола на посадочной посева. Пол + seed несутся в callback, чтобы
    после ответа открыть каталог под нужный пол, не теряя контекст креатива."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Я девушка', callback_data=f'seedg:{seed}:female',
            icon_custom_emoji_id=PE_SPARKLES, style='success')],
        [InlineKeyboardButton.model_construct(
            text='Я парень', callback_data=f'seedg:{seed}:male',
            icon_custom_emoji_id=PE_ROBOT, style='primary')],
        [InlineKeyboardButton.model_construct(
            text='Не важно — покажи всё', callback_data=f'seedg:{seed}:any')],
    ])


async def send_seed_landing(message: Message, seed: str, gift: int = 0) -> bool:
    """Посадочная под рекламный посев: hero-текст креатива + вопрос пола.
    gift — подарок новичку (для строки «+N кр»). Возвращает False если seed
    неизвестен (вызывающий тогда покажет дефолтный welcome)."""
    hero = SEED_LANDING_HERO.get(seed)
    if not hero:
        return False
    gift_line = (
        f'\n\n🎁 <b>+{gift} кредитов в подарок</b> — хватит на пару кадров.'
        if gift >= 200 else ''
    )
    text = premium_text(
        hero + gift_line +
        '\n\n<i>Сначала — для кого делаем фото? Подберу сцены под тебя.</i>'
    )
    # Коллаж-шапка как в welcome — живее голого текста (фолбэк на текст).
    fid = None
    try:
        from app.database.repo.bot_config import get_config_value as _gcv
        async with AsyncSessionFactory() as _s:
            fid = await _gcv(_s, 'showcase_collage_fid')
        if not fid:
            from app.services.welcome_collage import get_or_build_welcome_collage_fid
            fid = await get_or_build_welcome_collage_fid(message.bot)
    except Exception:
        fid = None
    kb = _seed_gender_kb(seed)
    if fid:
        try:
            await message.answer_photo(fid, caption=text, parse_mode='HTML',
                                       reply_markup=kb)
            return True
        except Exception:
            pass
    await message.answer(text, parse_mode='HTML', reply_markup=kb)
    return True


@router.callback_query(F.data.startswith('seedg:'))
async def cb_seed_gender(callback: CallbackQuery) -> None:
    """Пол выбран на посадочной посева → сохраняем и открываем каталог под пол.
    Каталог gender-aware: мужчинам прячем женские пресеты и берём мужские
    превью. Так юзер видит ВСЕ релевантные сцены, а не фикс-набор."""
    parts = callback.data.split(':')
    seed = parts[1] if len(parts) > 1 else ''
    g = parts[2] if len(parts) > 2 else 'any'
    if g not in ('male', 'female', 'any'):
        g = 'any'
    uid = None
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        if user:
            uid = user.id
            user.gender = g
            await session.commit()
    # Воронка посева: пол выбран. Source=seedN связывает с seed_landing_shown.
    if uid is not None:
        log_event_bg(uid, 'seed_gender_chosen', source=seed, gender=g)
    await callback.answer('Запомнил 👌')
    try:
        await callback.message.delete()
    except Exception:
        pass
    kb = await _categories_kb(is_male=(g == 'male'))
    intro = premium_text(
        '✨ <b>Лови сцены под тебя</b>\n\n'
        '<i>Открой раздел, листай готовые кадры и пришли селфи — верну фото '
        'с твоим лицом за минуту. Свежие сцены сверху 👇</i>'
    )
    await _send_catalog_header(callback.message, intro, kb)


@router.callback_query(F.data.startswith('gallery_open'))
async def cb_gallery_open(callback: CallbackQuery, state: FSMContext) -> None:
    """Открыть каталог (2 уровня: категории → карусель).

    Два входа:
    • plain `gallery_open` (меню/колесо/старт) — каталог с нуля, фото ещё нет.
    • `gallery_open:{refine_key}` (кнопка «Все сцены каталога» на экране
      результата) — подтягиваем ОРИГИНАЛ селфи из refine-кэша в reuse_file_id,
      чтобы выбранный из каталога пресет применился к тому же фото без
      повторной загрузки (как делают quickscene-кнопки). Раньше handler не
      принимал state и фото терялось после state.clear() в _run_edit → юзера
      просили заново прислать селфи. Это и был сломанный «выбор нового пресета».
    """
    await callback.answer()
    # Разбираем опциональный refine_key из callback_data.
    refine_key = callback.data.split(':', 1)[1] if ':' in callback.data else None
    orig = None
    if refine_key:
        try:
            import json as _json
            from redis.asyncio import Redis
            from app.config import settings as _settings
            _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
            raw = await _redis.get(f'refine:{refine_key}')
            await _redis.aclose()
            if raw:
                orig = (_json.loads(raw) or {}).get('orig')
        except Exception as exc:
            logger.warning('gallery_open refine read failed', error=str(exc)[:150])
    if orig:
        # Тот же оригинал → пресет из каталога применится сразу.
        await state.update_data(reuse_file_id=orig, face_photos=[],
                                pending_gallery_preset=None)
    else:
        # Каталог открыт без фото-контекста — гасим возможный «прилипший»
        # reuse_file_id от прошлой брошенной сессии, иначе пресет применился бы
        # к чужому/устаревшему фото вместо запроса нового селфи.
        await state.update_data(reuse_file_id=None)
    # Фикс мёртвой серии (24.05): юзеры заходят, тапают «Сделать фото» и
    # никогда не вызывают /start или /status — update_streak ни разу не
    # дёргался. Серии у всех = 1 навсегда. Теперь дёргаем тихо на любом
    # «реальном действии», milestone-бонус приходит отдельным push'ем.
    await _maybe_celebrate_streak(callback)
    await _open_catalog(callback)


async def _maybe_celebrate_streak(callback: CallbackQuery) -> None:
    """Обновляет streak и, если совпал milestone — шлёт юзеру отдельным
    сообщением празднование с +кр. Безопасно (try/except) — не валит handler."""
    try:
        from app.database.repo.credits import update_streak
        async with AsyncSessionFactory() as session:
            u = await get_user(session, callback.from_user.id)
            if not u:
                return
            days, bonus = await update_streak(session, u)
            await session.commit()
        if bonus > 0:
            await callback.message.answer(
                premium_text(
                    f'🔥 <b>{days} дней подряд!</b> Держи <b>+{bonus} кр</b> '
                    f'в подарок за серию.'
                ),
                parse_mode='HTML',
            )
    except Exception as exc:
        logger.warning('streak update failed', error=str(exc)[:200])


@router.callback_query(F.data == 'gallery_cats')
async def cb_gallery_categories(callback: CallbackQuery) -> None:
    """«К категориям» из карточки — снова меню категорий (с учётом пола)."""
    await callback.answer()
    await _open_catalog(callback)


@router.callback_query(F.data.startswith('bc_open:'))
async def cb_bc_open(callback: CallbackQuery) -> None:
    """Click-tracking для CTA-кнопки в рассылках + открытие каталога.

    Раньше тут всегда зовётся `cb_change_gender` → юзер с уже выбранным
    полом получал повторный вопрос «Для кого фото?», а коллаж рассылки
    удалялся. Теперь:
      • инкрементим click_count
      • снимаем кнопку (чтобы не нажали дважды), но НЕ удаляем фото
        рассылки — оно ценный визуальный контекст
      • если пол ещё не задан → показываем gender_kb новым сообщением
      • если пол уже задан → сразу шлём меню категорий
    """
    try:
        bc_id = int(callback.data.split(':', 1)[1])
        async with AsyncSessionFactory() as session:
            from app.database.models import Broadcast
            row = await session.get(Broadcast, bc_id)
            if row is not None:
                row.click_count = (row.click_count or 0) + 1
                await session.commit()
    except Exception as exc:
        logger.warning('bc_open click tracking failed', error=str(exc)[:200])
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    gender = await _get_gender(callback.from_user.id)
    if gender is None:
        await callback.message.answer(
            premium_text(
                '🖼 <b>Для кого делаем фото?</b>\n\n'
                '<i>Подберу подходящие сцены и стиль под тебя. '
                'Сменить можно в любой момент — в разделе «Ещё».</i>'
            ),
            parse_mode='HTML', reply_markup=_gender_kb(),
        )
        return
    kb = await _categories_kb(is_male=(gender == 'male'))
    await callback.message.answer(
        premium_text(
            '🖼 <b>Выбери настроение</b>\n\n'
            '<i>Открой раздел, листай примеры и пришли селфи 👇</i>'
        ),
        parse_mode='HTML', reply_markup=kb,
    )


@router.callback_query(F.data == 'change_gender')
async def cb_change_gender(callback: CallbackQuery) -> None:
    """Сменить пол из «Ещё» — снова показываем выбор."""
    await callback.answer()
    cur = await _get_gender(callback.from_user.id)
    cur_ru = {'male': 'парень', 'female': 'девушка', 'any': 'не важно'}.get(cur, 'не выбрано')
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        premium_text(
            f'⚙️ <b>Для кого делаем фото?</b>\n\n'
            f'<i>Сейчас: {cur_ru}. Выбери — обновлю каталог и стиль под тебя.</i>'
        ),
        parse_mode='HTML', reply_markup=_gender_kb(),
    )


@router.callback_query(F.data.startswith('setgender:'))
async def cb_set_gender(callback: CallbackQuery, state: FSMContext) -> None:
    """Сохранить выбранный пол и показать меню категорий. Если в state уже
    лежит `reuse_file_id` (юзер скинул фото из главного меню ДО выбора пола) —
    меняем текст на «применю к твоему фото», иначе зовём прислать селфи."""
    g = callback.data.split(':', 1)[1]
    if g not in ('male', 'female', 'any'):
        g = 'any'
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        if user:
            user.gender = g
            await session.commit()
    await callback.answer('Запомнил 👌')
    try:
        await callback.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    has_reuse = bool(data.get('reuse_file_id'))
    kb = await _categories_kb(is_male=(g == 'male'))
    if has_reuse:
        text = (
            '🖼 <b>Выбери настроение</b>\n\n'
            '<i>Применю к фото, которое ты уже прислал — заново слать не нужно. '
            'Открой раздел и жми «Хочу такое».</i>'
        )
    else:
        text = (
            '🖼 <b>Выбери настроение</b>\n\n'
            '<i>Открой раздел, листай примеры и пришли селфи 👇</i>'
        )
    await callback.message.answer(premium_text(text), parse_mode='HTML', reply_markup=kb)


@router.callback_query(F.data.startswith('gcat:'))
async def cb_gallery_cat(callback: CallbackQuery) -> None:
    """Навигация по карусели. Формат: gcat:<page|all|top>:<idx>:<m|f>.
    Пол несётся в callback — листание не сбрасывает мужской/женский набор.
    page='top' — динамическая категория «🔥 Что выбирают сейчас»."""
    parts = callback.data.split(':')
    try:
        page_part, idx = parts[1], int(parts[2])
        if page_part == 'all':
            page_idx = None
        elif page_part == 'top':
            page_idx = 'top'
        elif page_part == 'new':
            page_idx = 'new'
        else:
            page_idx = int(page_part)
        is_male = (len(parts) > 3 and parts[3] == 'm')
    except (ValueError, IndexError):
        await callback.answer()
        return
    await callback.answer()
    # Текстовое меню → шлём новую фото-карточку (удалив меню). Фото → edit на месте.
    is_text = callback.message and callback.message.text is not None
    if is_text:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await _send_gallery_card(callback.message, idx=idx, page_idx=page_idx, is_male=is_male)
        return
    edited = await _edit_gallery_card(callback.message, idx=idx, page_idx=page_idx, is_male=is_male)
    if not edited:
        await _send_gallery_card(callback.message, idx=idx, page_idx=page_idx, is_male=is_male)


@router.callback_query(F.data.startswith('gallery:'))
async def cb_gallery_nav(callback: CallbackQuery) -> None:
    """Legacy навигация по полной галерее (gallery:<idx>). Сохраняем для
    обратной совместимости с уже отправленными ботом сообщениями."""
    try:
        idx = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    await callback.answer()
    edited = await _edit_gallery_card(callback.message, idx=idx, page_idx=None)
    if not edited:
        await _send_gallery_card(callback.message, idx=idx, page_idx=None)


@router.callback_query(F.data == 'gallery_noop')
async def cb_gallery_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == 'mystery_today')
async def cb_mystery_today(callback: CallbackQuery, state: FSMContext) -> None:
    """Mystery Preset of the Day — бесплатное применение пресета дня.

    Один раз в сутки, без списания кредитов. Атомарно claim'им через Redis
    SETNX до отправки превью — если юзер передумает, день «сгорает» (это
    осознанная цена за защиту от спама). Показываем превью ЭТОГО же пресета
    (пол-aware) — юзер видит, что именно получит, а не угадывает по названию."""
    from app.services.mystery_preset import get_today_mystery, claim, is_used_today

    mystery = await get_today_mystery()
    if not mystery:
        await callback.answer('Сегодня без сюрприза, загляни завтра', show_alert=True)
        return
    if await is_used_today(callback.from_user.id):
        await callback.answer('Сегодня уже забирал — заходи завтра 🎲', show_alert=True)
        return
    key = mystery['preset_key']
    preset = EDIT_PRESETS.get(key)
    if not preset:
        await callback.answer('Не нашёл сегодняшний сюрприз', show_alert=True)
        return
    if not await claim(callback.from_user.id):
        await callback.answer('Уже забрал сегодня 🎲', show_alert=True)
        return
    preset_mode = PRESET_QUALITY_MAP.get(key, 'quality')

    await state.set_state(ImageEditState.waiting_photo)
    await state.update_data(preset_key=key, quality_mode=preset_mode,
                            pending_gallery_preset=key, mystery_free=True,
                            face_photos=[], mface_status_msg_id=None)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Превью того же пресета, привязанное к выбранному полу (position 1=муж,
    # 0=жен; фолбэк на женское если мужского нет, затем на текст). Юзер видит
    # конкретный результат сюрприза, а не угадывает по названию.
    gender = await _get_gender(callback.from_user.id)
    is_male = (gender == 'male')
    position = 1 if is_male else 0
    # Название тоже под пол (оба оверрайда): «Валькирия»→«Викинг» и т.п. Без
    # 🆕-бейджа — контекст сюрприза, не каталог.
    disp_label = _gender_label(key, is_male)
    file_id = None
    try:
        from app.database.repo.preset_previews import get_preview_file_id
        async with AsyncSessionFactory() as session:
            file_id = await get_preview_file_id(session, key, position=position)
            if not file_id and position == 1:
                file_id = await get_preview_file_id(session, key, position=0)
    except Exception as exc:
        logger.warning('mystery preview lookup failed', key=key, error=str(exc)[:150])

    caption = premium_text(
        f'🎲 <b>Сюрприз дня: {disp_label}</b> — бесплатно ✨\n\n'
        f'<b>Пришли селфи или портрет</b> — применю за минуту.\n\n'
        f'<i>Сегодня без списания кредитов. Завтра — новый сюрприз.</i>'
    )
    if file_id:
        try:
            await callback.message.answer_photo(
                file_id, caption=caption, parse_mode='HTML', reply_markup=_CANCEL_KB,
            )
            return
        except Exception as exc:
            logger.warning('mystery preview send failed', key=key, error=str(exc)[:150])
    await callback.message.answer(caption, parse_mode='HTML', reply_markup=_CANCEL_KB)


@router.callback_query(F.data.startswith('gallery_try:'))
async def cb_gallery_try(callback: CallbackQuery, state: FSMContext) -> None:
    """Из каталога — «Хочу такое». Переводим юзера в edit_start с выбранным пресетом
    в state, после того как пришлёт фото — пресет применится автоматически."""
    key = callback.data.split(':', 1)[1]
    preset = EDIT_PRESETS.get(key)
    if not preset:
        await callback.answer('Шаблон не найден', show_alert=True)
        return
    label = preset[0]
    preset_mode = PRESET_QUALITY_MAP.get(key, 'quality')
    cost = await effective_cost_for(preset_mode)

    # Проверка баланса заранее — если не хватит, сразу шлём upsell
    if not await _check_balance(callback, cost):
        return

    # Re-style: фото уже в сессии (пришли из «Ещё стиль для этого фото»).
    # Применяем выбранный пресет к тому же фото без повторной загрузки селфи.
    data = await state.get_data()
    reuse_file_id = data.get('reuse_file_id')
    if reuse_file_id:
        _, prompt = preset
        if prompt is None:
            # Пресет требует текстового уточнения (одежда/фон). Фото уже есть —
            # просим только уточнение, переходим в waiting_prompt.
            await state.set_state(ImageEditState.waiting_prompt)
            await state.update_data(preset_key=key, quality_mode=preset_mode,
                                    file_id=reuse_file_id, face_photos=[reuse_file_id],
                                    reuse_file_id=None, pending_gallery_preset=None)
            hint_map = {
                'change_clothes': 'В какую одежду одеть?\n<i>Пример: «деловой костюм», «красное платье»</i>',
                'change_bg': 'Какой фон поставить?\n<i>Пример: «пляж на закате», «офис из стекла»</i>',
            }
            await callback.message.answer(
                f'🪄 <b>{label}</b> · <i>{cost} кр</i>\n\n{hint_map.get(key, "Напиши уточнение:")}',
                parse_mode='HTML', reply_markup=_CANCEL_KB,
            )
            await callback.answer()
            return
        await state.set_state(None)
        await state.update_data(preset_key=key, quality_mode=preset_mode,
                                file_id=reuse_file_id, face_photos=[reuse_file_id],
                                reuse_file_id=None, pending_gallery_preset=None)
        await callback.answer('Применяю к твоему фото…')
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            premium_text(f'🪄 Применяю <b>{label}</b> к твоему фото…'),
            parse_mode='HTML',
        )
        await _run_edit(callback.message, state, prompt,
                        source_user_id=callback.from_user.id,
                        quality_mode=preset_mode)
        return

    await state.set_state(ImageEditState.waiting_photo)
    # Сбрасываем мульти-селфи буфер прошлой (возможно брошенной) сессии, чтобы
    # фото от другого пресета не «прилипли» к новому.
    await state.update_data(preset_key=key, quality_mode=preset_mode,
                            pending_gallery_preset=key,
                            face_photos=[], mface_status_msg_id=None)
    await callback.message.answer(
        f'📎 <b>{label}</b> · <i>{cost} кр</i>\n\n'
        f'<b>Пришли селфи или портрет</b> — применю настроение через минуту.\n\n'
        f'<b>✅ Хорошо:</b>\n'
        f'• лицо <b>анфас</b>, смотришь в камеру\n'
        f'• хорошее освещение (день или ровный свет)\n'
        f'• видны плечи, не только лицо впритык\n\n'
        f'<b>❌ Хуже выйдет:</b>\n'
        f'• большие очки, маска, шапка по брови\n'
        f'• сильная обрезка лица (нет лба или подбородка)\n'
        f'• сильно повёрнутая голова / тёмное фото\n\n'
        f'<i>💡 Можно прислать <b>2-3 ракурса</b> подряд — точнее воспроизведу лицо.</i>',
        parse_mode='HTML',
        reply_markup=_CANCEL_KB,
    )
    await callback.answer()


def _multi_face_kb(n: int) -> InlineKeyboardMarkup:
    """Только две кнопки: главная «Запустить с N фото» (зелёная) и «Отмена».
    «Добавить ещё ракурс» как кнопка лишняя — юзер просто шлёт ещё фото без
    нажатия. Раньше с тремя кнопками в чате был лишний шум."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text=f'✨ Запустить генерацию ({n} фото)',
            callback_data='mface_run',
            icon_custom_emoji_id=PE_SPARKLES, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Отмена', callback_data='edit_cancel',
            icon_custom_emoji_id=PE_CROSS, style='danger',
        )],
    ])


@router.callback_query(F.data == 'mface_add')
async def cb_mface_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Юзер хочет добавить ещё селфи. State остаётся waiting_photo, просим ещё фото."""
    data = await state.get_data()
    n = len(data.get('face_photos') or [])
    left = max(1, 3 - n)
    await callback.message.answer(
        f'📎 <b>Жду ещё селфи</b> · можно добавить до {left}.\n\n'
        f'<i>Другой ракурс — профиль, чуть сверху или с другой мимикой. Каждый '
        f'дополнительный кадр улучшит точность лица.</i>',
        parse_mode='HTML', reply_markup=_CANCEL_KB,
    )
    await callback.answer()


@router.callback_query(F.data == 'mface_run')
async def cb_mface_run(callback: CallbackQuery, state: FSMContext) -> None:
    """Юзер сказал «хватит, запускай». Применяем pending пресет ко всем собранным фото."""
    data = await state.get_data()
    pending = data.get('pending_gallery_preset')
    face_photos = data.get('face_photos') or []
    if not pending or pending not in EDIT_PRESETS or not face_photos:
        await callback.answer('Сессия истекла — пришли фото заново', show_alert=True)
        return
    label, prompt = EDIT_PRESETS[pending]
    preset_mode = data.get('quality_mode') or PRESET_QUALITY_MAP.get(pending, 'quality')
    await state.update_data(preset_key=pending, quality_mode=preset_mode,
                            pending_gallery_preset=None)
    n = len(face_photos)
    suffix = ' с одним фото' if n == 1 else f' с {n} ракурсами'
    await callback.answer()
    # Редактируем статус-сообщение (убираем кнопки, чтобы не тапнули повторно).
    launch = premium_text(f'🪄 Запускаю <b>{label}</b>{suffix}…')
    try:
        await callback.message.edit_text(launch, parse_mode='HTML')
    except Exception:
        await callback.message.answer(launch, parse_mode='HTML')
    await _run_edit(callback.message, state, prompt,
                    source_user_id=callback.from_user.id,
                    quality_mode=preset_mode)


@router.callback_query(F.data == 'edit_cancel')
async def cb_edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from app.keyboards.main import main_menu_kb_for_user
    from app.handlers.start import build_menu_text, _send_welcome_with_collage
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        text = await build_menu_text(session, callback.message.bot, user)
    # «Отмена» = возврат в главное меню → с welcome-коллажем (как cb_main_menu).
    try:
        await callback.message.delete()
    except Exception:
        pass
    kb = await main_menu_kb_for_user(
        callback.from_user.id, user.credits if user else 0,
    )
    await _send_welcome_with_collage(callback.message, text, kb)
    await callback.answer('Отменено')


@router.callback_query(F.data.startswith('edit_page:'))
async def cb_edit_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Листание страниц меню пресетов. FSM-state не меняем — данные о фото
    остаются в state.data; меняется только видимая страница.

    Если в state есть file_id (юзер пришёл с фото) — рендерим клавиатуру с
    photo-action кнопками. Иначе — сухое меню пресетов."""
    try:
        page_idx = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    page_idx = max(0, min(page_idx, len(PRESET_PAGES) - 1))

    data = await state.get_data()
    has_photo = bool(data.get('file_id') or data.get('last_photo_file_id'))
    if has_photo:
        from app.handlers.vision import _photo_action_kb
        kb = await _photo_action_kb(page_idx=page_idx)
    else:
        kb = _presets_kb(page_idx)

    header = _presets_header(page_idx)
    try:
        await callback.message.edit_text(header, reply_markup=kb)
    except Exception:
        # Сообщение слишком старое / медиа-caption — отдаём новое
        await callback.message.answer(header, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == 'edit_page_noop')
async def cb_edit_page_noop(callback: CallbackQuery) -> None:
    """Безмолвный ack на тап по индикатору страницы (X/Y)."""
    await callback.answer()


# ────────────────────────────────────────────────────────────
# FSM: режим редактирования (1 фото)
# ────────────────────────────────────────────────────────────

# Per-chat lock для multi-selfie: альбом (3 фото за раз) приходит почти
# одновременно отдельными апдейтами — aiogram обрабатывает их конкурентно.
# Без сериализации read-modify-write state гонится: фото теряются и
# дублируется статус-сообщение. Lock делает обработку фото в чате
# последовательной. dict не чистим — Lock на чат весит копейки.
_mface_locks: dict[int, asyncio.Lock] = {}


def _mface_lock(chat_id: int) -> asyncio.Lock:
    lock = _mface_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _mface_locks[chat_id] = lock
    return lock


@router.message(ImageEditState.waiting_photo, F.photo | F.document)
async def receive_edit_photo(message: Message, state: FSMContext) -> None:
    file_id = _extract_file_id(message)
    if not file_id:
        # iOS бывает шлёт .heic с mime=None — fallback по расширению уже учтён
        # в _extract_file_id. Если всё ещё None — реально не картинка.
        await message.answer(
            '❌ Не вижу картинки. Пришли <b>фото</b> или <b>картинку-файл</b> '
            '(jpg, png, heic). Если шлёшь .heic с айфона — попробуй также '
            '«Сжать изображение» при отправке.',
            parse_mode='HTML',
        )
        return
    try:
        await _receive_edit_photo_impl(message, state, file_id)
    except Exception as exc:
        # Раньше exception в multi-selfie loop падал тихо — юзер не видел ничего.
        logger.warning('receive_edit_photo crashed', error=str(exc)[:300])
        try:
            await message.answer(
                '⚠ <b>Не удалось обработать фото.</b>\n\n'
                'Попробуй прислать ещё раз. Если .heic — отправь через '
                '«Сжать изображение» (тогда придёт как обычное фото).',
                parse_mode='HTML',
            )
        except Exception:
            pass


async def _receive_edit_photo_impl(message: Message, state: FSMContext, file_id: str) -> None:
    data = await state.get_data()
    pending = data.get('pending_gallery_preset')
    # Funnel-событие: юзер реально прислал селфи. Закрывает слепое пятно между
    # регистрацией и image_edited — теперь видно где утечка (загрузил→не сгенерил).
    try:
        from app.database.base import AsyncSessionFactory
        from app.database.repo.users import get_user
        from app.database.repo.events import log_event
        async with AsyncSessionFactory() as _s:
            _u = await get_user(_s, message.from_user.id)
        if _u:
            await log_event(_u.id, 'selfie_uploaded',
                            has_pending_preset=bool(pending))
    except Exception as exc:
        logger.warning('selfie_uploaded event log failed', error=str(exc)[:150])
    # Диагностический лог — увидеть в проде что именно приходит и в каком state
    doc = message.document
    logger.info('receive_edit_photo',
                user_id=message.from_user.id, has_photo=bool(message.photo),
                has_doc=bool(doc),
                doc_mime=(doc.mime_type if doc else None),
                doc_name=(doc.file_name if doc else None),
                pending=pending,
                pending_known=(pending in EDIT_PRESETS) if pending else False,
                face_photos_n=len(data.get('face_photos') or []))

    # Gallery-flow: multi-selfie (до 3 ракурсов). Под локом, со свежим чтением
    # state — иначе альбом из 3 фото гонится.
    if pending and pending in EDIT_PRESETS:
        async with _mface_lock(message.chat.id):
            # Перечитываем state ВНУТРИ лока — предыдущее чтение могло устареть
            # (другое фото из альбома уже изменило/запустило сессию).
            data = await state.get_data()
            pending = data.get('pending_gallery_preset')
            if not pending or pending not in EDIT_PRESETS:
                # Раньше был silent return → юзер видел тишину. Теперь явный
                # ответ + клавиатура «начать заново», иначе он не понимает что
                # делать. Это покрывает: альбом-race, сброс pending после запуска,
                # пресет был удалён через дашборд между cb_gallery_try и фото.
                logger.info('receive_edit_photo: pending lost mid-flow',
                            user_id=message.from_user.id, pending=pending)
                await message.answer(
                    '⚠ <b>Сессия редактирования сброшена.</b>\n\n'
                    'Открой каталог и выбери сцену заново — селфи уже не нужно '
                    'повторно слать в этой сессии.',
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton.model_construct(
                            text='Открыть каталог',
                            callback_data='gallery_open',
                            icon_custom_emoji_id=PE_SPARKLES, style='success',
                        ),
                    ]]),
                )
                return

            label, prompt = EDIT_PRESETS[pending]
            if prompt is None:
                await state.update_data(file_id=file_id)
                await state.set_state(ImageEditState.waiting_prompt)
                await message.answer(_PRESETS_HEADER, reply_markup=_presets_kb())
                return

            face_photos = list(data.get('face_photos') or [])
            face_photos.append(file_id)
            face_photos = face_photos[:3]  # hard cap
            n = len(face_photos)
            await state.update_data(file_id=file_id, face_photos=face_photos)
            status_id = data.get('mface_status_msg_id')

            if n >= 3:
                # Потолок — запускаем автоматически. pending сбрасываем ДО _run_edit,
                # чтобы соседнее фото на локе увидело None и не запустило повторно.
                preset_mode = data.get('quality_mode') or PRESET_QUALITY_MAP.get(pending, 'quality')
                await state.update_data(preset_key=pending, quality_mode=preset_mode,
                                        pending_gallery_preset=None)
                launch_text = premium_text(f'🪄 Запускаю <b>{label}</b> с 3 ракурсами…')
                if status_id:
                    try:
                        await message.bot.edit_message_text(launch_text, chat_id=message.chat.id,
                                                           message_id=status_id, parse_mode='HTML')
                    except Exception:
                        await message.answer(launch_text, parse_mode='HTML')
                else:
                    await message.answer(launch_text, parse_mode='HTML')
                await _run_edit(message, state, prompt,
                                source_user_id=message.from_user.id,
                                quality_mode=preset_mode)
                return

            # Один свежий статус ВНИЗУ чата: удаляем старый (если есть) и шлём
            # новый. Так юзер всегда видит актуальный счётчик и кнопку прямо
            # под последним прислnным фото — никаких reply'ев и указателей
            # на несуществующие кнопки.
            if status_id:
                try:
                    await message.bot.delete_message(message.chat.id, status_id)
                except Exception:
                    pass
            text = premium_text(
                f'✅ <b>Принял · {n}/3</b>\n\n'
                f'<i>Можно прислать ещё ракурс — или жми кнопку чтобы '
                f'сгенерировать сейчас.</i>'
            )
            sent = await message.answer(text, parse_mode='HTML', reply_markup=_multi_face_kb(n))
            await state.update_data(mface_status_msg_id=sent.message_id)
            return

    # Freeform-flow (не из галереи): показываем НОВЫЙ каталог (категории →
    # Новинки), а НЕ легаси-список _presets_kb. Фото кладём как reuse_file_id —
    # выбранная сцена применится к нему без повторной загрузки. Фикс 26.05:
    # раньше тут открывалось «старое меню пресетов».
    await state.set_state(None)
    await state.update_data(reuse_file_id=file_id, file_id=file_id,
                            quality_mode=None, face_photos=[],
                            pending_gallery_preset=None)
    await open_catalog_from_message(
        message, message.from_user.id,
        intro_text=('✅ <b>Фото принял!</b>\n\n'
                    '<i>Теперь выбери сцену — применю к нему, заново слать '
                    'не нужно 👇</i>'),
    )


@router.message(ImageEditState.waiting_photo)
async def reject_non_photo_edit(message: Message) -> None:
    await message.answer('📎 Жду фото. Пришли картинку или файл с изображением.', reply_markup=_CANCEL_KB)


@router.callback_query(ImageEditState.waiting_prompt, F.data.startswith('edit_preset:'))
async def cb_edit_preset(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(':', 1)[1]
    preset = EDIT_PRESETS.get(key)
    if not preset:
        await callback.answer('Неизвестный шаблон', show_alert=True)
        return
    label, prompt = preset
    # Если пользователь явно выбрал режим — уважаем его. Иначе используем
    # рекомендуемый по карте пресетов.
    data = await state.get_data()
    preset_mode = data.get('quality_mode') or PRESET_QUALITY_MAP.get(key, 'quality')

    if prompt is None:
        # Спрашиваем уточнение у юзера
        await state.update_data(preset_key=key, quality_mode=preset_mode)
        hint_map = {
            'change_clothes': 'В какую одежду одеть?\n<i>Пример: "деловой костюм", "красное платье", "косуха и джинсы"</i>',
            'change_bg': 'Какой фон поставить?\n<i>Пример: "пляж на закате", "офис из стекла", "лес осенью"</i>',
        }
        cost = _cost_for(preset_mode)
        await _switch_message(
            callback,
            f'🪄 <b>{label}</b> · <i>{cost} кр</i>\n\n{hint_map.get(key, "Напиши уточнение:")}',
            reply_markup=_CANCEL_KB,
        )
        await callback.answer()
        return

    # Готовый промпт. Сохраняем preset_key в state (нужен для caption + auto-apply).
    await state.update_data(preset_key=key, quality_mode=preset_mode)

    # Если есть preview-картинка — показываем её сначала с кнопкой подтверждения.
    # Юзер видит ВЫГОДУ до списания кредитов = выше доверие, меньше «не так получилось».
    # Если preview нет — старый flow (сразу запускаем).
    from app.database.repo.preset_previews import get_preview_file_id
    async with AsyncSessionFactory() as session:
        preview_file_id = await get_preview_file_id(session, key)

    if preview_file_id:
        cost = await effective_cost_for(preset_mode)
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton.model_construct(
                text=f'Создать с моим селфи · {cost} кр',
                callback_data=f'edit_apply:{key}',
                icon_custom_emoji_id=PE_SPARKLES, style='success',
            )],
            [InlineKeyboardButton.model_construct(
                text='Назад к выбору', callback_data='edit_page:0',
                icon_custom_emoji_id=PE_LEFT,
            )],
        ])
        try:
            await callback.message.answer_photo(
                photo=preview_file_id,
                caption=(
                    f'🪄 <b>{label}</b>\n\n'
                    f'<i>Так будут выглядеть результаты этого настроения. '
                    f'Реальное фото получишь со своим лицом.</i>'
                ),
                parse_mode='HTML',
                reply_markup=confirm_kb,
            )
            await callback.answer()
            return
        except Exception as exc:
            # Если file_id протух или TG не принял — fallback на прямой запуск
            logger.warning('preset preview failed, running directly',
                           preset_key=key, error=str(exc)[:200])

    # Нет preview → запускаем сразу (старое поведение)
    await callback.answer('Запускаю…')
    await _run_edit(callback.message, state, prompt,
                    source_user_id=callback.from_user.id,
                    quality_mode=preset_mode)


@router.callback_query(F.data.startswith('edit_apply:'))
async def cb_edit_apply(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение запуска пресета после показа preview.
    Восстанавливает preset_key + quality_mode из callback_data и state."""
    key = callback.data.split(':', 1)[1]
    preset = EDIT_PRESETS.get(key)
    if not preset:
        await callback.answer('Шаблон не найден', show_alert=True)
        return
    label, prompt = preset
    if prompt is None:
        # Этот пресет требует уточнения — preview-flow для него не должен был сработать
        await callback.answer('Нужно уточнение для этого пресета', show_alert=True)
        return

    data = await state.get_data()
    preset_mode = data.get('quality_mode') or PRESET_QUALITY_MAP.get(key, 'quality')
    await state.update_data(preset_key=key, quality_mode=preset_mode)
    await callback.answer('Запускаю…')
    await _run_edit(callback.message, state, prompt,
                    source_user_id=callback.from_user.id,
                    quality_mode=preset_mode)


@router.message(ImageEditState.waiting_prompt, F.text)
async def receive_edit_prompt(message: Message, state: FSMContext) -> None:
    text = (message.text or '').strip()
    if not text:
        await message.answer('❌ Напиши что хочешь изменить.', reply_markup=_CANCEL_KB)
        return

    data = await state.get_data()

    # TTL для auto-edit сессии. Если юзер залил фото и через час прислал
    # «погода в Москве» — это явно не edit-запрос. Сбрасываем сессию и просим
    # юзера прислать фото заново, если хотел редактировать. Кредиты не списываем.
    started_at = data.get('photo_session_started_at')
    if started_at and (time.time() - started_at) > PHOTO_AUTO_EDIT_TTL_S:
        await state.clear()
        await message.answer(
            'ℹ️ <b>Сессия редактирования истекла</b>\n\n'
            'Прошло больше 5 минут с момента загрузки фото — '
            'на всякий случай не запускаю редактирование автоматически.\n\n'
            'Пришли фото заново, если хочешь его отредактировать.',
        )
        return

    preset_key = data.get('preset_key')
    if preset_key and preset_key in _PRESET_PROMPT_TEMPLATES:
        prompt = _PRESET_PROMPT_TEMPLATES[preset_key].format(user=text)
    else:
        prompt = text

    await _run_edit(message, state, prompt, source_user_id=message.from_user.id)


# ────────────────────────────────────────────────────────────
# FSM: face swap (2 фото)
# ────────────────────────────────────────────────────────────
@router.message(ImageEditState.waiting_face_base, F.photo | F.document)
async def receive_face_base(message: Message, state: FSMContext) -> None:
    file_id = _extract_file_id(message)
    if not file_id:
        await message.answer('❌ Пришли фото (как картинку или файл).')
        return
    await state.update_data(base_file_id=file_id)
    await state.set_state(ImageEditState.waiting_face_source)
    await message.answer(
        '🎭 <b>Подставить лицо — Шаг 2 из 2</b>\n\n'
        '📎 Теперь пришли <b>фото с твоим лицом</b> — то лицо, которое подставлю на первое фото.\n\n'
        '<i>Лучше всего: чёткий портрет анфас, хорошее освещение.</i>\n\n'
        '⚠️ <i>Только своё лицо или с явного согласия человека.</i>',
        reply_markup=_CANCEL_KB,
    )


@router.message(ImageEditState.waiting_face_base)
async def reject_non_photo_face_base(message: Message) -> None:
    await message.answer(
        '📎 Жду фото, на котором надо заменить лицо. Пришли картинку или файл.',
        reply_markup=_CANCEL_KB,
    )


@router.message(ImageEditState.waiting_face_source, F.photo | F.document)
async def receive_face_source(message: Message, state: FSMContext) -> None:
    file_id = _extract_file_id(message)
    if not file_id:
        await message.answer('❌ Пришли фото с лицом.')
        return
    await state.update_data(face_file_id=file_id)
    # Запускаем сразу — без дополнительного промпта, он опционален и может быть передан
    # текстом в caption. Делаем UX простым: 2 фото → результат.
    extra = (message.caption or '').strip()
    await _run_face_swap(message, state, extra_prompt=extra, source_user_id=message.from_user.id)


@router.message(ImageEditState.waiting_face_source)
async def reject_non_photo_face_source(message: Message) -> None:
    await message.answer('📎 Жду фото с лицом. Пришли картинку или файл.', reply_markup=_CANCEL_KB)


# ────────────────────────────────────────────────────────────
# Внутренние функции
# ────────────────────────────────────────────────────────────
_IMG_EXTS = ('.heic', '.heif', '.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')


def _extract_file_id(message: Message) -> str | None:
    """Извлечь file_id из фото или документа-картинки. iOS Telegram иногда шлёт
    HEIC БЕЗ mime_type или как `application/octet-stream` — поэтому если mime
    не похож на картинку, дополнительно проверяем расширение имени файла."""
    if message.photo:
        return message.photo[-1].file_id
    doc = message.document
    if not doc:
        return None
    mime = (doc.mime_type or '').lower()
    if mime.startswith('image/'):
        return doc.file_id
    # iOS fallback: mime отсутствует/octet-stream → проверяем расширение
    name = (doc.file_name or '').lower()
    if name.endswith(_IMG_EXTS):
        return doc.file_id
    return None


async def _check_balance(callback: CallbackQuery, cost: int) -> bool:
    """Проверяет что у юзера хватает кредитов. Если нет — показывает экран с покупкой и
    возвращает False. Иначе True."""
    user_tg_id = callback.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await callback.answer('Нажми /start', show_alert=True)
            return False
        from app.services import channel_service
        await channel_service.grant_daily_aware(callback.bot, session, user)
        from app.database.repo.credits import total_balance
        credits = total_balance(user)  # включает burn — deduct_credits умеет их тратить
        await session.commit()

    if credits < cost:
        await _switch_message(
            callback,
            f'🪄 Нужно <b>{cost} кредитов</b>.\n'
            f'У тебя: <b>{credits} кр</b>\n\n'
            f'Пополни баланс — и редактируй фото без ограничений.',
            reply_markup=_BUY_KB,
        )
        await callback.answer()
        return False
    return True


async def _get_user_or_start(message: Message):
    """Достаёт User для command-handler-а. Если нет — просит /start."""
    async with AsyncSessionFactory() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return None
        from app.services import channel_service
        await channel_service.grant_daily_aware(message.bot, session, user)
        await session.commit()
        return user


async def _download_telegram_file(bot, file_id: str) -> bytes | None:
    """Скачивает файл из Telegram по file_id. None при ошибке."""
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        return buf.getvalue()
    except Exception as exc:
        logger.warning('TG file download failed', error=str(exc)[:200], file_id=file_id[:20])
        return None


async def _heartbeat_loop(bot, chat_id: int, status_msg, action_label: str, stop: asyncio.Event) -> None:
    """Каждые ~12 сек обновляет статус-сообщение с прошедшим временем и
    держит индикатор 'отправляет фото' живым. Если bot или edit упадут — глотаем тихо."""
    elapsed = 0
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=12.0)
                return  # stop event set during wait
            except asyncio.TimeoutError:
                pass
            elapsed += 12
            try:
                await bot.send_chat_action(chat_id=chat_id, action='upload_photo')
            except Exception:
                pass
            # Спокойные подсказки — юзер не должен волноваться по таймеру
            if elapsed <= 45:
                note = 'Уже думаю над твоим фото.'
            elif elapsed <= 90:
                note = 'Прорабатываю детали.'
            elif elapsed <= 150:
                note = 'Ещё немного — финальные штрихи.'
            elif elapsed <= 240:
                note = 'Сложная правка — почти готово.'
            elif elapsed <= 360:
                note = 'Ещё чуть-чуть — обязательно дойду до результата.'
            else:
                note = 'Финиширую — спасибо за терпение.'
            try:
                await status_msg.edit_text(
                    f'{action_label}\n\n'
                    f'<i>Прошло {elapsed} сек · {note}</i>'
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        return


@router.callback_query(F.data.startswith('quickscene:'))
async def cb_quickscene(callback: CallbackQuery, state: FSMContext) -> None:
    """Пост-результатный луп: применить конкретную НОВУЮ сцену к тому же селфи
    (orig из refine-кэша) одним тапом. Формат: quickscene:{refine_key}:{preset}."""
    try:
        _, refine_key, preset_key = callback.data.split(':', 2)
    except ValueError:
        await callback.answer('Ошибка', show_alert=True)
        return
    orig = None
    try:
        import json as _json
        from redis.asyncio import Redis
        from app.config import settings as _settings
        _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
        raw = await _redis.get(f'refine:{refine_key}')
        if raw:
            orig = (_json.loads(raw) or {}).get('orig')
    except Exception:
        orig = None
    if not orig:
        await callback.answer('Сессия истекла — пришли фото заново', show_alert=True)
        return
    if preset_key not in EDIT_PRESETS:
        await callback.answer('Сцена недоступна', show_alert=True)
        return
    _label, prompt = EDIT_PRESETS[preset_key]
    qmode = PRESET_QUALITY_MAP.get(preset_key, 'quality')
    await state.set_state(None)
    await state.update_data(
        file_id=orig, face_photos=[orig], preset_key=preset_key,
        quality_mode=qmode, mystery_free=False, reuse_file_id=None,
        pending_gallery_preset=None,
    )
    await callback.answer('Делаю новую сцену…')
    await _run_edit(callback.message, state, prompt, callback.from_user.id,
                    quality_mode=qmode)


@router.callback_query(F.data.startswith('freescene:'))
async def cb_freescene(callback: CallbackQuery, state: FSMContext) -> None:
    """Подарок дня в один тап с экрана результата: применить сегодняшний
    бесплатный пресет к ТОМУ ЖЕ селфи БЕЗ списания (claim + mystery_free).
    Формат: freescene:{refine_key}. Главный драйвер 2-й генерации."""
    refine_key = callback.data.split(':', 1)[1]
    from app.services.mystery_preset import get_today_mystery, claim, is_used_today
    mystery = await get_today_mystery()
    if not mystery or mystery['preset_key'] not in EDIT_PRESETS:
        await callback.answer('Подарок дня сейчас недоступен', show_alert=True)
        return
    orig = None
    try:
        import json as _json
        from redis.asyncio import Redis
        from app.config import settings as _settings
        _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
        raw = await _redis.get(f'refine:{refine_key}')
        if raw:
            orig = (_json.loads(raw) or {}).get('orig')
    except Exception:
        orig = None
    if not orig:
        await callback.answer('Сессия истекла — пришли фото заново', show_alert=True)
        return
    if await is_used_today(callback.from_user.id):
        await callback.answer('Сегодняшний подарок уже забрал — следующий завтра 🎁', show_alert=True)
        return
    if not await claim(callback.from_user.id):
        await callback.answer('Сегодняшний подарок уже забрал — следующий завтра 🎁', show_alert=True)
        return
    preset_key = mystery['preset_key']
    _label, prompt = EDIT_PRESETS[preset_key]
    qmode = PRESET_QUALITY_MAP.get(preset_key, 'quality')
    await state.set_state(None)
    await state.update_data(
        file_id=orig, face_photos=[orig], preset_key=preset_key,
        quality_mode=qmode, mystery_free=True, reuse_file_id=None,
        pending_gallery_preset=None,
    )
    await callback.answer('Делаю бесплатную сцену дня…')
    await _run_edit(callback.message, state, prompt, callback.from_user.id,
                    quality_mode=qmode)


@router.callback_query(F.data.startswith('retry_edit:'))
async def cb_retry_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Повтор провалившейся правки тем же фото, но в режиме Качество.
    Контекст (фото+промпт+пресет) берём из retry:{key} в Redis (TTL 1ч)."""
    key = callback.data.split(':', 1)[1]
    _redis = None
    try:
        import json as _json
        from redis.asyncio import Redis
        from app.config import settings as _settings
        _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
        raw = await _redis.get(f'retry:{key}')
    except Exception:
        raw = None
    if not raw:
        await callback.answer('Сессия истекла — пришли фото заново', show_alert=True)
        return
    # Одноразовый ключ: удаляем СРАЗУ → дабл-клик не приведёт к двойному списанию
    # (второй клик увидит «сессия истекла»). Если повтор тоже упадёт — ветка
    # провала в _run_edit создаст новый ключ + кнопку.
    try:
        await _redis.delete(f'retry:{key}')
    except Exception:
        pass
    try:
        data = _json.loads(raw)
    except Exception:
        await callback.answer('Ошибка сессии', show_alert=True)
        return
    face_photos = data.get('face_photos') or []
    prompt = data.get('prompt') or ''
    preset_key = data.get('preset_key')
    mode = data.get('mode') or 'quality'
    if not face_photos or not prompt:
        await callback.answer('Не хватает данных для повтора', show_alert=True)
        return
    # Если падал бесплатный «Подарок дня» — повтор тоже бесплатный: при провале
    # claim был откачен (release), поэтому заново занимаем слот. Не удалось занять
    # (уже использован сегодня) → повтор платный, чтобы не раздавать второй free.
    mystery_free = bool(data.get('mystery_free'))
    if mystery_free:
        from app.services.mystery_preset import claim as _mystery_claim
        mystery_free = await _mystery_claim(callback.from_user.id)
    # Восстанавливаем контекст в state.
    await state.update_data(
        face_photos=face_photos, file_id=face_photos[0],
        preset_key=preset_key, quality_mode=mode, mystery_free=mystery_free,
    )
    await callback.answer('Повторяю…' if not mystery_free else 'Повторяю бесплатно…')
    await _run_edit(callback.message, state, prompt, callback.from_user.id,
                    quality_mode=mode)


async def _acquire_gen_lock(user_id: int) -> bool:
    """Анти-даблтап: True если генерация для юзера не идёт (лок взят). nx + TTL
    180с как бэкстоп — если процесс упадёт, лок сам протухнет, юзер не
    заблокирован навсегда. Redis недоступен → fail-open (не блокируем)."""
    try:
        from redis.asyncio import Redis
        from app.config import settings as _s
        r = Redis.from_url(_s.REDIS_URL, decode_responses=True)
        try:
            return bool(await r.set(f'genlock:{user_id}', '1', nx=True, ex=180))
        finally:
            await r.aclose()
    except Exception:
        return True


async def _release_gen_lock(user_id: int) -> None:
    try:
        from redis.asyncio import Redis
        from app.config import settings as _s
        r = Redis.from_url(_s.REDIS_URL, decode_responses=True)
        try:
            await r.delete(f'genlock:{user_id}')
        finally:
            await r.aclose()
    except Exception:
        pass


async def _run_edit(
    message: Message, state: FSMContext, prompt: str,
    source_user_id: int, quality_mode: str | None = None,
) -> None:
    """Тонкая обёртка вокруг генерации с анти-даблтап локом: один юзер = одна
    генерация одновременно. Раньше двойной тап по кнопке сцены запускал две
    генерации и списывал кредиты дважды. Лок снимается по завершении (и успех,
    и провал) — следующую генерацию можно начинать сразу после результата."""
    if not await _acquire_gen_lock(source_user_id):
        await message.answer(
            '⏳ Я ещё доделываю твоё предыдущее фото — дождись результата, '
            'потом жми ещё раз.'
        )
        return
    try:
        await _run_edit_inner(message, state, prompt, source_user_id, quality_mode)
    finally:
        await _release_gen_lock(source_user_id)


async def _run_edit_inner(
    message: Message, state: FSMContext, prompt: str,
    source_user_id: int, quality_mode: str | None = None,
) -> None:
    """Списывает кредиты, запускает edit_image, возвращает результат или рефанд.

    quality_mode: 'fast' (mini-only), 'quality' (full), либо None → авто-детект.
    Цена quality учитывает active promo (-100 кр до image_promo_until).
    """
    data = await state.get_data()
    file_id = data.get('file_id')
    # face_photos: список всех ракурсов от юзера (multi-selfie). Если пусто —
    # fallback на single file_id для backward-совместимости со старыми флоу.
    face_photos: list[str] = list(data.get('face_photos') or [])
    if not face_photos and file_id:
        face_photos = [file_id]
    # Сохраняем preset_key до state.clear() — нужен для русского label в caption
    preset_key = data.get('preset_key')
    # Mystery Preset of the Day: бесплатное применение, кредиты не списываются.
    # claim() уже произошёл в cb_mystery_today, здесь только пропускаем deduct.
    mystery_free = bool(data.get('mystery_free'))
    # Берём mode из state (если выбран кнопкой), потом аргумент функции, потом auto
    if quality_mode is None:
        quality_mode = data.get('quality_mode')
    # Единый режим: всё, что не precise, нормализуем в quality (fast убран).
    if quality_mode != 'precise':
        quality_mode = 'quality'

    cost = 0 if mystery_free else await effective_cost_for(quality_mode)
    await state.clear()

    if not face_photos:
        await message.answer('❌ Фото не найдено в сессии. Пришли заново.')
        return

    # Guard: пустой/None промпт. Пресеты change_clothes/change_bg хранят prompt=None
    # (нужно текстовое уточнение — это умеет cb_gallery_try). Но quickscene/freescene
    # зовут _run_edit НАПРЯМУЮ: без этой проверки строка native_wm делала `None + str`
    # (TypeError) уже ПОСЛЕ показа анимации → «Подкручиваю реальность» висло без
    # результата и без ошибки (ровно симптом «анимация есть, фото нет»). Кредиты
    # здесь ещё не списаны (deduct ниже) — рефанд не нужен.
    if not prompt:
        if preset_key is not None:
            await message.answer(
                '✏️ Этот образ нужно уточнить текстом (что надеть / какой фон). '
                'Открой его в каталоге и напиши детали — применю к этому же фото.'
            )
        else:
            await message.answer('❌ Пустой запрос. Напиши, что сделать с фото.')
        return

    _MODE_LABEL_RU = {'quality': 'качественная', 'precise': 'с точным лицом'}

    # Guard: проверяем баланс fal.ai ДО списания кредитов.
    # precise = nano-banana-2 + face-swap, quality = nano-banana-2.
    # Оба требуют fal — если баланс упал, показываем юзеру понятное сообщение
    # вместо абстрактного «ошибка». Fast идёт через OpenRouter — не блокируем.
    if quality_mode in ('quality', 'precise'):
        from app.services import fal_balance
        if quality_mode == 'precise' and not await fal_balance.is_precise_allowed():
            await message.answer(
                '🛠 <b>Опция «Точное лицо» временно недоступна</b>\n\n'
                'Мы кратко обновляем серверы face-swap модели. Вернёмся '
                'через несколько часов.\n\n'
                '💡 Попробуй пресет из категории <b>«Стилизация»</b> (Vogue, '
                'Cinema, Cyberpunk, Ghibli) — они работают как обычно.\n\n'
                '<i>Кредиты не списались.</i>',
                parse_mode='HTML',
            )
            return
        if not await fal_balance.is_fal_allowed():
            await message.answer(
                '🛠 <b>Редактор фото временно недоступен</b>\n\n'
                'Обновляем серверы. Вернёмся через несколько часов — '
                'ты получишь уведомление.\n\n'
                '<i>Кредиты не списались.</i>',
                parse_mode='HTML',
            )
            return

    # Списание (для mystery_free пропускаем — Mystery дня бесплатен)
    async with AsyncSessionFactory() as session:
        user = await get_user(session, source_user_id)
        if not user:
            await message.answer('Нажми /start.')
            return
        if not mystery_free:
            from app.database.repo.credits import total_balance
            user_total = total_balance(user)
            if user_total < cost:
                await message.answer(
                    f'🪄 Нужно <b>{cost} кр</b> ({_MODE_LABEL_RU.get(quality_mode, "правка")}). '
                    f'У тебя: <b>{user_total} кр</b>',
                    reply_markup=_BUY_KB,
                )
                return
            ok = await deduct_credits(session, user, cost)
            if not ok:
                await message.answer('💳 Недостаточно кредитов.', reply_markup=_BUY_KB)
                return
        remaining = user.credits
        user_db_id = user.id
        user_gender = user.gender  # для мужского режима промпта
        paid = await is_paid_user(session, user_db_id)
        await session.commit()

    # Мужской режим: мужской оверрайд промпта (для «тяжёлых» пресетов) либо
    # оригинал + усиленный суффикс. Убирает косички/серьги/язык/жен.одежду.
    # 24.05: НЕ применяем для свободных правок текстом (preset_key=None) —
    # мужчина уже на входном фото, дополнительный английский male-suffix
    # ТОЛЬКО разбавляет короткую инструкцию юзера («измени надпись» теряется
    # в простыне про NO makeup/NO earrings) и модель не делает правку.
    # Базовый промпт ДО мутаций (male_adapt/native_wm) — кэшируем для кнопки
    # «повторить» при провале: _run_edit при повторе сам переприменит мутации.
    retry_prompt = prompt
    if user_gender == 'male' and preset_key is not None:
        prompt = _male_adapt(preset_key, prompt)

    # Нативная вотермарка для не-платящих — только на ПРЕСЕТ-сценах (wow-кадры,
    # которыми делятся). Для свободных правок/refine (preset_key=None) НЕ
    # добавляем: иначе на каждой итерации копится граффити + это правка
    # личного фото юзера, а не витринная сцена. Там остаётся corner-fallback.
    native_wm = (not paid) and (preset_key is not None)
    if native_wm:
        prompt = prompt + _NATIVE_WATERMARK_INSTRUCTION

    status = await message.answer(
        '📥 <i>Загружаю фото…</i>'
    )
    await message.bot.send_chat_action(chat_id=message.chat.id, action='upload_photo')

    # Скачиваем все ракурсы (1–3 фото). Хоть одно — продолжаем; ноль — отказ.
    img_bytes_list: list[bytes] = []
    for fid in face_photos:
        b = await _download_telegram_file(message.bot, fid)
        if b:
            img_bytes_list.append(b)
    if not img_bytes_list:
        await _refund(source_user_id, cost)  # рефанд РЕАЛЬНОЙ списанной суммы, не фикс
        log_event_bg(user_db_id, 'image_edit_failed', model_key=quality_mode,
                     preset=preset_key or '', stage='download')
        await status.edit_text('❌ Не удалось загрузить фото. Кредиты возвращены.')
        return

    # Препроцессинг фото: face-detect crop 3:4 — НУЖЕН для пресетов (сцена
    # строится вокруг лица). НО для свободного текста («измени надпись», «добавь
    # тату на руке») face-crop вырезает контекст → юзер просит изменить то, чего
    # уже нет в кадре. Решение 24.05: для preset_key=None пропускаем prep и шлём
    # оригинал — модель видит весь кадр и меняет только то, что просили.
    if preset_key is not None:
        try:
            from app.services.image_prep import prepare_face_image
            prepared = [prepare_face_image(b) for b in img_bytes_list]
            logger.info('image_prep done',
                       n=len(prepared),
                       size_in_kb=sum(len(b) for b in img_bytes_list) // 1024,
                       size_out_kb=sum(len(b) for b in prepared) // 1024)
            img_bytes_list = prepared
        except Exception as exc:
            logger.warning('image_prep skipped', error=str(exc)[:200])
    else:
        logger.info('image_prep skipped: free-text edit, keeping full frame')

    # Guard: не отправляем в fal неоткрываемые байты (HEIC без плагина, RAW,
    # битый файл). Иначе модель не «видит» вход и генерит чушь по одному
    # промпту, а кредиты уже списаны. Лучше честно отказать и вернуть кредиты.
    from app.services.image_prep import is_decodable_image
    decodable = [b for b in img_bytes_list if is_decodable_image(b)]
    if not decodable:
        await _refund(source_user_id, cost)
        await status.edit_text(
            '⚠️ <b>Не смог прочитать фото.</b>\n\n'
            'Похоже, оно пришло в формате, который я не открываю — так бывает, '
            'когда телефон отправляет снимок <b>файлом</b> (.heic и т.п.).\n\n'
            '👉 Пришли это же фото <b>как обычное фото</b> (не «Файл/Документ») — '
            'и я всё сделаю.\n\n'
            '💳 <i>Кредиты возвращены.</i>',
            parse_mode='HTML',
        )
        return
    img_bytes_list = decodable

    # Backward-compat: оставшийся код может ссылаться на img_bytes (single)
    img_bytes = img_bytes_list[0]

    await status.edit_text(
        '🪄 <b>Подкручиваю реальность…</b>\n\n'
        '<i>Обычно 1–3 минуты. Не закрывай чат — я пришлю результат сюда, как только будет готов.</i>'
    )

    stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat_loop(
        message.bot, message.chat.id, status,
        '🪄 <b>Подкручиваю реальность…</b>',
        stop,
    ))

    try:
        # Image-задачи только через fal.ai. Внутри fal — цепочка моделей
        # (nano-banana-2 → flux-kontext). OpenRouter для image не используется,
        # он только для текста (chat).
        result = None
        if quality_mode in ('quality', 'precise') and fal_image_service.is_enabled():
            # Multi-selfie: передаём все ракурсы — модель видит лицо со всех сторон.
            # Для свободного текста (preset_key=None) НЕ форсим aspect_ratio —
            # модель сохранит размеры входа (если просили «измени надпись» — оно
            # же должно вернуться той же формы, не 1:1). Для пресетов — 3:4.
            kwargs = {}
            if preset_key is None:
                kwargs['image_size'] = None  # пропустит aspect_ratio в payload
            result = await fal_image_service.edit_image(img_bytes_list, prompt, **kwargs)
            if result:
                logger.info('image_edit served by fal.ai (quality chain)',
                           user_db_id=user_db_id, mode=quality_mode,
                           n_inputs=len(img_bytes_list))
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            hb_task.cancel()

    if not result:
        await _refund(source_user_id, cost)
        # Подарок дня не должен сгорать при сбое генерации: откатываем claim,
        # чтобы юзер мог забрать бесплатную сцену заново (или через free-retry ниже).
        if mystery_free:
            try:
                from app.services.mystery_preset import release as _mystery_release
                await _mystery_release(source_user_id)
            except Exception as exc:
                logger.warning('mystery release failed', error=str(exc)[:150])
        # P1: логируем провал генерации. Без этого retention-аналитика слепа —
        # не отличить «доволен, ушёл» от «попробовал, упало, бросил».
        log_event_bg(user_db_id, 'image_edit_failed', model_key=quality_mode,
                     preset=preset_key or '', stage='generate')
        # P2: кнопка повтора вместо текстового тупика. Единый режим quality —
        # при временном сбое провайдера повтор того же кадра спасает.
        retry_rows: list[list[InlineKeyboardButton]] = []
        try:
            import uuid as _uuid, json as _json
            from redis.asyncio import Redis
            from app.config import settings as _settings
            _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
            _rk = _uuid.uuid4().hex[:12]
            await _redis.set(
                f'retry:{_rk}',
                _json.dumps({'face_photos': face_photos, 'prompt': retry_prompt,
                             'preset_key': preset_key, 'mode': quality_mode,
                             'mystery_free': mystery_free}),
                ex=3600,
            )
            retry_rows.append([InlineKeyboardButton.model_construct(
                text='↻ Попробовать ещё раз', callback_data=f'retry_edit:{_rk}',
                icon_custom_emoji_id=PE_SPARKLES, style='success',
            )])
        except Exception as exc:
            logger.warning('retry cache failed', error=str(exc)[:160])
        retry_rows.append([InlineKeyboardButton.model_construct(
            text='Выбрать другую сцену', callback_data='gallery_open',
            icon_custom_emoji_id=PE_SPARKLES,
            style=('primary' if retry_rows else 'success'),
        )])
        await status.edit_text(
            '❌ <b>Не получилось отредактировать.</b>\n\n'
            'Возможные причины:\n'
            '• Запрос отклонила политика безопасности модели\n'
            '• Слишком сложная задача — попробуй упростить или прислать более чёткое фото\n'
            '• Временный сбой провайдера'
            '\n\n💳 <b>Кредиты возвращены на баланс.</b>',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=retry_rows),
        )
        return

    # ── Precise face-swap ОТКЛЮЧЁН (22.05.2026, по решению владельца) ──────
    # fal-ai/face-swap принудительно пересохранял весь кадр в JPEG 4:2:0, что
    # давало «лесенку» на резких цветных краях (мрамор/красный фон). Эндпоинт
    # игнорирует output_format=png, поэтому убрать повреждение нельзя. Решили
    # отдавать чистый 2K PNG от nano-banana-2 без второго прохода. Флаг ниже
    # держит блок выключенным; identity на 2K от nano-banana и так хороший.
    PRECISE_FACESWAP_ENABLED = False
    precise_swap_done = False
    if PRECISE_FACESWAP_ENABLED and quality_mode == 'precise' and result and fal_image_service.is_enabled():
        await status.edit_text(
            '✅ <b>Стиль применён · переношу точное лицо…</b>\n\n'
            '<i>Финальный шаг, обычно ~1 минута. Лицо станет в точности как на оригинале.</i>'
        )
        stop2 = asyncio.Event()
        hb_task2 = asyncio.create_task(_heartbeat_loop(
            message.bot, message.chat.id, status,
            '🛡 <b>Переношу точное лицо…</b>',
            stop2,
        ))
        try:
            swap_result = await fal_image_service.face_swap(
                base_image_bytes=result,
                face_image_bytes=img_bytes,
                extra_prompt='Place the face from the second image onto the subject in the first image. Preserve the styling, pose and scene of the first image exactly.',
            )
        except Exception as exc:
            logger.warning('precise auto face-swap raised',
                          user_db_id=user_db_id, error=str(exc)[:200])
            swap_result = None
        finally:
            stop2.set()
            try:
                await asyncio.wait_for(hb_task2, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                hb_task2.cancel()
        if swap_result:
            result = swap_result
            precise_swap_done = True
            logger.info('precise auto-swap applied', user_db_id=user_db_id)
        else:
            # Edit сработал, но swap упал — отдаём edit-only результат и возвращаем
            # разницу precise→quality (200 - 100 = 100 кр)
            refund_diff = cost - await effective_cost_for('quality')
            if refund_diff > 0:
                await _refund(source_user_id, refund_diff)
                cost -= refund_diff
                remaining += refund_diff
            logger.warning('precise swap failed, returned edit-only result',
                          user_db_id=user_db_id, refunded=refund_diff)

    # log_event AWAIT (не _bg) — потому что _gamify_after_edit ниже сразу
    # читает count_user_events. С _bg INSERT мог не успеть закоммититься →
    # milestone срабатывал на +1 событие позже («3-е фото» сюрприз на 4-м).
    from app.database.repo.events import log_event
    await log_event(user_db_id, 'image_edited', credits_spent=cost,
                    quality_mode=quality_mode,
                    precise_swap_done=precise_swap_done,
                    preset_key=preset_key,
                    mystery_free=mystery_free)

    # Геймификация после успешной генерации (24.05): обновить streak +
    # проверить milestone «N фото». Тихо — ошибки не валят основной flow.
    streak_bonus_msg, milestone_msg = await _gamify_after_edit(source_user_id)

    # Static corner-вотермарка — fallback. Для пресет-сцен не-платящих уже
    # вшита нативная @NeyraChatBot в сам кадр (native_wm) → corner не нужен,
    # не дублируем. Для свободных правок (native_wm=False) ставим corner.
    if not paid and not native_wm:
        result = watermark.add_watermark(result)

    await status.delete()
    bio = BytesIO(result)
    bio.name = 'edited.png'
    photo_file = BufferedInputFile(bio.read(), filename='edited.png')
    if quality_mode == 'precise' and precise_swap_done:
        mode_badge = '🛡 Точное лицо'
    else:
        mode_badge = '💎 Качество'

    # Запоминаем оригинал + результат в Redis на 1 час — для возможного refine_face
    refine_key = None
    if file_id:
        try:
            import uuid as _uuid
            from redis.asyncio import Redis
            from app.config import settings as _settings
            _redis = Redis.from_url(_settings.REDIS_URL, decode_responses=True)
            refine_key = _uuid.uuid4().hex[:12]
            # Сохраняем оригинал; result_file_id запишем после ответа
            import json as _json
            await _redis.set(
                f'refine:{refine_key}',
                _json.dumps({'orig': file_id, 'user_id': source_user_id}),
                ex=3600,
            )
        except Exception as exc:
            logger.warning('refine cache failed', error=str(exc)[:200])
            refine_key = None

    swap_cost = await effective_face_swap_cost()
    style_cost = await effective_cost_for('quality')
    # «Мало кредитов» = не хватит даже на самое дешёвое осмысленное действие
    # (новый стиль / новый swap). Тогда hero становится upsell кредитов.
    low_balance = remaining < min(style_cost, swap_cost)
    # «Лицо точнее» ОТКЛЮЧЕНО: кнопка запускала тот же fal-ai/face-swap (JPEG 4:2:0
    # → лесенка). Раз face-swap-проход убран, и кнопку не показываем.
    show_refine_btn = False
    # «Применить другой стиль»: нужен refine_key (оригинал в кеше) И хватает кредитов
    can_restyle = bool(refine_key and remaining >= style_cost)
    can_faceswap = remaining >= swap_cost

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    # ─────────────────────────────────────────────────────────────────────
    # Экран результата заточен под ОДНУ цель — следующая генерация (главная
    # боль: после 1-го кадра почти никто не делает 2-й). Сверху — самые
    # низкофрикционные способы сделать ещё кадр; лишнее убрано вниз и тихо.
    # ─────────────────────────────────────────────────────────────────────

    # 1) ГЕРОЙ — Подарок дня БЕСПЛАТНО. Снимает сразу два барьера (цена + выбор)
    #    → сильнейший драйвер 2-й генерации. Применяется к тому же селфи одним
    #    тапом без списания (claim + mystery_free в cb_freescene).
    free_gift_key = None
    if refine_key:
        try:
            from app.services.mystery_preset import get_today_mystery, is_used_today
            _m = await get_today_mystery()
            if (_m and _m['preset_key'] in EDIT_PRESETS
                    and _m['preset_key'] != preset_key
                    and not (user_gender == 'male' and _m['preset_key'] in _FEMALE_ONLY_PRESETS)
                    and not await is_used_today(source_user_id)):
                free_gift_key = _m['preset_key']
        except Exception as exc:
            logger.warning('result free-gift check failed', error=str(exc)[:150])
    # Цвета (style) расставлены по UX-иерархии: success(зелёный)=то, что хотим
    # тапнуть больше всего (бесплатный кадр / снять знак=выручка), primary(синий)=
    # ещё генерации, default=вторичное (друг/меню). Эмодзи — премиум (PE_*).
    if free_gift_key:
        # Короткий фикс-лейбл (имя сцены НЕ влезает в кнопку → Telegram режет).
        keyboard_rows.append([InlineKeyboardButton.model_construct(
            text='Сегодня бесплатно',
            callback_data=f'freescene:{refine_key}',
            icon_custom_emoji_id=PE_GIFT, style='success',
        )])

    # 2) Квик-сцены: 1-2 конкретные сцены в один тап к ТОМУ ЖЕ селфи. БЕЗ цены в
    #    подписи (она в шапке — не нагнетаем расход). Текст = имя сцены (со своим
    #    эмодзи) без префикса. Синий (primary) — это «ещё генерация».
    if refine_key and not low_balance:
        _suggest = [k for k in await _get_new_preset_keys(limit=12)
                    if k != preset_key and k != free_gift_key
                    and not (user_gender == 'male' and k in _FEMALE_ONLY_PRESETS)
                    # prompt=None (change_clothes/change_bg) нельзя запускать одним
                    # тапом — им нужно текстовое уточнение. Не предлагаем в quickscene.
                    and EDIT_PRESETS.get(k, (None, None))[1] is not None][:2]
        for _k in _suggest:
            keyboard_rows.append([InlineKeyboardButton.model_construct(
                text=_label_for(_k, user_gender == "male"),
                callback_data=f'quickscene:{refine_key}:{_k}',
                style='primary',
            )])

    # 3) Кредиты кончились → бурн-пак (пик готовности платить сразу после WOW).
    #    Зелёный герой (это и нужный экшен, и выручка). Подписка рядом — синяя.
    if low_balance:
        keyboard_rows.append([InlineKeyboardButton.model_construct(
            text='1 500 кр за 99₽',
            callback_data='buy_pack:burn_1500_7d', icon_custom_emoji_id=PE_FIRE,
            style='success',
        )])
        if not paid:
            keyboard_rows.append([InlineKeyboardButton.model_construct(
                text='Без знака · подписка 149₽',
                callback_data='subscription', icon_custom_emoji_id=PE_AU_GOLD,
                style='primary',
            )])

    # 4) Один вход в каталог — заменяет ДВА прежних дубля «другой стиль» (оба вели
    #    в edit_again — был баг дублирования из-за проверки только row[0]).
    # refine_key в callback → каталог применит выбранный пресет к ТОМУ ЖЕ селфи
    # (без повторной загрузки). Без него (нет кэша) — обычный каталог с нуля.
    keyboard_rows.append([InlineKeyboardButton.model_construct(
        text='Все сцены каталога',
        callback_data=(f'gallery_open:{refine_key}' if refine_key else 'gallery_open'),
        icon_custom_emoji_id=PE_FRAME, style='primary',
    )])

    # 5) Контекстная монетизация (не-low-balance): не платил → снять знак.
    #    Зелёный (success) — это прямая выручка, делаем заметной.
    if not low_balance and not paid:
        keyboard_rows.append([InlineKeyboardButton.model_construct(
            text='Убрать водяной знак · 149₽',
            callback_data='subscription', icon_custom_emoji_id=PE_UNLOCK,
            style='success',
        )])

    # 6) Вторичное в ОДИН ряд: позвать друга (реф-бонус) + неприметный выход.
    #    «Меню» больше НЕ красная-жирная — не должна конкурировать с продолжением.
    #    Face-swap убран отсюда (доступен из главного меню как отд. инструмент).
    secondary_row: list[InlineKeyboardButton] = []
    try:
        from urllib.parse import quote
        from app.config import settings as _settings
        from app.constants import REFERRAL_BONUS
        bot_username = _settings.BOT_USERNAME or 'NeyraChatBot'
        ref_link = f'https://t.me/{bot_username}?start=ref_{source_user_id}'
        share_text = ('Заценил Нейру — кидаешь селфи, получаешь себя в любой сцене. '
                      'Балет у статуи, Bentley, Париж в неоне. Лицо в точности твоё.')
        share_url = (f'https://t.me/share/url?url={quote(ref_link, safe="")}'
                     f'&text={quote(share_text, safe="")}')
        secondary_row.append(InlineKeyboardButton.model_construct(
            text=f'Друг +{REFERRAL_BONUS}',  # без 🤝 в тексте — он уже в иконке (был дубль)
            url=share_url, icon_custom_emoji_id=PE_HANDSHAKE,
        ))
    except Exception as exc:
        logger.warning('share button build failed', error=str(exc)[:200])
    secondary_row.append(InlineKeyboardButton.model_construct(
        text='Меню', callback_data='main_menu', icon_custom_emoji_id=PE_HOME,
    ))
    keyboard_rows.append(secondary_row)

    # Caption: для пресета — его русский label. Для свободного текстового
    # запроса НЕ дублируем промпт (24.05: юзер просил убрать — он и так видит
    # свой текст в своём сообщении выше).
    if preset_key and preset_key in EDIT_PRESETS:
        scene_line = f'<i>{EDIT_PRESETS[preset_key][0]}</i>'
    else:
        scene_line = ''

    caption_parts = [
        f'🪄 <b>Готово</b> · <i>{mode_badge} · {cost} кр</i>',
    ]
    if scene_line:
        caption_parts.append(scene_line)
    caption_parts.append(f'\n💰 Баланс: <b>{remaining:,} кр</b>')
    if precise_swap_done:
        caption_parts.append('<i>🛡 Лицо перенесено в точности — авто-swap уже применён.</i>')
    elif show_refine_btn:
        caption_parts.append('<i>💡 Если лицо не идеально — жми «🔄 Сделать лицо точнее».</i>')
    if low_balance:
        # Контекстный upsell: юзер только что увидел WOW, баланс почти кончился —
        # самое время предложить именно burn-пак 99₽ (быстрый, низкий риск).
        caption_parts.append(
            '<i>🔥 Кредиты почти кончились — докинь <b>99₽</b> и получи '
            '<b>1 500 кр</b> (~15 сцен) на 7 дней.</i>'
        )
        if not paid:
            # Не упускаем причину платить даже когда фокус на докупке кредитов:
            # подписка снимает знак + даёт кредиты ежедневно (видимая ценность).
            caption_parts.append(
                '<i>🔓 А подписка от <b>149₽</b> убирает водяной знак с фото '
                'и даёт кредиты каждый день.</i>'
            )
    elif not paid:
        # Делаем ЦЕННОСТЬ оплаты явной: называем, что на фото знак, и что
        # именно даёт оплата (чистое фото + HD + ежедневный бонус). Знак сам
        # остаётся деликатным (виральность) — продаёт его снятие копия, не агрессия.
        caption_parts.append(
            '<i>🔓 Сейчас в кадр вплетён знак <b>@NeyraChatBot</b>. Подписка '
            'от <b>149₽/мес</b> = фото <b>без знака</b>, HD-качество и '
            '<b>+30 кр</b> каждый день.</i>'
        )

    sent_msg = await message.answer_photo(
        photo=photo_file,
        caption='\n'.join(caption_parts),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )

    # HD-оригинал документом: Telegram пережимает «фото» в JPEG (артефакты на
    # краях лица/пальцев), а файлы не трогает. Даём полное качество одним файлом.
    try:
        hd = BufferedInputFile(result, filename='neyra_hd.png')
        await message.answer_document(
            document=hd,
            caption='📎 <b>HD без сжатия</b> — открой файл для полного качества '
                    '(в превью Telegram сжимает фото).',
            parse_mode='HTML',
        )
    except Exception as exc:
        logger.warning('HD document send failed', error=str(exc)[:200])

    # 24.05: сохраняем file_id результата в FSM-state на 10 мин — даёт юзеру
    # возможность «доработать текстом» сразу после результата («сделай надпись
    # ярче», «убери чашку»). Перехват — в chat.handle_message, если в state
    # лежит свежий last_result_file_id, текст уходит в _run_edit с этим фото.
    if sent_msg and sent_msg.photo:
        import time as _time
        # Контекст последней правки — для refine. Пресет → русский label;
        # свободный текст → сам текст юзера (cap 200). Один шаг назад (без
        # цепи) — иначе модель повторно применяет уже сделанное.
        if preset_key and preset_key in EDIT_PRESETS:
            ctx_label = EDIT_PRESETS[preset_key][0]
        else:
            ctx_label = (prompt or '').strip()[:200]
        try:
            await state.update_data(
                last_result_file_id=sent_msg.photo[-1].file_id,
                last_result_at=_time.time(),
                last_prompt_label=ctx_label,
            )
        except Exception as exc:
            logger.warning('save last_result state failed', error=str(exc)[:200])

    # Дописываем file_id результата в Redis (для face_swap)
    if refine_key and sent_msg and sent_msg.photo:
        try:
            await _redis.set(
                f'refine:{refine_key}',
                _json.dumps({
                    'orig': file_id,
                    'result': sent_msg.photo[-1].file_id,
                    'user_id': source_user_id,
                }),
                ex=3600,
            )
        except Exception as exc:
            logger.warning('refine cache update failed', error=str(exc)[:200])
    # Закрываем Redis-соединение в любом случае — раньше утекало по pool на
    # каждой генерации (24.05 фикс). refine_key=None если первый set провалился,
    # в этом случае _redis вообще не создан — проверяем через locals().
    if '_redis' in locals():
        try:
            await _redis.aclose()
        except Exception:
            pass

    # Геймификация (24.05): после доставки результата шлём отдельным
    # сообщением празднование за streak/milestone — НЕ смешиваем с caption
    # фото (там и так upsell + 4 кнопки, message overload).
    if streak_bonus_msg:
        try:
            await message.answer(streak_bonus_msg, parse_mode='HTML')
        except Exception:
            pass
    if milestone_msg:
        try:
            await message.answer(milestone_msg, parse_mode='HTML')
        except Exception:
            pass


# Milestones по числу сгенерированных фото. Шкала «лестницей» — чем дальше
# юзер, тем щедрее, потому что: (а) heavy-users = главный LTV, нельзя терять;
# (б) к 30+ они потратили на fal ≥$10 — мы окупились многократно; (в) discrete
# tiers ощущаются как «достижение», работают сильнее линейного. Текст-хвост в
# _gamify_after_edit динамически перечисляет следующие ступени.
_PHOTO_MILESTONES: dict[int, int] = {
    3:   50,    # признание новичка
    7:   150,   # регулярный
    15:  400,   # фанат
    30:  800,   # месяц активного использования
    50:  1500,  # преданный (≈$40 потрачено на fal)
    100: 3000,  # легенда
}


async def _gamify_after_edit(user_tg_id: int) -> tuple[str | None, str | None]:
    """После успешной image_edited: обновить streak + проверить photos-milestone.
    Возвращает (streak_msg, milestone_msg) — оба строки или None.

    Безопасно (try/except) — если что-то падает, основной flow не страдает,
    юзер просто не видит празднование (не критично).
    """
    from app.database.repo.credits import update_streak, add_credits
    from app.database.repo.events import count_user_events
    streak_msg: str | None = None
    milestone_msg: str | None = None
    try:
        async with AsyncSessionFactory() as session:
            user = await get_user(session, user_tg_id)
            if not user:
                return None, None
            days, sbonus = await update_streak(session, user)
            if sbonus > 0:
                streak_msg = premium_text(
                    f'🔥 <b>{days} дней подряд!</b> Держи <b>+{sbonus} кр</b> '
                    f'в подарок за серию.'
                )

            # photos_total через bot_events — без миграций. image_edited уже
            # записан выше, count актуальный.
            total = await count_user_events(session, user.id, 'image_edited')
            mbonus = _PHOTO_MILESTONES.get(total)
            if mbonus:
                await add_credits(session, user, mbonus)
                await session.commit()
                # Хвост — контекстный: если есть следующие milestones, перечис-
                # ляем их с реальными бонусами. На последнем (15) — благодарим.
                next_ms = [m for m in sorted(_PHOTO_MILESTONES) if m > total]
                if next_ms:
                    tail = (
                        'Дальше — ' +
                        ' · '.join(f'+{_PHOTO_MILESTONES[m]} кр на {m}-м'
                                   for m in next_ms) + '.'
                    )
                else:
                    tail = '🏆 Это последний milestone-бонус. Спасибо что ты с Нейрой!'
                milestone_msg = premium_text(
                    f'🎉 <b>Это твоё {total}-е фото в Нейре!</b>\n\n'
                    f'Держи <b>+{mbonus} кр</b> в подарок. {tail}'
                )
            else:
                await session.commit()
                # «Прогресс-бар»: подсказка до следующего milestone, чтобы
                # юзер видел путь, а не просто получил подарок «из ниоткуда».
                next_m = next((m for m in sorted(_PHOTO_MILESTONES) if m > total), None)
                if next_m and total >= 1:
                    left = next_m - total
                    plural = 'фото' if left % 10 == 1 and left % 100 != 11 else (
                        'фото' if 2 <= left % 10 <= 4 and not (12 <= left % 100 <= 14) else 'фото'
                    )
                    bonus_amt = _PHOTO_MILESTONES[next_m]
                    milestone_msg = (
                        f'✨ <i>Прогресс: {total}/{next_m} фото → '
                        f'+{bonus_amt} кр на {next_m}-м</i>'
                    )
    except Exception as exc:
        logger.warning('gamify after edit failed', user_tg=user_tg_id, error=str(exc)[:200])
    return streak_msg, milestone_msg


async def _run_face_swap(message: Message, state: FSMContext, extra_prompt: str, source_user_id: int) -> None:
    data = await state.get_data()
    base_file_id = data.get('base_file_id')
    face_file_id = data.get('face_file_id')
    photo_template_id = data.get('photo_template_id')  # сохраняем перед clear()
    await state.clear()

    if not base_file_id or not face_file_id:
        await message.answer('❌ Одного из фото нет в сессии. Начни заново через меню.')
        return

    # Guard: face-swap = fal операция, проверяем баланс ДО списания
    from app.services import fal_balance
    if not await fal_balance.is_fal_allowed():
        await message.answer(
            '🛠 <b>Face-swap временно недоступен</b>\n\n'
            'Обновляем серверы. Скоро вернёмся.\n\n'
            '<i>Кредиты не списались.</i>',
            parse_mode='HTML',
        )
        return

    # Списание
    async with AsyncSessionFactory() as session:
        user = await get_user(session, source_user_id)
        if not user:
            await message.answer('Нажми /start.')
            return
        eff_swap = await effective_face_swap_cost()
        from app.database.repo.credits import total_balance
        user_total = total_balance(user)
        if user_total < eff_swap:
            await message.answer(
                f'🎭 Нужно <b>{eff_swap} кр</b>. У тебя: <b>{user_total} кр</b>',
                reply_markup=_BUY_KB,
            )
            return
        ok = await deduct_credits(session, user, eff_swap)
        remaining = user.credits
        user_db_id = user.id
        paid = await is_paid_user(session, user_db_id)
        await session.commit()
        if not ok:
            await message.answer('💳 Недостаточно кредитов.', reply_markup=_BUY_KB)
            return

    status = await message.answer('📥 <i>Загружаю фото…</i>')
    await message.bot.send_chat_action(chat_id=message.chat.id, action='upload_photo')

    base_bytes = await _download_telegram_file(message.bot, base_file_id)
    face_bytes = await _download_telegram_file(message.bot, face_file_id)

    if not base_bytes or not face_bytes:
        await _refund(source_user_id, eff_swap)
        await status.edit_text('❌ Не удалось загрузить одно из фото. Кредиты возвращены.')
        return

    await status.edit_text(
        '🎭 <b>Переношу лицо…</b>\n\n'
        '<i>Обычно 1–3 минуты. Не закрывай чат — я пришлю результат сюда, как только будет готов.</i>'
    )

    stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat_loop(
        message.bot, message.chat.id, status,
        '🎭 <b>Переношу лицо…</b>',
        stop,
    ))

    try:
        # face_swap → ТОЛЬКО fal.ai (Gemini 3.1) если ключ есть, без фолбэка.
        # Так увидим реальную работу Gemini в логах.
        result = None
        if fal_image_service.is_enabled():
            result = await fal_image_service.face_swap(base_bytes, face_bytes, extra_prompt=extra_prompt)
            if result:
                logger.info('face_swap served by fal.ai (nano-banana-2)', user_db_id=user_db_id)
            else:
                logger.warning('fal face_swap returned None — no fallback',
                              user_db_id=user_db_id)
        else:
            result = await image_edit_service.face_swap(base_bytes, face_bytes, extra_prompt=extra_prompt)
            if result:
                logger.info('face_swap served by openrouter (gpt-5-image)', user_db_id=user_db_id)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            hb_task.cancel()

    if not result:
        await _refund(source_user_id, eff_swap)
        await status.edit_text(
            '❌ <b>Не получилось перенести лицо.</b>\n\n'
            'Возможные причины:\n'
            '• Лицо плохо различимо на одном из фото\n'
            '• Политика безопасности отклонила запрос\n'
            '• Временный сбой провайдера\n\n'
            '💡 <b>Совет:</b> используй фото с чётким, фронтальным лицом и хорошим светом.\n\n'
            '💳 <b>Кредиты возвращены.</b>'
        )
        return

    log_event_bg(user_db_id, 'face_swapped', credits_spent=eff_swap)

    # Если face_swap был запущен из шаблона — инкрементируем conversion_count
    if photo_template_id:
        try:
            from sqlalchemy import select as _select
            from app.database.models import PhotoTemplate
            async with AsyncSessionFactory() as session:
                tpl = await session.scalar(_select(PhotoTemplate).where(PhotoTemplate.id == photo_template_id))
                if tpl:
                    tpl.conversion_count += 1
                    await session.commit()
        except Exception as exc:
            logger.warning('tpl conversion bump failed', error=str(exc)[:200])

    if not paid:
        result = watermark.add_watermark(result)

    await status.delete()
    bio = BytesIO(result)
    bio.name = 'face_swap.png'
    photo_file = BufferedInputFile(bio.read(), filename='face_swap.png')
    await message.answer_photo(
        photo=photo_file,
        caption=(
            f'🎭 <b>Готово — лицо перенесено</b>\n\n'
            f'💰 Баланс: <b>{remaining} кр</b>'
            + ('\n\n<i>💡 Купи любой пакет — следующие фото без водяного знака.</i>' if not paid else '')
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton.model_construct(text=f'Подставить лицо в другое фото · {await effective_face_swap_cost()} кр', callback_data='face_swap_start', icon_custom_emoji_id=PE_ROBOT, style='primary')],
            [InlineKeyboardButton.model_construct(text='Изменить фото', callback_data='edit_start', icon_custom_emoji_id=PE_SPARKLES)],
            [InlineKeyboardButton.model_construct(text='В главное меню', callback_data='main_menu', icon_custom_emoji_id=PE_BACK, style='danger')],
        ]),
    )
    # HD-оригинал документом (без JPEG-пережатия Telegram).
    try:
        await message.answer_document(
            document=BufferedInputFile(result, filename='neyra_hd.png'),
            caption='📎 <b>HD без сжатия</b> — открой файл для полного качества.',
            parse_mode='HTML',
        )
    except Exception as exc:
        logger.warning('HD document (face_swap) send failed', error=str(exc)[:200])


async def _refund(tg_id: int, amount: int) -> None:
    """Возврат кредитов при ошибке. Никогда не падает."""
    try:
        async with AsyncSessionFactory() as session:
            user = await get_user(session, tg_id)
            if user:
                await add_credits(session, user, amount)
                await session.commit()
    except Exception as exc:
        logger.error('Refund failed', tg_id=tg_id, amount=amount, error=str(exc)[:200])

"""Mystery Preset of the Day — главный retention-механизм.

Каждый день в боте появляется ОДИН ротируемый пресет, который любой юзер
может применить БЕСПЛАТНО (один раз в сутки). Это даёт чёткий ежедневный
повод вернуться: «вернись посмотреть что сегодня бесплатно».

Архитектура без миграций (24.05):
- bot_config.mystery_today_data — JSON {date, preset_key, label}
- Redis ключ mystery:used:{date_iso}:{tg_id} — TTL до конца суток МСК
- Lazy rotation: при чтении get_today_mystery() проверяем дату, если устарела
  → выбираем новый пресет (рандом из MYSTERY_ELIGIBLE, не вчерашний) и
  сохраняем. Никакого cron — пусть первая встреча в сутках всё сделает.

Защита:
- Только пресеты с обоими (M+F) превью попадают в пул (юзер любого пола
  получит превью своего пола)
- Не дважды подряд один и тот же
- Бесплатное использование = НЕ списываем кредиты, но event пишется с
  пометкой mystery=True для дашборда (можно посчитать LTV-эффект)
"""

from __future__ import annotations

import json
import random

import structlog

from app.database.base import AsyncSessionFactory
from app.database.repo.bot_config import get_config_value, set_config_value
from app.utils import today_msk

logger = structlog.get_logger(__name__)

# Пресеты-кандидаты для Mystery. Все «вкусные» пресеты с превью M+F.
# Не утилитарные (change_bg / remove_bg) — это скучный fast и не радует.
# Не female-only — там мужчинам показываем не то, что в карточке.
MYSTERY_ELIGIBLE: list[str] = [
    "cinema",
    "cyberpunk",
    "golden_hour",
    "retro_90s",
    "rooftop_wine",
    "tennis_70s",
    "warrior",
    "rainy_taxi",
    "vintage_rain",
    "berlin_metro",
    "giant_city",
    "fisheye_paris",
    "fisheye_porsche",
    "y2k_fridge",
    "macro_watermelon",
    "night_racing",
    "bold_red_studio",
    "magazine_vibe",
    "cyber_neon",
    "red_lust",
    "underwater_red",
    "goldfish_dream",
    # Новая пачка 24.05 (из uploads) — дают свежесть ротации + бесплатную
    # витрину новым сценам. Все custom-ключи в EDIT_PRESETS после reload.
    "dice_throw",
    "doberman_boss",
    "gold_coffee",
    "diner_poster",
    "claw_machine",
    "dj_booth",
    "supermarket_sweep",
    "wolfcut_portrait",
    "graffiti_elevator",
    # Вторая пачка 24.05
    "diner_cereal",
    "store_cooler",
    "street_lowangle",
    "chrome_goggles",
    "tokyo_vending",
    "bubblegum_car",
    "courtside_glam",
    "white_void",
    "red_punk",
    # ⚠️ cat_beanie УБРАН (02.06): female-only + нет мужского превью → мужчинам
    # выпадала женская сцена в подарок (нарушает «Не female-only» из доки выше).
]

_CONFIG_KEY = "mystery_today_data"


def _redis():
    from redis.asyncio import Redis

    from app.config import settings

    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def get_today_mystery() -> dict | None:
    """Возвращает {date, preset_key, label} для сегодняшнего Mystery или None
    если что-то пошло не так. Ленивая ротация: если в БД дата != сегодня, или
    нет записи — выбираем новый пресет, сохраняем.

    Подсказка: label берётся живым из EDIT_PRESETS (поддержит переименования)."""
    from app.handlers.image_edit import EDIT_PRESETS

    # Подарок дня доступен ТОЛЬКО по вторникам и четвергам. В остальные дни
    # возвращаем None — это разом скрывает кнопку в меню, не даёт уйти пушу и
    # блокирует тап «Сегодня бесплатно» (все точки входа гейтятся этим методом).
    if today_msk().weekday() not in (1, 3):  # Mon=0 … Tue=1, Thu=3
        return None

    today = today_msk().isoformat()
    async with AsyncSessionFactory() as session:
        raw = await get_config_value(session, _CONFIG_KEY)

    stored: dict = {}
    if raw:
        try:
            stored = json.loads(raw)
        except Exception:
            stored = {}

    if stored.get("date") == today and stored.get("preset_key") in EDIT_PRESETS:
        # Свежий — отдаём (но label берём актуальный из EDIT_PRESETS, не из кэша)
        k = stored["preset_key"]
        return {"date": today, "preset_key": k, "label": EDIT_PRESETS[k][0]}

    # Нужна ротация. Берём ТОЛЬКО пресеты, у которых есть превью (position 0) —
    # гарантия, что пуш «Подарок дня» уйдёт с картинкой этого же дня, а не текстом.
    from app.database.repo.preset_previews import get_previewed_keys

    async with AsyncSessionFactory() as session:
        previewed = await get_previewed_keys(session, position=0)

    yesterday_key = stored.get("preset_key")
    # Early-access сцены НЕ выдаём в «Подарке дня» — иначе free получит закрытый
    # пресет бесплатно мимо paywall. Прогреваем кэш раннего доступа.
    from app.handlers.image_edit import _is_early_access, refresh_fresh_presets

    try:
        await refresh_fresh_presets()
    except Exception:
        pass
    pool = [
        k
        for k in MYSTERY_ELIGIBLE
        if k in EDIT_PRESETS and k in previewed and k != yesterday_key and not _is_early_access(k)
    ]
    if not pool:
        # fallback: без исключения вчерашнего (но всё ещё только с превью и не early-access)
        pool = [
            k
            for k in MYSTERY_ELIGIBLE
            if k in EDIT_PRESETS and k in previewed and not _is_early_access(k)
        ]
    if not pool:
        return None
    new_key = random.choice(pool)
    new_data = {"date": today, "preset_key": new_key, "label": EDIT_PRESETS[new_key][0]}
    try:
        async with AsyncSessionFactory() as session:
            await set_config_value(session, _CONFIG_KEY, json.dumps(new_data))
            await session.commit()
        logger.info("mystery rotated", preset_key=new_key, date=today, prev=yesterday_key)
    except Exception as exc:
        logger.warning("mystery save failed", error=str(exc)[:200])
    return new_data


async def is_used_today(user_tg_id: int) -> bool:
    """Уже забрал ли юзер сегодняшний Mystery."""
    today = today_msk().isoformat()
    key = f"mystery:used:{today}:{user_tg_id}"
    try:
        r = _redis()
        try:
            return bool(await r.exists(key))
        finally:
            await r.aclose()
    except Exception:
        return False


async def claim(user_tg_id: int) -> bool:
    """Атомарно занимает сегодняшний Mystery для юзера. True = можно использовать;
    False = уже использовал. TTL ставим до полуночи МСК — простой 24ч ок (юзеру
    «следующее доступно завтра» = понятно, без перфекционизма точной полуночи)."""
    today = today_msk().isoformat()
    key = f"mystery:used:{today}:{user_tg_id}"
    try:
        r = _redis()
        try:
            return bool(await r.set(key, "1", nx=True, ex=86400))
        finally:
            await r.aclose()
    except Exception as exc:
        logger.warning("mystery claim redis failed", error=str(exc)[:200])
        return False


async def release(user_tg_id: int) -> None:
    """Откат claim — если бесплатная генерация подарка дня провалилась, слот НЕ
    должен сгорать: возвращаем юзеру право забрать сегодняшний Mystery заново."""
    today = today_msk().isoformat()
    key = f"mystery:used:{today}:{user_tg_id}"
    try:
        r = _redis()
        try:
            await r.delete(key)
        finally:
            await r.aclose()
    except Exception as exc:
        logger.warning("mystery release redis failed", error=str(exc)[:200])

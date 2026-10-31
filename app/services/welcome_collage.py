"""Welcome-коллаж для /start. Три ярких превью склеены в одну картинку
side-by-side и отправляются вместе с приветствием — резко повышает CTR
на новых юзерах (особенно с реф-ссылок и посевов).

Алгоритм:
  1. Берём 3 готовых preview file_id из БД (хардкод-набор).
  2. Скачиваем по бот-токену, склеиваем PIL'ом, кодируем JPEG q92.
  3. Заливаем в админ-чат → берём compressed file_id.
  4. Кешируем в bot_config (`welcome_collage_file_id`) — следующие /start
     шлют по file_id без сборки. fal не дёргается ВООБЩЕ — это коллаж из
     уже сгенерённых превью.

Если набор пресетов сменился (например, добавлен новый яркий) — оператор
удаляет ключ `welcome_collage_file_id` через /admin/texts и при следующем
/start коллаж пересоберётся.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Welcome-коллаж РОТИРУЕТСЯ раз в 3 дня (детерминированно по дате → все юзеры
# в одном окне видят один набор). Выбор: 2 ярких сцены из _POOL_A + 1 мужская
# из _POOL_B (гарантируем сигнал «и для парней»), порядок тасуется. Все ключи —
# с подтверждёнными превью (position 0=жен, 1=муж). marble_punk исключён (жест
# отталкивает новичков). Курируемые HERO-сцены — заметные и разнообразные.
_POOL_A: list[tuple[str, int]] = [
    ('neon_portrait', 0), ('fisheye_paris', 0), ('golden_hour', 0),
    ('rooftop_wine', 0),  ('vogue', 0),         ('red_lust', 0),
    ('underwater_red', 0),('rainy_taxi', 0),    ('tokyo_vending', 0),
    ('cyberpunk', 0),     ('ramen_night', 0),   ('warrior', 0),
    ('fireworks_night', 0),('times_fisheye', 0),
]
_POOL_B: list[tuple[str, int]] = [
    ('sky_ledge', 1),  ('suit_smoke', 1),  ('night_racing', 1),
    ('berlin_metro', 1),('warrior', 1),    ('cyberpunk', 1),
    ('trunk_pov', 1),
]
_ROTATE_DAYS = 3
_BOT_CFG_KEY = 'welcome_collage_file_id'
_PERIOD_CFG_KEY = 'welcome_collage_period'  # текущее 3-дневное окно (инвалидация)


def _current_period() -> int:
    """Номер текущего 3-дневного окна (UTC). Меняется → коллаж пересобирается."""
    from datetime import datetime
    return datetime.now(timezone.utc).date().toordinal() // _ROTATE_DAYS


def _select_presets(period: int) -> list[tuple[str, int]]:
    """Детерминированный набор из 3 (key, position) для окна `period`:
    2 сцены из _POOL_A + 1 мужская из _POOL_B, порядок перемешан."""
    import random
    rng = random.Random(period)
    a = rng.sample(_POOL_A, 2)
    a_keys = {k for k, _ in a}
    # Мужская сцена из B, но НЕ дублируя ключ из A (cyberpunk/warrior есть в обоих).
    b_candidates = [t for t in _POOL_B if t[0] not in a_keys] or _POOL_B
    picks = a + [rng.choice(b_candidates)]
    rng.shuffle(picks)
    return picks


def is_collage_preset(key: str, position: int | None = None) -> bool:
    """Входит ли (key,position) в ТЕКУЩИЙ (по окну) welcome-коллаж. Нужно админ-
    хендлерам, чтобы автоматически инвалидировать кэш при regen/delete-thumb
    одного из этих превью (иначе welcome будет слать старую картинку)."""
    current = _select_presets(_current_period())
    if position is None:
        return any(k == key for k, _ in current)
    return (key, position) in current


async def _download_fid(bot, fid: str) -> bytes:
    f = await bot.get_file(fid)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    return buf.getvalue()


async def _build_collage_bytes(bot, presets: list[tuple[str, int]] | None = None) -> bytes | None:
    from PIL import Image

    from app.database.base import AsyncSessionFactory
    from app.database.repo.preset_previews import get_preview_file_id

    if presets is None:
        presets = _select_presets(_current_period())
    async with AsyncSessionFactory() as session:
        fids: list[str] = []
        for key, pos in presets:
            fid = await get_preview_file_id(session, key, position=pos)
            if fid:
                fids.append(fid)
    if not fids:
        logger.warning('welcome collage: no previews found in DB')
        return None

    imgs = []
    for fid in fids:
        try:
            imgs.append(Image.open(io.BytesIO(await _download_fid(bot, fid))).convert('RGB'))
        except Exception as exc:
            logger.warning('welcome collage: download/decode failed', error=str(exc)[:200])
    if not imgs:
        return None

    h = min(im.height for im in imgs)
    resized = [im.resize((max(1, int(im.width * h / im.height)), h)) for im in imgs]
    gap = 12
    width = sum(im.width for im in resized) + gap * (len(resized) - 1)
    canvas = Image.new('RGB', (width, h), (18, 18, 20))
    x = 0
    for im in resized:
        canvas.paste(im, (x, 0))
        x += im.width + gap
    out = io.BytesIO()
    canvas.save(out, 'JPEG', quality=92, optimize=True)
    return out.getvalue()


async def invalidate_welcome_collage() -> None:
    """Удаляет закэшированный file_id (следующий /start пересоберёт). Зовётся
    автоматически если send_photo упал — значит fid протух."""
    from app.database.base import AsyncSessionFactory
    from app.database.repo.bot_config import set_config_value
    async with AsyncSessionFactory() as session:
        await set_config_value(session, _BOT_CFG_KEY, '')
        await session.commit()


async def get_or_build_welcome_collage_fid(bot) -> str | None:
    """Возвращает file_id коллажа для ТЕКУЩЕГО 3-дневного окна. Если кэш пуст
    ИЛИ окно сменилось — пересобирает (новый набор сцен), заливает в админ-чат,
    сохраняет file_id + номер окна в bot_config. Внутри окна — тот же file_id
    без сборки."""
    from aiogram.types import BufferedInputFile

    from app.config import settings
    from app.database.base import AsyncSessionFactory
    from app.database.repo.bot_config import get_config_value, set_config_value

    period = _current_period()
    async with AsyncSessionFactory() as session:
        cached = await get_config_value(session, _BOT_CFG_KEY)
        cached_period = await get_config_value(session, _PERIOD_CFG_KEY)
    # Валиден только если есть fid И окно совпадает (иначе — ротация).
    if cached and cached_period == str(period):
        return cached

    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    if not admin:
        return cached or None  # нет админ-чата для сборки — отдаём старое, чтоб не падать
    try:
        collage = await _build_collage_bytes(bot, _select_presets(period))
        if not collage:
            return None
        scenes = ', '.join(k for k, _ in _select_presets(period))
        msg = await bot.send_photo(
            admin, BufferedInputFile(collage, 'neyra_welcome.jpg'),
            caption=f'🖼 <i>Welcome-коллаж окна #{period} кеширован — будет вшит '
                    f'во все /start ближайшие {_ROTATE_DAYS} дн.\nСцены: {scenes}</i>',
            parse_mode='HTML',
        )
        fid = msg.photo[-1].file_id
        async with AsyncSessionFactory() as session:
            await set_config_value(session, _BOT_CFG_KEY, fid)
            await set_config_value(session, _PERIOD_CFG_KEY, str(period))
            await session.commit()
        return fid
    except Exception as exc:
        logger.warning('welcome collage build/upload failed', error=str(exc)[:200])
        return None

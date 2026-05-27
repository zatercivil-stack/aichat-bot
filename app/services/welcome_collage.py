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
from typing import Optional

logger = logging.getLogger(__name__)

# Три ярких пресета для приветственного коллажа. Берём СУЩЕСТВУЮЩИЕ превью.
# Порядок = слева направо. Конфиг-ключ можно вынести в bot_config позже.
# 27.05: набор синхронизирован с showcase-коллажем главного меню (HERO-сцены,
# position 0). marble_punk УБРАН отовсюду — жест (фак) отталкивает новичков.
_PRESETS: list[tuple[str, int]] = [
    ('sky_ledge',     1),  # 🏙 На карнизе высоток — МУЖСКОЙ кадр (сигнал «и для парней»)
    ('ramen_night',   0),  # 🍜 Ночь в раменной — тёплая киношная атмосфера (жен)
    ('neon_portrait', 0),  # 🕶 Неоновый портрет — резкий цветной крупный план (жен)
]
_BOT_CFG_KEY = 'welcome_collage_file_id'


def is_collage_preset(key: str, position: int | None = None) -> bool:
    """Используется ли (key,position) в текущем welcome-коллаже. Нужно админ-
    хендлерам, чтобы автоматически инвалидировать кэш при regen/delete-thumb
    одного из этих превью (иначе welcome будет слать старую картинку)."""
    if position is None:
        return any(k == key for k, _ in _PRESETS)
    return (key, position) in _PRESETS


async def _download_fid(bot, fid: str) -> bytes:
    f = await bot.get_file(fid)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    return buf.getvalue()


async def _build_collage_bytes(bot) -> Optional[bytes]:
    from PIL import Image
    from app.database.base import AsyncSessionFactory
    from app.database.repo.preset_previews import get_preview_file_id

    async with AsyncSessionFactory() as session:
        fids: list[str] = []
        for key, pos in _PRESETS:
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


async def get_or_build_welcome_collage_fid(bot) -> Optional[str]:
    """Возвращает file_id готового коллажа. Если в bot_config пусто — строит,
    заливает в админ-чат → получает file_id → сохраняет в bot_config. Все
    последующие /start используют тот же file_id без сборки."""
    from aiogram.types import BufferedInputFile
    from app.config import settings
    from app.database.base import AsyncSessionFactory
    from app.database.repo.bot_config import get_config_value, set_config_value

    async with AsyncSessionFactory() as session:
        cached = await get_config_value(session, _BOT_CFG_KEY)
    if cached:
        return cached

    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    if not admin:
        return None
    try:
        collage = await _build_collage_bytes(bot)
        if not collage:
            return None
        msg = await bot.send_photo(
            admin, BufferedInputFile(collage, 'neyra_welcome.jpg'),
            caption='🖼 <i>Welcome-коллаж кеширован в bot_config — '
                    'будет вшит во все /start.</i>',
            parse_mode='HTML',
        )
        fid = msg.photo[-1].file_id
        async with AsyncSessionFactory() as session:
            await set_config_value(session, _BOT_CFG_KEY, fid)
            await session.commit()
        return fid
    except Exception as exc:
        logger.warning('welcome collage build/upload failed', error=str(exc)[:200])
        return None

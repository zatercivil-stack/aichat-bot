"""Черновик канал-поста с презентацией новинок: 2x3 коллаж лучших новых сцен
+ текст. Шлёт админу в личку на согласование (НЕ в канал). Печатает collage
file_id + число сегодняшних новинок."""
import asyncio
import io
from datetime import datetime, timezone

from PIL import Image
from sqlalchemy import select, func

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import CustomPreset
from app.database.repo.preset_previews import get_preview_file_id
from app.services.welcome_collage import _download_fid

HERO = ['sky_ledge', 'ramen_night', 'neon_portrait',
        'fireworks_night', 'arcade_night', 'times_fisheye']

CAPTION = (
    "🔥 <b>Нейра пополнилась — куча новых сцен</b>\n\n"
    "Кидаешь одно селфи → выбираешь сцену → через минуту готовый кадр, лицо "
    "<b>1-в-1 твоё</b>:\n\n"
    "🏙 На карнизе высоток · 🍜 Ночь в раменной\n"
    "🕶 Неоновый портрет · 🎆 Огни фейерверка\n"
    "🕹 Зал автоматов · 🗽 Тайм-сквер в рыбий глаз\n"
    "📞 Звонок из ужастика · 📺 Аниме 90-х · 🥃 Ночь в баре\n\n"
    "У каждой — свой свет, плёнка и настроение. Не «куда-то похоже», а именно "
    "та атмосфера.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🎁 <b>Подписчикам канала — +300 кр</b>:\n"
    "<tg-spoiler><code>CHAN_SECRET_300</code></tg-spoiler>\n"
    "<i>Жми, копируй в бота → /promo КОД. Активен 14 дней.</i>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    '<a href="https://t.me/conturD">😈 Контур доступа</a>\n'
    "—\n"
    '🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Попробовать новые '
    'сцены → +500 кр новичку</b></a>'
)


async def build_grid(bot, keys, cols=2):
    imgs = []
    async with AsyncSessionFactory() as s:
        for k in keys:
            fid = await get_preview_file_id(s, k, position=0)
            if not fid:
                continue
            try:
                imgs.append(Image.open(io.BytesIO(await _download_fid(bot, fid))).convert('RGB'))
            except Exception:
                pass
    if not imgs:
        return None
    # ячейка: единый размер (по медиане), портрет 3:4
    cw, ch = 600, 800
    cells = []
    for im in imgs:
        # crop-to-fill 3:4
        scale = max(cw / im.width, ch / im.height)
        im2 = im.resize((int(im.width * scale), int(im.height * scale)))
        left = (im2.width - cw) // 2
        top = (im2.height - ch) // 2
        cells.append(im2.crop((left, top, left + cw, top + ch)))
    rows = (len(cells) + cols - 1) // cols
    gap = 10
    W = cols * cw + (cols - 1) * gap
    H = rows * ch + (rows - 1) * gap
    canvas = Image.new('RGB', (W, H), (18, 18, 20))
    for i, c in enumerate(cells):
        r, cl = divmod(i, cols)
        canvas.paste(c, (cl * (cw + gap), r * (ch + gap)))
    out = io.BytesIO()
    canvas.save(out, 'JPEG', quality=90, optimize=True)
    return out.getvalue()


async def main():
    from app.bot import create_bot
    bot = create_bot()
    try:
        # число сегодняшних новинок (enabled, created 25-26.05)
        async with AsyncSessionFactory() as s:
            cnt = await s.scalar(
                select(func.count()).select_from(CustomPreset).where(
                    CustomPreset.enabled == True,
                    CustomPreset.created_at >= datetime(2026, 5, 25, tzinfo=timezone.utc),
                )
            )
        print('новинок сегодня (enabled):', cnt)

        collage = await build_grid(bot, HERO, cols=2)
        admin = settings.admin_ids_list[0]
        from aiogram.types import BufferedInputFile
        if collage:
            sent = await bot.send_photo(
                admin, BufferedInputFile(collage, 'showcase.jpg'),
                caption='🧪 ЧЕРНОВИК канал-поста (НЕ опубликован). Текст ниже ⤵️',
            )
            print('collage sent, file_id:', sent.photo[-1].file_id)
        # сам текст отдельным сообщением (чтобы видеть рендер)
        await bot.send_message(admin, CAPTION, parse_mode='HTML',
                               disable_web_page_preview=True)
        print('draft caption sent to admin')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

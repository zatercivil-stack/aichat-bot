"""Генерит минималистичный логотип-аватарку и шлёт админу. ~$0.08."""
import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

from app.config import settings
from app.services import fal_image_service as F

PROMPT = (
    "A minimalist app icon logo on a deep violet-purple solid gradient background. "
    "Centered: a single bold elegant white Cyrillic letter 'Н' (looks like 'H'), "
    "with a subtle stylized female face profile silhouette integrated into the "
    "negative space of the letter. Flat modern vector design, clean crisp edges, "
    "high contrast, glossy premium tech-brand aesthetic, perfectly centered, "
    "symmetrical, no extra text, square 1:1 composition, app-icon style."
)


async def main():
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    try:
        img = await F.generate_image(PROMPT, image_size='square_hd')
        if not img:
            await bot.send_message(admin, '❌ Логотип не сгенерился.')
            return
        await bot.send_photo(
            admin, BufferedInputFile(img, 'neyra_logo.png'),
            caption='🟣 <b>Вариант логотипа-аватарки</b>\n\n'
                    'Если ок — поставь как аватар канала и бота (через BotFather '
                    '→ /setuserpic для бота). Не ок — перегенерю.',
        )
        # Документом — без сжатия, чтобы ставить аватаркой в полном качестве.
        await bot.send_document(
            admin, BufferedInputFile(img, 'neyra_logo_hd.png'),
            caption='📎 HD-версия (для аватарки без сжатия).',
        )
        print('logo sent')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

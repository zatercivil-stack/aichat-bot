"""Аватарка бота — портрет персоны «Нейра». Шлёт админу (ставить через BotFather
/setuserpic). ~$0.08."""
import asyncio
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile
from app.config import settings
from app.services import fal_image_service as F

PROMPT = (
    "App-icon style avatar portrait of a confident alt-girl (alternative "
    "e-girl subculture aesthetic). BLACK messy layered punk shag haircut with "
    "choppy wispy bangs framing the face. Smoky dark eye makeup, pale clear skin, "
    "edgy confident expression with a slight smirk, looking straight at camera. "
    "Layered silver chain choker with small pendants, a couple of ear piercings. "
    "Bold crimson-red studio background with dramatic warm red rim light on the "
    "hair and cheekbones. Glossy premium editorial aesthetic, vibrant and eye-"
    "catching, sharp focus on the face, head-and-shoulders close-up, perfectly "
    "centered, square 1:1, no text."
)


async def main():
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    try:
        img = await F.generate_image(PROMPT, image_size='square_hd')
        if not img:
            await bot.send_message(admin, '❌ Аватарка бота не сгенерилась.')
            return
        await bot.send_photo(
            admin, BufferedInputFile(img, 'neyra_avatar.png'),
            caption='🟣 <b>Аватарка бота — персона «Нейра»</b>\n\n'
                    'Ставится только через @BotFather: /setuserpic → @NeyraChatBot '
                    '→ отправь это фото (или HD-файл ниже).\n'
                    'Не нравится — перегенерю.',
        )
        await bot.send_document(
            admin, BufferedInputFile(img, 'neyra_avatar_hd.png'),
            caption='📎 HD-версия (для аватарки без сжатия).',
        )
        print('avatar sent')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

"""ТЕСТОВЫЙ пост о новых пресетах — шлётся АДМИНУ (не в канал @conturD).
Коллаж из 2 контрастных новых сцен + копирайт в голосе канала + CTA-кнопка.
Запуск:
    docker compose exec -T -e PYTHONPATH=/app bot python3 /app/scripts/test_channel_post_newpresets.py
"""
import asyncio

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
)

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.preset_previews import get_preview_file_id
from app.handlers.admin import _build_showcase_collage, PE_SPARKLES

CAPTION = (
    "🆕 <b>В Нейре — большое пополнение сцен.</b>\n\n"
    "Одно селфи, и ты в кадре, который не стыдно на аву:\n\n"
    "🐕 <b>Босс с добберманами</b> — ты, две собаки и красный суперкар\n"
    "🎲 <b>Бросок костей</b> — азартный кадр через портал\n"
    "☕ <b>Кофе по-джентльменски</b> — золотые очки, верхний ракурс\n"
    "🥤 <b>Дайнер-постер</b> — ретро в плитке, свой для парня и для девушки\n"
    "🎧 <b>За пультом диджея</b> — со своим сетапом\n\n"
    "И ещё десяток новых: токийская улица, белый бокс, рыжий панк и не только.\n\n"
    "Лицо в кадре — <b>1-в-1 твоё</b>, не чужая модель. Готово за минуту.\n\n"
    "👇 Жми, пришли селфи, выбирай сцену. Новичку <b>+500 кр</b> в подарок."
)


async def main():
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    try:
        # Коллаж: муж доберман + жен дайнер — показать диапазон (М и Ж).
        async with AsyncSessionFactory() as s:
            male_fid = await get_preview_file_id(s, 'doberman_boss', position=1)
            female_fid = await get_preview_file_id(s, 'diner_poster', position=0)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton.model_construct(
                text='Сделать фото с моим лицом',
                url='https://t.me/NeyraChatBot?start=promo_CHAN_NEWSCENES',
                icon_custom_emoji_id=PE_SPARKLES, style='success',
            )
        ]])
        if male_fid and female_fid:
            collage = await _build_showcase_collage(bot, male_fid, female_fid)
            await bot.send_photo(
                admin, BufferedInputFile(collage, 'newscenes.jpg'),
                caption='🧪 <b>ТЕСТ-ПОСТ (в канал НЕ ушёл)</b>\n\n' + CAPTION,
                parse_mode='HTML', reply_markup=kb,
            )
            print('test post sent to admin')
        else:
            print(f'no previews: male={bool(male_fid)} female={bool(female_fid)}')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

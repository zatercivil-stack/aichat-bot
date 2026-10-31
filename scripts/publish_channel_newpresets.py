"""Публикация поста о новых пресетах в @conturD (disable_notification=True).
Записывает в channel_posts. Запуск:
    docker compose exec -T -e PYTHONPATH=/app bot python3 /app/scripts/publish_channel_newpresets.py
"""
import asyncio
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.preset_previews import get_preview_file_id
from app.handlers.admin import PE_SPARKLES, _build_showcase_collage

CHANNEL_ID = "@conturD"

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
    try:
        async with AsyncSessionFactory() as s:
            male_fid = await get_preview_file_id(s, 'doberman_boss', position=1)
            female_fid = await get_preview_file_id(s, 'diner_poster', position=0)
        if not (male_fid and female_fid):
            print('NO previews — abort')
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton.model_construct(
                text='Сделать фото с моим лицом',
                url='https://t.me/NeyraChatBot?start=promo_CHAN_NEWSCENES',
                icon_custom_emoji_id=PE_SPARKLES, style='success',
            )
        ]])
        collage = await _build_showcase_collage(bot, male_fid, female_fid)
        sent = await bot.send_photo(
            CHANNEL_ID, BufferedInputFile(collage, 'newscenes.jpg'),
            caption=CAPTION, parse_mode='HTML', reply_markup=kb,
            disable_notification=True,
        )
        # Запись в channel_posts (трекинг + детект пропусков)
        from app.database.models import ChannelPost
        async with AsyncSessionFactory() as s:
            s.add(ChannelPost(
                topic='Анонс новых пресетов (вторая+третья пачка 24.05)',
                body=CAPTION, cta_type='neyra',
                scheduled_at=datetime.now(UTC),
                published_at=datetime.now(UTC),
                message_id=sent.message_id, status='published',
            ))
            await s.commit()
        print(f'PUBLISHED: https://t.me/conturD/{sent.message_id}')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

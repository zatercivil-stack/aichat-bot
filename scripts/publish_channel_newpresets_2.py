"""Пост о 2 новых пресетах (suit_smoke + trunk_pov) в @conturD.
Коллаж: 🖤 Костюм и сигарета (Ж) + 🚗 Кадр из багажника (М).

Предпросмотр (собрать коллаж в файл, НЕ постить):
    docker compose exec -T -e DRY_RUN=1 bot python3 /app/scripts/publish_channel_newpresets_2.py
Публикация:
    docker compose exec -T bot python3 /app/scripts/publish_channel_newpresets_2.py
"""
import asyncio
import os
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.preset_previews import get_preview_file_id
from app.handlers.admin import PE_SPARKLES, _build_showcase_collage

CHANNEL_ID = "@conturD"

CAPTION = (
    "🆕 <b>Две новые сцены в Нейре.</b>\n\n"
    "Одно селфи — и ты в кадре, который не стыдно на аву:\n\n"
    "🖤 <b>Костюм и сигарета</b> — холодный дерзкий портрет: строгий костюм, "
    "белая рубашка, сигарета. Дорогая небрежность.\n"
    "🚗 <b>Кадр из багажника</b> — кино-сцена через рыбий глаз, как в "
    "криминальной комедии 70-х.\n\n"
    "Работает и для парня, и для девушки — лицо в кадре <b>1-в-1 твоё</b>, "
    "не чужая модель. Готово за минуту.\n\n"
    "👇 Пришли селфи, выбери сцену. Новичку <b>+500 кр</b> в подарок."
)

DRAFT_PATH = "/app/data/_draft_newpresets2.jpg"


async def main():
    dry = bool(os.environ.get("DRY_RUN"))
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        async with AsyncSessionFactory() as s:
            female_fid = await get_preview_file_id(s, 'suit_smoke', position=0)
            male_fid = await get_preview_file_id(s, 'trunk_pov', position=1)
        if not (male_fid and female_fid):
            print('NO previews — abort')
            return
        collage = await _build_showcase_collage(bot, male_fid, female_fid)

        if dry:
            with open(DRAFT_PATH, 'wb') as fh:
                fh.write(collage)
            print(f'DRY_RUN: collage saved -> {DRAFT_PATH} ({len(collage)} bytes)')
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton.model_construct(
                text='Сделать фото с моим лицом',
                url='https://t.me/NeyraChatBot?start=promo_CHAN_NEWSCENES',
                icon_custom_emoji_id=PE_SPARKLES, style='success',
            )
        ]])

        # TEST=1 — шлём полный пост в ЛС владельцу (превью), в канал НЕ постим.
        if os.environ.get("TEST"):
            owner = settings.admin_ids_list[0] if settings.admin_ids_list else None
            if not owner:
                print('NO admin id — abort test')
                return
            await bot.send_photo(
                owner, BufferedInputFile(collage, 'newpresets2.jpg'),
                caption='🧪 ТЕСТ поста (в канал НЕ опубликовано):\n\n' + CAPTION,
                parse_mode='HTML', reply_markup=kb,
            )
            print(f'TEST sent to owner {owner}')
            return

        sent = await bot.send_photo(
            CHANNEL_ID, BufferedInputFile(collage, 'newpresets2.jpg'),
            caption=CAPTION, parse_mode='HTML', reply_markup=kb,
            disable_notification=True,
        )
        from app.database.models import ChannelPost
        async with AsyncSessionFactory() as s:
            s.add(ChannelPost(
                topic='Анонс новых пресетов: Костюм и сигарета + Кадр из багажника (01.06)',
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

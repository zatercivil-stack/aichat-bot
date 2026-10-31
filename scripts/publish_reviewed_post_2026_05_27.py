"""Опубликовать СОГЛАСОВАННЫЙ пост из bot_config['pending_review_post'] в
@conturD. Запускать ТОЛЬКО после явного «ок» владельца. Публикует ровно то,
что было прислано на проверку, silent (disable_notification), пишет в
channel_posts и ставит skip_next, чтобы авто-пост не задвоил.

Запуск: python -m scripts.publish_reviewed_post_2026_05_27
"""
import asyncio
import json
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import insert as sql_insert

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import ChannelPost
from app.database.repo.bot_config import get_config_value, set_config_value
from app.services.channel_publisher import CHANNEL_ID


async def main():
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        async with AsyncSessionFactory() as s:
            raw = await get_config_value(s, 'pending_review_post')
        if not raw:
            print('NO pending_review_post — нечего публиковать')
            return
        data = json.loads(raw)
        caption = data['caption']
        fid = data.get('image_fid')

        if fid:
            sent = await bot.send_photo(chat_id=CHANNEL_ID, photo=fid,
                                        caption=caption, parse_mode='HTML',
                                        disable_notification=True)
        else:
            sent = await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                          parse_mode='HTML',
                                          disable_web_page_preview=True,
                                          disable_notification=True)

        async with AsyncSessionFactory() as s:
            await s.execute(sql_insert(ChannelPost).values(
                topic=data.get('topic', 'catch-up 27.05'), body=caption,
                cta_type=data.get('cta_type', 'neira_plain'),
                scheduled_at=datetime.now(UTC),
                published_at=datetime.now(UTC),
                message_id=sent.message_id, status='published',
            ))
            await set_config_value(s, 'pending_review_post', '')
            await set_config_value(s, 'channel_publisher_skip_next', '1')
            await s.commit()

        print('PUBLISHED to', CHANNEL_ID, 'message_id:', sent.message_id)
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

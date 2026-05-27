"""Сгенерировать пропущенный сегодня (27.05) feature-пост канала и прислать
АДМИНУ в личку на проверку. НЕ публикует в @conturD. Сохраняет точное
содержимое (caption + image file_id) в bot_config['pending_review_post'],
чтобы после «ОК» опубликовать ровно то, что согласовано.

Запуск: python -m scripts.draft_channel_post_2026_05_27
"""
import asyncio
import json

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.bot_config import set_config_value
from app.services.channel_publisher import _build_post, _pick_topic, _to_photo_payload


async def main():
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        admin = settings.admin_ids_list[0]
        topic = await _pick_topic(mode='feature')
        full_text, image_source, cta_type, reply_markup = await _build_post(topic, 'feature')

        photo = _to_photo_payload(image_source)
        if photo is not None:
            sent = await bot.send_photo(admin, photo=photo, caption=full_text,
                                        parse_mode='HTML', reply_markup=reply_markup)
            fid = image_source if isinstance(image_source, str) else (
                sent.photo[-1].file_id if sent.photo else None)
        else:
            await bot.send_message(admin, full_text, parse_mode='HTML',
                                   reply_markup=reply_markup)
            fid = None

        await bot.send_message(
            admin,
            '👆 <i>ЧЕРНОВИК сегодняшнего поста (НЕ опубликован). '
            'Скажи «ок» — выложу в @conturD ровно это.</i>',
            parse_mode='HTML')

        async with AsyncSessionFactory() as s:
            await set_config_value(s, 'pending_review_post', json.dumps({
                'caption': full_text, 'image_fid': fid,
                'topic': topic, 'cta_type': cta_type,
            }))
            await s.commit()

        print('topic:', topic)
        print('cta_type:', cta_type)
        print('image_fid:', (fid or 'NONE')[:30])
        print('caption_len:', len(full_text))
        print('stashed -> bot_config[pending_review_post]')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())

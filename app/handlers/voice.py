"""Голосовой ввод в чате — отключён в рамках visual-only pivot.

DeepSeek через OpenRouter + транскрипция WAV = убыточно. На любое голосовое
отвечаем редиректом в фото-воронку.

История логики (текст-чат через OpenRouter, ffmpeg-конверсия, voice-service)
осталась в git до коммита отключения — восстановить при возврате к чату.
"""
from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.base import AsyncSessionFactory
from app.database.repo.users import get_user
from app.keyboards.main import PE_FRAME, PE_HOME, PE_ROBOT

logger = structlog.get_logger(__name__)
router = Router()


@router.message(F.voice | F.audio)
async def handle_voice(message: Message) -> None:
    user_tg_id = message.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return
        if user.is_banned:
            await message.answer('🚫 Ваш аккаунт заблокирован.')
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Открыть каталог настроений',
            callback_data='gallery_open',
            icon_custom_emoji_id=PE_FRAME, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Подменить лицо в чужом фото',
            callback_data='face_swap_start',
            icon_custom_emoji_id=PE_ROBOT, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='В главное меню',
            callback_data='main_menu',
            icon_custom_emoji_id=PE_HOME, style='danger',
        )],
    ])
    await message.answer(
        '🪄 <b>Голосовые я не разбираю — я делаю фото с твоим лицом.</b>\n\n'
        'Кинь селфи или открой каталог настроений 👇',
        parse_mode='HTML',
        reply_markup=kb,
    )

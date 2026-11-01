from __future__ import annotations

from io import BytesIO

import structlog
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import (
    MODEL_COSTS,
    add_credits,
    deduct_credits,
)
from app.database.repo.users import get_user, is_paid_user
from app.keyboards.main import PE_CROSS, buy_or_invite_kb
from app.services import image_service, watermark
from app.states import ImageState

logger = structlog.get_logger(__name__)
router = Router()

_BUY_KB = buy_or_invite_kb()


@router.callback_query(lambda c: c.data == "img_start")
async def cb_img_start(callback: CallbackQuery, state: FSMContext) -> None:
    user_tg_id = callback.from_user.id

    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await callback.answer("Нажми /start", show_alert=True)
            return
        from app.services import channel_service

        await channel_service.grant_daily_aware(callback.bot, session, user)
        cost = MODEL_COSTS["image"]
        from app.database.repo.credits import total_balance

        user_total = total_balance(user)
        if user_total < cost:
            await session.commit()
            await callback.message.edit_text(
                f"🎨 Нужно <b>{cost} кредитов</b> для картинки.\n"
                f"У тебя <b>{user_total} кредитов</b>.\n\n"
                f"Пополни баланс — и создавай изображения!",
                reply_markup=_BUY_KB,
            )
            await callback.answer()
            return
        await session.commit()

    await state.set_state(ImageState.waiting_prompt)
    await callback.message.edit_text(
        "🎨 <b>Опиши что хочешь создать</b>\n\n"
        "Один запрос = <b>60 кредитов</b> = одно уникальное изображение.\n\n"
        "<b>Примеры:</b>\n"
        '• "закат над Токио, аниме стиль"\n'
        '• "портрет волка, масло, реализм"\n'
        '• "логотип для кафе с чашкой кофе"\n'
        '• "космический корабль в стиле ретро"\n\n'
        "<b>Напиши описание:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton.model_construct(
                        text="Отмена",
                        callback_data="cancel_img",
                        icon_custom_emoji_id=PE_CROSS,
                        style="danger",
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "cancel_img")
async def cb_cancel_img(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from app.database.base import AsyncSessionFactory
    from app.database.repo.users import get_user
    from app.handlers.start import _send_welcome_with_collage, build_menu_text
    from app.keyboards.main import main_menu_kb_for_user

    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        text = await build_menu_text(session, callback.message.bot, user)
    # «Отмена» из /img → возврат в главное меню с коллажем (как cb_main_menu).
    try:
        await callback.message.delete()
    except Exception:
        pass
    kb = await main_menu_kb_for_user(
        callback.from_user.id,
        user.credits if user else 0,
    )
    await _send_welcome_with_collage(callback.message, text, kb)
    await callback.answer()


@router.message(ImageState.waiting_prompt)
async def handle_image_prompt(message: Message, state: FSMContext) -> None:
    await state.clear()
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("❌ Пустой запрос.")
        return
    await _generate(message, prompt)


@router.message(Command("img"))
async def cmd_img(message: Message, command: CommandObject) -> None:
    prompt = (command.args or "").strip()
    if not prompt:
        await message.answer(
            "🎨 Опиши что хочешь:\n<code>/img котёнок в космосе</code>",
            parse_mode="HTML",
        )
        return
    await _generate(message, prompt)


_IMAGE_BUY_KB = buy_or_invite_kb()


async def _generate(message: Message, prompt: str) -> None:
    user_tg_id = message.from_user.id
    cost = MODEL_COSTS["image"]

    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer("Нажми /start для начала работы.")
            return

        from app.services import channel_service

        await channel_service.grant_daily_aware(message.bot, session, user)

        from app.database.repo.credits import total_balance

        user_total = total_balance(user)
        if user_total < cost:
            await session.commit()
            await message.answer(
                f"🎨 Нужно <b>{cost} кредитов</b> для картинки.\n"
                f"У тебя {user_total} кредитов.\n\n"
                f"Пополни баланс — и создавай изображения!",
                reply_markup=_IMAGE_BUY_KB,
            )
            return

        ok = await deduct_credits(session, user, cost)
        remaining = user.credits
        paid = await is_paid_user(session, user.id)
        await session.commit()
        if not ok:
            await message.answer("💳 Недостаточно кредитов.", reply_markup=_IMAGE_BUY_KB)
            return

    status = await message.answer("🎨 Генерирую...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")

    image_bytes = await image_service.generate_image(prompt)

    if not image_bytes:
        # Возвращаем кредиты при ошибке
        async with AsyncSessionFactory() as refund_session:
            user = await get_user(refund_session, user_tg_id)
            if user:
                await add_credits(refund_session, user, cost)
                await refund_session.commit()
        await status.edit_text(
            "❌ Не удалось сгенерировать. Попробуй другой запрос или повтори позже."
        )
        return

    if not paid:
        image_bytes = watermark.add_watermark(image_bytes)

    await status.delete()

    # Преобразуем байты в BufferedInputFile для отправки в Telegram
    bio = BytesIO(image_bytes)
    bio.name = "image.png"
    photo_file = BufferedInputFile(bio.read(), filename="image.png")

    caption = f"🎨 <b>{prompt}</b>\n\n💰 Баланс: <b>{remaining} кредитов</b>"
    if not paid:
        caption += "\n\n<i>💡 Купи пак или подписку — 30 дней без водяного знака на всех фото.</i>"

    await message.answer_photo(
        photo=photo_file,
        caption=caption,
        parse_mode="HTML",
    )

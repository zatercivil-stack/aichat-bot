from __future__ import annotations

import structlog
from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, PreCheckoutQuery

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import add_credits, get_pack_by_id
from app.database.repo.users import get_user
from app.keyboards.main import PE_FRAME, PE_HANDSHAKE, PE_PAINT

logger = structlog.get_logger(__name__)
router = Router()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(lambda m: m.successful_payment is not None)
async def successful_payment(message: Message) -> None:
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    parts = payload.split(':')
    stars = payment_info.total_amount
    charge_id = payment_info.telegram_payment_charge_id

    if parts[0] == 'credits' and len(parts) >= 2:
        await _handle_credits_payment(message, int(parts[1]), stars, charge_id)
    else:
        logger.error('Unknown payment payload', payload=payload)
        await message.answer('⚠️ Ошибка обработки платежа. Напишите в поддержку.')


async def _handle_credits_payment(
    message: Message, credits_amount: int, stars: int, charge_id: str
) -> None:
    user_tg_id = message.from_user.id

    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer('❌ Пользователь не найден. Нажми /start')
            return

        await add_credits(session, user, credits_amount)
        await session.commit()
        # Refresh user to get updated credits
        await session.refresh(user)
        new_balance = user.credits

    logger.info('Credits purchased', user_id=user_tg_id, credits=credits_amount, stars=stars)

    success_text = (
        f'🎉 <b>Готово!</b> <b>{credits_amount:,} кредитов</b> на счету.\n\n'
        f'💳 Твой баланс: <b>{new_balance:,} кредитов</b>\n\n'
        f'<b>Что хочешь сделать прямо сейчас?</b>'
    )
    # visual-only pivot: "Задать вопрос" заменён на каталог настроений (наша
    # money-maker воронка), вторая кнопка — рисование с нуля.
    success_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Каталог настроений',
            callback_data='gallery_open',
            icon_custom_emoji_id=PE_FRAME, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Создать картинку с нуля',
            callback_data='img_start',
            icon_custom_emoji_id=PE_PAINT,
        )],
        [InlineKeyboardButton.model_construct(
            text='Пригласить друга +150 кр',
            callback_data='ref_menu',
            icon_custom_emoji_id=PE_HANDSHAKE,
        )],
    ])
    await message.answer(success_text, reply_markup=success_kb)

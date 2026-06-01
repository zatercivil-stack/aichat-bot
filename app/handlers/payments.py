from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, PreCheckoutQuery

from app.config import settings
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

    # Новый единый путь: altpay:<payment_id> (Stars через payments_alt) →
    # активация через process_succeeded_payment (кредиты/подписка, идемпотентно).
    if payload.startswith('altpay:'):
        try:
            pid = int(payload.split(':', 1)[1])
        except ValueError:
            await message.answer('⚠️ Ошибка платежа. Напиши в поддержку.')
            return
        from app.services.payment_processing import process_succeeded_payment
        ok = await process_succeeded_payment(pid, external_id=charge_id, bot=message.bot)
        if not ok:
            await message.answer('⚠️ Оплата прошла, но активация не удалась. '
                                 'Напиши в поддержку — начислим вручную.')
        return

    if parts[0] == 'credits' and len(parts) >= 2:
        await _handle_credits_payment(message, int(parts[1]), stars, charge_id)
    else:
        logger.error('Unknown payment payload', payload=payload)
        await message.answer('⚠️ Ошибка обработки платежа. Напишите в поддержку.')


# ── Кнопки альт-оплаты: Stars (⭐) и Крипта (CryptoBot) ──────────────────────
@router.callback_query(F.data.startswith('payst:'))
async def cb_pay_stars(callback: CallbackQuery) -> None:
    product_id = callback.data.split(':', 1)[1]
    async with AsyncSessionFactory() as s:
        user = await get_user(s, callback.from_user.id)
    if not user:
        await callback.answer('Нажми /start', show_alert=True)
        return
    from app.services.payments_alt import send_stars_invoice
    ok = await send_stars_invoice(callback.bot, callback.message.chat.id, user.id,
                                  callback.from_user.id, product_id)
    await callback.answer('Открываю оплату Stars…' if ok else 'Не вышло, попробуй позже',
                          show_alert=not ok)


@router.callback_query(F.data.startswith('paycr:'))
async def cb_pay_crypto(callback: CallbackQuery) -> None:
    product_id = callback.data.split(':', 1)[1]
    async with AsyncSessionFactory() as s:
        user = await get_user(s, callback.from_user.id)
    if not user:
        await callback.answer('Нажми /start', show_alert=True)
        return
    from app.services.payments_alt import create_crypto_invoice, product_info
    await callback.answer('Создаю счёт…')
    bot_username = settings.BOT_USERNAME or 'NeyraChatBot'
    url = await create_crypto_invoice(user.id, callback.from_user.id, product_id, bot_username)
    if not url:
        await callback.answer('Крипта-оплата временно недоступна', show_alert=True)
        return
    from app.keyboards.main import PE_BACK
    info = product_info(product_id)
    back = 'subscription' if (info and info.get('kind') == 'subscription') else f'buy_pack:{product_id}'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='💎 Оплатить криптой', url=url, style='success')],
        [InlineKeyboardButton.model_construct(
            text='Назад', callback_data=back, icon_custom_emoji_id=PE_BACK, style='danger')],
    ])
    # Редактируем ТЕКУЩЕЕ сообщение (без нового) — меньше шума.
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        await callback.message.answer('💎 Счёт создан — оплати:', reply_markup=kb)


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

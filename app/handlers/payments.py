from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.users import get_user

logger = structlog.get_logger(__name__)
router = Router()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(lambda m: m.successful_payment is not None)
async def successful_payment(message: Message) -> None:
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    parts = payload.split(":")
    stars = payment_info.total_amount
    charge_id = payment_info.telegram_payment_charge_id

    # Новый единый путь: altpay:<payment_id> (Stars через payments_alt) →
    # активация через process_succeeded_payment (кредиты/подписка, идемпотентно).
    if payload.startswith("altpay:"):
        try:
            pid = int(payload.split(":", 1)[1])
        except ValueError:
            await message.answer("⚠️ Ошибка платежа. Напиши в поддержку.")
            return
        from app.services.payment_processing import process_succeeded_payment

        ok = await process_succeeded_payment(pid, external_id=charge_id, bot=message.bot)
        if not ok:
            await message.answer(
                "⚠️ Оплата прошла, но активация не удалась. Напиши в поддержку — начислим вручную."
            )
        return

    if parts[0] == "credits" and len(parts) >= 2:
        await _handle_credits_payment(message, int(parts[1]), stars, charge_id)
    else:
        logger.error("Unknown payment payload", payload=payload)
        await message.answer("⚠️ Ошибка обработки платежа. Напишите в поддержку.")


# ── Кнопки альт-оплаты: Stars (⭐) и Крипта (CryptoBot) ──────────────────────
@router.callback_query(F.data.startswith("payst:"))
async def cb_pay_stars(callback: CallbackQuery) -> None:
    product_id = callback.data.split(":", 1)[1]
    async with AsyncSessionFactory() as s:
        user = await get_user(s, callback.from_user.id)
    if not user:
        await callback.answer("Нажми /start", show_alert=True)
        return
    from app.services.payments_alt import send_stars_invoice

    ok = await send_stars_invoice(
        callback.bot, callback.message.chat.id, user.id, callback.from_user.id, product_id
    )
    await callback.answer(
        "Открываю оплату Stars…" if ok else "Не вышло, попробуй позже", show_alert=not ok
    )


@router.callback_query(F.data.startswith("paycr:"))
async def cb_pay_crypto(callback: CallbackQuery) -> None:
    product_id = callback.data.split(":", 1)[1]
    async with AsyncSessionFactory() as s:
        user = await get_user(s, callback.from_user.id)
    if not user:
        await callback.answer("Нажми /start", show_alert=True)
        return
    from app.services.payments_alt import create_crypto_invoice, product_info

    await callback.answer("Создаю счёт…")
    bot_username = settings.BOT_USERNAME or "NeyraChatBot"
    url = await create_crypto_invoice(user.id, callback.from_user.id, product_id, bot_username)
    if not url:
        await callback.answer("Оплата Cryptobot временно недоступна", show_alert=True)
        return
    from app.keyboards.main import PE_BACK

    info = product_info(product_id)
    back = (
        "subscription"
        if (info and info.get("kind") == "subscription")
        else f"buy_pack:{product_id}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text="💎 Оплатить через Cryptobot", url=url, style="success"
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Назад", callback_data=back, icon_custom_emoji_id=PE_BACK, style="danger"
                )
            ],
        ]
    )
    # Редактируем ТЕКУЩЕЕ сообщение (без нового) — меньше шума.
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        await callback.message.answer("💎 Счёт создан — оплати:", reply_markup=kb)


async def _handle_credits_payment(
    message: Message, credits_amount: int, stars: int, charge_id: str
) -> None:
    # Legacy invoices predate the local payment ID in invoice_payload. Persist a
    # ledger record under the user lock, then reuse the normal activation path.
    from decimal import Decimal

    from sqlalchemy import select

    from app.database.models import Plan, User
    from app.database.repo.payments import create_payment, get_payment_by_external_id
    from app.services.payment_processing import process_succeeded_payment

    if credits_amount <= 0 or stars <= 0 or not charge_id:
        logger.error("Invalid legacy Stars payment")
        return
    async with AsyncSessionFactory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == message.from_user.id).with_for_update()
        )
        if not user:
            await message.answer("❌ Пользователь не найден. Нажми /start")
            return
        payment = await get_payment_by_external_id(session, charge_id)
        if payment and payment.user_id != user.id:
            logger.error("Stars charge belongs to a different user")
            return
        if payment is None:
            payment = await create_payment(
                session,
                user.id,
                Decimal(stars),
                "XTR",
                "stars",
                Plan.FREE,
                extra={"credits": credits_amount, "user_tg_id": user.telegram_id},
            )
            payment.external_id = charge_id
        payment_id = payment.id
        await session.commit()
    await process_succeeded_payment(payment_id, external_id=charge_id, bot=message.bot)

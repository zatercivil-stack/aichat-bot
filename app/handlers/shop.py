from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Plan, PaymentStatus
from app.database.repo.credits import CREDIT_PACKS, add_credits, get_pack_by_id
from app.database.repo.payments import create_payment, get_pending_payments, update_payment
from aiogram import Bot
from app.database.repo.users import get_user
from app.keyboards.main import PE_CARD
from app.keyboards.shop import confirm_pack_kb, credits_info_text, shop_kb

logger = structlog.get_logger(__name__)
router = Router()


@router.message(Command('shop'))
async def cmd_shop(message: Message) -> None:
    await _show_shop(message)


@router.callback_query(lambda c: c.data == 'shop')
async def cb_shop(callback: CallbackQuery) -> None:
    user_tg_id = callback.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        credits = user.credits if user else 0

    # safe_edit_or_answer: главное меню — это фото с caption, edit_text по
    # такому сообщению падает с TelegramBadRequest → юзер раньше нажимал
    # «Купить кредиты разово» и ничего не происходило (24.05 фикс).
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(
        callback,
        credits_info_text(credits),
        reply_markup=shop_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith('buy_pack:'))
async def cb_buy_pack(callback: CallbackQuery) -> None:
    pack_id = callback.data.split(':', 1)[1]
    pack = get_pack_by_id(pack_id)
    if not pack:
        await callback.answer('Пакет не найден.', show_alert=True)
        return

    cr = pack['credits']
    # Считаем в единицах фото-операций. Единый тариф: одна правка = 200 кр
    # (квалити, базовая цена; запуск-акция истекла 19.05).
    photos = cr // 200
    expires_days = pack.get('expires_days')
    is_burn = expires_days is not None

    # Шапка зависит от типа пака
    if is_burn:
        header = (
            f"🔥 <b>Срочный пак {cr:,} кредитов · {expires_days} дней</b>\n"
            f"<i>В 2-3 раза БОЛЬШЕ кредитов за ту же цену — но сгорят через "
            f"{expires_days} дн. Бери если планируешь использовать сразу.</i>\n\n"
        )
        expire_line = f"⏱ <b>Срок:</b> сгорят через {expires_days} дней\n"
    else:
        header = (
            f"{pack['emoji']} <b>ВЕЧНЫЙ пак {cr:,} кредитов</b>\n"
            f"<i>Никаких сроков — лежат годами. Слегка дороже на кредит,\n"
            f"зато спокойно — не торопишься.</i>\n\n"
        )
        expire_line = f"✓ Кредиты <b>не сгорают</b> никогда\n"

    text = (
        f"{header}"
        f"Этого хватит примерно на:\n"
        f"• 💎 <b>{photos}</b> фото с твоим лицом в любой сцене\n"
        f"  <i>(Балет, старинное авто, Париж, неон, кино-нуар…)</i>\n\n"
        f"💳 К оплате: <b>{pack['rub']}₽</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✓ Зачисление за <b>1 минуту</b>\n"
        f"{expire_line}"
        f"✓ <b>Без автосписаний</b> — платишь один раз\n"
        f"✓ <b>Без водяного знака</b> на фото в течение 30 дней\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(callback, text, reply_markup=confirm_pack_kb(pack_id))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith('pay_rub:'))
async def cb_pay_rub(callback: CallbackQuery) -> None:
    if not settings.yookassa_enabled:
        await callback.answer(
            'Оплата временно недоступна. Попробуйте позже.',
            show_alert=True,
        )
        return

    pack_id = callback.data.split(':', 1)[1]
    pack = get_pack_by_id(pack_id)
    if not pack:
        await callback.answer('Пакет не найден.', show_alert=True)
        return

    await callback.answer()

    try:
        from yookassa import Configuration, Payment as YKPayment

        Configuration.account_id = settings.YOOKASSA_SHOP_ID
        Configuration.secret_key = settings.YOOKASSA_SECRET_KEY

        async with AsyncSessionFactory() as session:
            user = await get_user(session, callback.from_user.id)
            if not user:
                await callback.message.answer('❌ Пользователь не найден. Нажми /start')
                return

            db_payment = await create_payment(
                session,
                user_id=user.id,
                amount=Decimal(pack['rub']),
                currency='RUB',
                payment_method='yookassa',
                plan=Plan.FREE,
                extra={
                    'credits': pack['credits'],
                    'pack_id': pack['id'],
                    'user_tg_id': callback.from_user.id,
                    # expires_days = None для вечных, число для burn-паков
                    'expires_days': pack.get('expires_days'),
                },
            )
            await session.commit()
            payment_db_id = db_payment.id

        yk_payment = YKPayment.create({
            'amount': {'value': str(pack['rub']), 'currency': 'RUB'},
            'confirmation': {'type': 'redirect', 'return_url': settings.yookassa_return_url},
            'capture': True,
            'description': f"Нейра: {pack['name']} — {pack['credits']:,} кредитов",
            'metadata': {'payment_db_id': str(payment_db_id)},
        }, str(uuid.uuid4()))

        # Сохранить ID платежа YooKassa в базе данных
        async with AsyncSessionFactory() as session:
            await update_payment(session, payment_db_id, PaymentStatus.PENDING, yk_payment.id)
            await session.commit()

        pay_url = yk_payment.confirmation.confirmation_url

        from app.keyboards.main import PE_BACK
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton.model_construct(
                text='Перейти к оплате',
                url=pay_url,
                icon_custom_emoji_id=PE_CARD, style='success',
            )],
            [InlineKeyboardButton.model_construct(
                text='Назад', callback_data='shop',
                icon_custom_emoji_id=PE_BACK, style='danger',
            )],
        ])
        # 24.05: было answer (создавало 3-е сообщение в чате). Теперь edit —
        # оплата живёт в том же экране, что и выбор пака → меньше шума.
        from app.utils import safe_edit_or_answer
        await safe_edit_or_answer(
            callback,
            f"💳 <b>Оплата: {pack['credits']:,} кредитов за {pack['rub']}₽</b>\n\n"
            f"✅ После оплаты <b>{pack['credits']:,} кредитов</b> зачислятся на твой баланс\n"
            f"⏱ <i>Автоматически за 1 минуту</i>\n\n"
            f"🔔 Пришлю уведомление как только кредиты придут.\n\n"
            f"💡 <i>Если страница оплаты не открывается — выключи VPN "
            f"и нажми кнопку снова.</i>\n\n"
            f"Нажми кнопку ниже 👇",
            reply_markup=kb,
        )

    except Exception as e:
        logger.error('YooKassa payment creation failed', error=e, exc_info=True)
        await callback.message.answer('❌ Ошибка создания платежа. Попробуйте позже.')


@router.callback_query(lambda c: c.data and c.data.startswith('confirm_pack:'))
async def cb_confirm_pack(callback: CallbackQuery) -> None:
    pack_id = callback.data.split(':', 1)[1]
    pack = get_pack_by_id(pack_id)
    if not pack:
        await callback.answer('Пакет не найден.', show_alert=True)
        return

    payload = f"credits:{pack['credits']}:{callback.from_user.id}"
    await callback.message.answer_invoice(
        title=f"Нейра — {pack['emoji']} {pack['name']}",
        description=f"Нейра: {pack['credits']:,} кредитов · фото-сцены, face-swap, стили",
        payload=payload,
        provider_token='',
        currency='XTR',
        prices=[LabeledPrice(label=f"{pack['credits']:,} кредитов", amount=pack['stars'])],
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == 'complete_pending')
async def cb_complete_pending(callback: CallbackQuery) -> None:
    """Ручное завершение висящих платежей для админа"""
    if callback.from_user.id not in settings.admin_ids_list:
        await callback.answer('❌ Нет доступа', show_alert=True)
        return

    async with AsyncSessionFactory() as session:
        pending = await get_pending_payments(session)
        if not pending:
            await callback.answer('Нет висящих платежей', show_alert=True)
            return

        completed = 0
        for p in pending:
            credits = p.extra.get('credits', 0)
            user_tg_id = p.extra.get('user_tg_id')
            if credits and user_tg_id:
                user = await get_user(session, int(user_tg_id))
                if user:
                    await add_credits(session, user, credits)
                    await update_payment(session, p.id, PaymentStatus.SUCCEEDED, external_id='manual')
                    completed += 1
        await session.commit()

    await callback.message.answer(f'✅ Завершено {completed} платежей, кредиты зачислены.')
    await callback.answer()


async def _show_shop(message: Message) -> None:
    user_tg_id = message.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        credits = user.credits if user else 0

    await message.answer(
        credits_info_text(credits),
        parse_mode='HTML',
        reply_markup=shop_kb(),
    )

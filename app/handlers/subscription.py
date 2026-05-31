from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import PaymentStatus, Plan
from app.database.repo.credits import SUBSCRIPTION_PLANS, get_subscription_plan
from app.database.repo.payments import create_payment, update_payment
from app.database.repo.subscriptions import get_subscription
from app.database.repo.users import get_user
from app.keyboards.main import (
    PE_AU_GOLD, PE_BACK, PE_CALENDAR, PE_CARD, PE_HOME, PE_LIGHTNING, PE_ROCKET,
)

logger = structlog.get_logger(__name__)
router = Router()


def _format_plans_block(recurring_enabled: bool = False) -> str:
    """Текст плашек с тарифами. Строку про автопродление показываем ТОЛЬКО когда
    у YooKassa включены рекуррентные платежи (флаг yookassa_recurring_enabled).
    Иначе врать юзеру нельзя."""
    base = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>СТАРТ — 149₽/мес</b>\n"
        "• <b>1 000 кредитов</b> сразу = ~5 сцен\n"
        "• <b>+30 кр</b> каждый день = ещё ~4 сцены в месяц\n"
        "• Хватит на пробу разных настроений из коллекции\n\n"
        "🚀 <b>ПРО — 399₽/мес</b>  ✨ <b>самый популярный</b>\n"
        "• <b>5 000 кредитов</b> сразу = ~25 сцен\n"
        "• <b>+50 кр</b> каждый день = ещё ~7 сцен в месяц\n"
        "• Хватит на свою фотоленту 1-2 раза в неделю\n\n"
        "💎 <b>МАКСИМУМ — 890₽/мес</b>\n"
        "• <b>15 000 кредитов</b> сразу = ~75 сцен\n"
        "• <b>+100 кр</b> каждый день = ещё ~15 сцен в месяц\n"
        "• Безлимит для пользователя-блогера\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💳 Оплата картой, СБП и другими способами"
    )
    if recurring_enabled:
        base += "\n🔁 Автопродление включается при оплате · отключить в /sub одной кнопкой"
    return base


async def _recurring_enabled() -> bool:
    """Проверяет глобальный флаг рекуррентов из bot_config."""
    from app.database.repo.bot_config import get_config_value
    async with AsyncSessionFactory() as session:
        raw = (await get_config_value(session, 'yookassa_recurring_enabled') or '0').strip()
    return raw in ('1', 'true', 'True', 'yes')


def _plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Оформить СТАРТ — 149₽/мес',
            callback_data='choose_plan:starter',
            icon_custom_emoji_id=PE_LIGHTNING, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='Оформить ПРО — 399₽/мес ✨',
            callback_data='choose_plan:pro',
            icon_custom_emoji_id=PE_ROCKET, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Оформить МАКСИМУМ — 890₽/мес',
            callback_data='choose_plan:max',
            icon_custom_emoji_id=PE_AU_GOLD, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='Не нужна подписка — купить разово',
            callback_data='shop',
            icon_custom_emoji_id=PE_CARD,
        )],
        [InlineKeyboardButton.model_construct(
            text='В главное меню',
            callback_data='main_menu',
            icon_custom_emoji_id=PE_HOME, style='danger',
        )],
    ])


def _renew_kb(current_plan_id: str | None, auto_renew_on: bool = False,
              recurring_enabled: bool = False) -> InlineKeyboardMarkup:
    # Plan-emoji → premium. Если плана нет в карте — fallback на текст с эмодзи.
    _plan_pe = {'⚡': PE_LIGHTNING, '🚀': PE_ROCKET, '💎': PE_AU_GOLD, '🥇': PE_AU_GOLD}
    rows = []
    for plan in SUBSCRIPTION_PLANS:
        marker = ' ✅' if plan['id'] == current_plan_id else ''
        style = 'success' if plan['id'] == current_plan_id else 'primary'
        pe_id = _plan_pe.get(plan['emoji'])
        if pe_id:
            rows.append([InlineKeyboardButton.model_construct(
                text=f"Продлить {plan['name']} — {plan['rub']}₽{marker}",
                callback_data=f"choose_plan:{plan['id']}",
                icon_custom_emoji_id=pe_id, style=style,
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=f"{plan['emoji']} Продлить {plan['name']} — {plan['rub']}₽{marker}",
                callback_data=f"choose_plan:{plan['id']}",
                style=style,
            )])
    # Toggle автопродления показываем ТОЛЬКО при включённом recurring flag —
    # иначе кнопка обманчива (тык → alert «карта не сохранена», т.к.
    # save_payment_method не передаётся при оплате).
    if current_plan_id and recurring_enabled:
        if auto_renew_on:
            rows.append([InlineKeyboardButton.model_construct(
                text='Автопродление: ВКЛ · отключить',
                callback_data='sub_cancel_renew',
                icon_custom_emoji_id=PE_CALENDAR, style='danger',
            )])
        else:
            rows.append([InlineKeyboardButton.model_construct(
                text='Автопродление: ВЫКЛ · включить',
                callback_data='sub_enable_renew',
                icon_custom_emoji_id=PE_CALENDAR, style='success',
            )])
    rows.append([InlineKeyboardButton.model_construct(
        text='Купить кредиты разово',
        callback_data='shop',
        icon_custom_emoji_id=PE_CARD,
    )])
    rows.append([InlineKeyboardButton.model_construct(
        text='В главное меню',
        callback_data='main_menu',
        icon_custom_emoji_id=PE_HOME, style='danger',
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_subscription_view(user_tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает текст и клавиатуру для просмотра подписок."""
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            return ('❌ Пользователь не найден. Нажми /start', _plans_kb())
        sub = await get_subscription(session, user.id)

    now = datetime.utcnow()
    active = False
    plan_dict = None
    expires_str = ''
    days_left = 0

    if sub and sub.is_active and sub.expires_at:
        sub_expires = sub.expires_at
        if sub_expires.tzinfo is not None:
            sub_expires = sub_expires.replace(tzinfo=None)
        if sub_expires > now:
            active = True
            plan_val = sub.plan.value if hasattr(sub.plan, 'value') else str(sub.plan)
            plan_dict = next((p for p in SUBSCRIPTION_PLANS if p['plan_enum'] == plan_val), None)
            expires_str = sub_expires.strftime('%d.%m.%Y')
            days_left = max(0, (sub_expires - now).days)

    recurring_enabled = await _recurring_enabled()

    if active and plan_dict:
        auto_renew_on = bool(getattr(sub, 'auto_renew', False) and getattr(sub, 'payment_method_id', None))
        # Строку про автопродление показываем только когда оно реально работает.
        if recurring_enabled and auto_renew_on:
            renew_line = (
                f"🔁 Автопродление: <b>ВКЛ</b> — спишется {plan_dict['rub']}₽ на 30 дней "
                f"перед окончанием срока.\n\n"
            )
        elif recurring_enabled:
            renew_line = (
                f"🔁 Автопродление: <b>ВЫКЛ</b> — после окончания срока надо будет "
                f"оформить заново.\n\n"
            )
        else:
            # Recurring глобально выключен — не упоминаем автопродление вообще.
            renew_line = (
                f"<i>После окончания срока продли подпиской ещё раз — "
                f"одна оплата = +30 дней.</i>\n\n"
            )
        text = (
            f"✅ <b>У тебя активна подписка {plan_dict['name']}</b>\n"
            f"Действует до: <b>{expires_str}</b> (через {days_left} дн.)\n"
            f"{renew_line}"
            f"{_format_plans_block(recurring_enabled=recurring_enabled)}"
        )
        return text, _renew_kb(plan_dict['id'], auto_renew_on=auto_renew_on,
                                recurring_enabled=recurring_enabled)

    text = (
        "💎 <b>Подписки Нейра</b>\n\n"
        "С подпиской ты получаешь сразу много кредитов и ежедневный бонус "
        "— <b>выгоднее, чем покупать пакетами</b>.\n\n"
        f"{_format_plans_block(recurring_enabled=recurring_enabled)}"
    )
    return text, _plans_kb()


@router.message(Command('sub'))
async def cmd_sub(message: Message) -> None:
    text, kb = await _build_subscription_view(message.from_user.id)
    await message.answer(text, parse_mode='HTML', reply_markup=kb)


@router.callback_query(lambda c: c.data == 'subscription')
async def cb_subscription(callback: CallbackQuery) -> None:
    text, kb = await _build_subscription_view(callback.from_user.id)
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith('plan_info:'))
async def cb_plan_info(callback: CallbackQuery) -> None:
    from app.keyboards.main import back_kb
    text, _ = await _build_subscription_view(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=back_kb('subscription'))
    await callback.answer()


@router.callback_query(lambda c: c.data == 'sub_cancel_renew')
async def cb_sub_cancel_renew(callback: CallbackQuery) -> None:
    """Отключение автопродления. Подписка остаётся активна до expires_at."""
    # Если recurring глобально выключен — кнопки уже нет в новых рендерах,
    # но юзер может ткнуть старую (висит в истории). Отвечаем мягко.
    if not await _recurring_enabled():
        await callback.answer('Автопродление сейчас не настроено — функция временно недоступна.',
                              show_alert=True)
        return
    from sqlalchemy import update as sa_update
    from app.database.models import Subscription
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        if not user:
            await callback.answer('Нажми /start', show_alert=True)
            return
        await session.execute(
            sa_update(Subscription)
            .where(Subscription.user_id == user.id)
            .values(auto_renew=False)
        )
        await session.commit()
    await callback.answer('Автопродление отключено')
    # Re-render subscription view
    text, kb = await _build_subscription_view(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode='HTML', reply_markup=kb)


@router.callback_query(lambda c: c.data == 'sub_enable_renew')
async def cb_sub_enable_renew(callback: CallbackQuery) -> None:
    """Включение автопродления — только если есть сохранённый payment_method."""
    if not await _recurring_enabled():
        await callback.answer('Автопродление сейчас не настроено — функция временно недоступна.',
                              show_alert=True)
        return
    from sqlalchemy import update as sa_update, select as sa_select
    from app.database.models import Subscription
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        if not user:
            await callback.answer('Нажми /start', show_alert=True)
            return
        sub = await session.scalar(
            sa_select(Subscription).where(Subscription.user_id == user.id)
        )
        if not sub or not sub.payment_method_id:
            await callback.answer(
                'Карта не сохранена. Оплати подписку заново — '
                'YooKassa спросит про автоплатежи.',
                show_alert=True,
            )
            return
        await session.execute(
            sa_update(Subscription)
            .where(Subscription.user_id == user.id)
            .values(auto_renew=True, renewal_failures=0)
        )
        await session.commit()
    await callback.answer('Автопродление включено')
    text, kb = await _build_subscription_view(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode='HTML', reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith('choose_plan:'))
async def cb_choose_plan(callback: CallbackQuery) -> None:
    plan_id = callback.data.split(':', 1)[1]
    plan_dict = get_subscription_plan(plan_id)
    if not plan_dict:
        await callback.answer('План не найден.', show_alert=True)
        return

    if not settings.yookassa_enabled:
        await callback.answer(
            'Оплата временно недоступна. Попробуй позже.',
            show_alert=True,
        )
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

            plan_enum = Plan(plan_dict['plan_enum'])
            db_payment = await create_payment(
                session,
                user_id=user.id,
                amount=Decimal(plan_dict['rub']),
                currency='RUB',
                payment_method='yookassa_sub',
                plan=plan_enum,
                extra={
                    'plan_id': plan_dict['id'],
                    'sub_plan': plan_dict['plan_enum'],
                    'credits': plan_dict['credits_monthly'],
                    'plan_name': plan_dict['name'],
                    'user_tg_id': callback.from_user.id,
                },
            )
            await session.commit()
            payment_db_id = db_payment.id

        # Рекуррент: save_payment_method=True работает ТОЛЬКО если YooKassa подключила
        # магазину «автоматические платежи» (отдельный запрос менеджеру). Если нет —
        # API вернёт 403. Поэтому читаем флаг из bot_config: yookassa_recurring_enabled.
        # Когда включишь функцию в YooKassa-кабинете — поставь '1' в /admin/pricing.
        from app.database.repo.bot_config import get_config_value
        async with AsyncSessionFactory() as cfg_session:
            recurring_flag = (await get_config_value(cfg_session, 'yookassa_recurring_enabled') or '0').strip()
            recurring_enabled = recurring_flag in ('1', 'true', 'True', 'yes')

        payload = {
            'amount': {'value': str(plan_dict['rub']), 'currency': 'RUB'},
            'confirmation': {'type': 'redirect', 'return_url': settings.yookassa_return_url},
            'capture': True,
            'description': (
                f"Нейра: подписка {plan_dict['name']} на 30 дней"
                + (' (автопродление)' if recurring_enabled else '')
            ),
            'metadata': {
                'payment_db_id': str(payment_db_id),
                'kind': 'subscription',
                'plan_id': plan_dict['id'],
                'user_id': str(user.id),
            },
        }
        if recurring_enabled:
            payload['save_payment_method'] = True
        yk_payment = YKPayment.create(payload, str(uuid.uuid4()))

        async with AsyncSessionFactory() as session:
            await update_payment(session, payment_db_id, PaymentStatus.PENDING, yk_payment.id)
            await session.commit()

        pay_url = yk_payment.confirmation.confirmation_url

        # Текст про автопродление показываем ТОЛЬКО если включён recurring флаг.
        # Иначе вводит юзера в заблуждение (платёж без save_payment_method).
        recurring_line = (
            f'🔁 <b>Автопродление включено</b> — следующее списание через 30 дней. '
            f'Отменить можно в любой момент в <code>/sub</code>.\n\n'
            if recurring_enabled else ''
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton.model_construct(
                text='Перейти к оплате', url=pay_url,
                icon_custom_emoji_id=PE_CARD, style='success',
            )],
            [InlineKeyboardButton.model_construct(
                text='Назад', callback_data='subscription',
                icon_custom_emoji_id=PE_BACK, style='danger',
            )],
        ])
        # 24.05: было answer (создавало 3-е сообщение). Теперь edit —
        # оплата живёт в том же экране что и выбор плана.
        from app.utils import safe_edit_or_answer
        await safe_edit_or_answer(
            callback,
            f"💳 <b>Подписка {plan_dict['name']} — {plan_dict['rub']}₽/мес</b>\n\n"
            f"После оплаты:\n"
            f"• <b>{plan_dict['credits_monthly']:,} кредитов</b> сразу\n"
            f"• <b>+{plan_dict['daily_bonus']} кр</b> ежедневно 30 дней\n\n"
            f"{recurring_line}"
            f"⏱ <i>Зачисление автоматически в течение 1 минуты после оплаты.</i>\n\n"
            f"💡 <i>Если страница оплаты не открывается — выключи VPN "
            f"и нажми кнопку снова.</i>\n\n"
            f"Жми кнопку ниже 👇",
            reply_markup=kb,
        )

    except Exception as e:
        logger.error('YooKassa subscription payment creation failed', error=e, exc_info=True)
        await callback.message.answer('❌ Ошибка создания платежа. Попробуй позже.')

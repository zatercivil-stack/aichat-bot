"""Авто-продление YooKassa-подписок.

Запускается шедулером каждый день в 03:00 МСК (admin.py). Логика:
  1. Pre-charge уведомление за 2 дня до списания (один раз, по renewal_reminded_at).
  2. В день истечения — попытка списания через saved payment_method_id.
  3. На успех — extend_subscription + начисление кредитов + уведомление юзеру.
  4. На fail — increment renewal_failures, retry на следующий день. После 3 фейлов
     auto_renew=FALSE + уведомление юзеру.

Не обрабатывает legacy-подписки без payment_method_id (они работают как раньше —
истекают и юзер должен покупать заново).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.database.base import AsyncSessionFactory
from app.database.models import (
    PaymentStatus,
    Plan,
    Subscription,
    User,
)
from app.database.repo.credits import (
    SUBSCRIPTION_PLANS,
)
from app.database.repo.payments import create_payment, update_payment
from app.keyboards.main import PE_AU_GOLD, PE_CALENDAR, PE_CARD, PE_HOME

logger = structlog.get_logger(__name__)

# Сколько дней до истечения шлём pre-charge напоминание
REMIND_DAYS_BEFORE = 2
# Сколько фейлов подряд — выключить автопродление
MAX_RENEWAL_FAILURES = 3


def _plan_by_enum(plan_enum: str) -> dict | None:
    return next(
        (p for p in SUBSCRIPTION_PLANS if p["plan_enum"] == plan_enum),
        None,
    )


async def _send_pre_charge_notice(
    bot, user_tg_id: int, plan_name: str, amount_rub: float, expires_at: datetime
) -> None:
    """Pre-charge уведомление — нейтральный информационный тон, без CTA на отмену.
    Цель: соблюсти этическое требование «предупредить заранее», но не подталкивать к
    cancellation. Отмена доступна в /sub, кнопкой — отдельно от этого нотифика."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            # Primary: остаться (нейтрально для лояльных). Управление автопродлением — через /sub.
            [
                InlineKeyboardButton.model_construct(
                    text="Открыть меню подписок",
                    callback_data="subscription",
                    icon_custom_emoji_id=PE_CALENDAR,
                )
            ],
        ]
    )
    days = max(1, (expires_at - datetime.now(UTC)).days)
    await bot.send_message(
        user_tg_id,
        f"🔁 <b>Подписка {plan_name} автоматически продлится через {days} дн.</b>\n\n"
        f"Спишем <b>{amount_rub:.0f}₽</b> с карты, которую ты привязала при первом платеже. "
        f"После списания на счёт зачислятся кредиты и продлится срок действия.\n\n"
        f"<i>Управление автопродлением — в /sub.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _attempt_recurring_charge(
    sub: Subscription,
    user: User,
    plan_dict: dict,
) -> tuple[bool, str | None]:
    """Создаёт recurring-платёж через YooKassa с saved payment_method_id.
    Возвращает (success, payment_id_or_error)."""
    try:
        from yookassa import Configuration
        from yookassa import Payment as YKPayment

        from app.config import settings

        Configuration.account_id = settings.YOOKASSA_SHOP_ID
        Configuration.secret_key = settings.YOOKASSA_SECRET_KEY

        async with AsyncSessionFactory() as session:
            db_payment = await create_payment(
                session,
                user_id=user.id,
                amount=Decimal(plan_dict["rub"]),
                currency="RUB",
                payment_method="yookassa_recurring",
                plan=Plan(plan_dict["plan_enum"]),
                extra={
                    "plan_id": plan_dict["id"],
                    "sub_plan": plan_dict["plan_enum"],
                    "credits": plan_dict["credits_monthly"],
                    "plan_name": plan_dict["name"],
                    "user_tg_id": user.telegram_id,
                    "recurring": True,
                },
            )
            await session.commit()
            payment_db_id = db_payment.id

        yk_payment = YKPayment.create(
            {
                "amount": {"value": str(plan_dict["rub"]), "currency": "RUB"},
                "capture": True,
                "payment_method_id": sub.payment_method_id,
                "description": f"Нейра: автопродление {plan_dict['name']} на 30 дней",
                "metadata": {
                    "payment_db_id": str(payment_db_id),
                    "kind": "subscription",
                    "plan_id": plan_dict["id"],
                    "user_id": str(user.id),
                },
            },
            str(uuid.uuid4()),
        )

        async with AsyncSessionFactory() as session:
            if yk_payment.status == "succeeded":
                # Платёж прошёл сразу — webhook нас догонит и сделает extend_subscription.
                # Но на всякий случай тоже обновим payment_db статус.
                await update_payment(session, payment_db_id, PaymentStatus.PENDING, yk_payment.id)
                await session.commit()
                return True, yk_payment.id
            elif yk_payment.status == "pending":
                # Редко (например 3DS-проверка) — оставляем PENDING, webhook догонит
                await update_payment(session, payment_db_id, PaymentStatus.PENDING, yk_payment.id)
                await session.commit()
                return True, yk_payment.id
            else:
                # canceled / other
                await update_payment(session, payment_db_id, PaymentStatus.FAILED, yk_payment.id)
                await session.commit()
                return False, f"yk_status={yk_payment.status}"

    except Exception as exc:
        logger.error("recurring charge raised", user_id=user.id, error=str(exc)[:300])
        return False, str(exc)[:200]


async def run_subscription_renewals(bot) -> None:
    """Главная точка входа шедулера. Запускается раз в день."""
    now = datetime.now(UTC)
    remind_cutoff = now + timedelta(days=REMIND_DAYS_BEFORE)

    notified = renewed = failed = disabled = 0

    async with AsyncSessionFactory() as session:
        # Все подписки с автопродлением, истекающие в ближайшие REMIND_DAYS_BEFORE дней
        result = await session.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.auto_renew,
                Subscription.payment_method_id.isnot(None),
                Subscription.is_active,
                Subscription.expires_at.isnot(None),
                Subscription.expires_at <= remind_cutoff,
                not User.is_banned,
            )
        )
        rows = result.all()

    for sub, user in rows:
        plan_val = sub.plan.value if hasattr(sub.plan, "value") else str(sub.plan)
        plan_dict = _plan_by_enum(plan_val)
        if not plan_dict:
            logger.warning("renewal: unknown plan", user_id=user.id, plan=plan_val)
            continue

        # Если подписка ещё не вот-вот истекает — отправляем pre-charge уведомление
        time_to_expire = sub.expires_at - now if sub.expires_at else timedelta(days=999)
        if time_to_expire > timedelta(days=1):
            # Pre-charge: только если ещё не уведомляли в этом цикле
            if not sub.renewal_reminded_at or sub.renewal_reminded_at < now - timedelta(days=10):
                try:
                    await _send_pre_charge_notice(
                        bot,
                        user.telegram_id,
                        plan_dict["name"],
                        float(plan_dict["rub"]),
                        sub.expires_at,
                    )
                    async with AsyncSessionFactory() as session2:
                        await session2.execute(
                            sa_update(Subscription)
                            .where(Subscription.id == sub.id)
                            .values(renewal_reminded_at=now)
                        )
                        await session2.commit()
                    notified += 1
                except Exception as exc:
                    logger.warning(
                        "renewal: pre-charge notify failed", user_id=user.id, error=str(exc)[:200]
                    )
            continue

        # Истечение в течение 24 часов — пытаемся списать
        success, info = await _attempt_recurring_charge(sub, user, plan_dict)
        async with AsyncSessionFactory() as session2:
            if success:
                await session2.execute(
                    sa_update(Subscription)
                    .where(Subscription.id == sub.id)
                    .values(last_renewal_attempt_at=now, renewal_failures=0)
                )
                await session2.commit()
                renewed += 1
                logger.info("subscription renewed", user_id=user.id, yk_id=info)
                # Уведомление о начислении придёт через webhook
            else:
                new_failures = (sub.renewal_failures or 0) + 1
                disable_now = new_failures >= MAX_RENEWAL_FAILURES
                await session2.execute(
                    sa_update(Subscription)
                    .where(Subscription.id == sub.id)
                    .values(
                        last_renewal_attempt_at=now,
                        renewal_failures=new_failures,
                        auto_renew=not disable_now,
                    )
                )
                await session2.commit()
                failed += 1
                if disable_now:
                    disabled += 1
                # Сообщаем юзеру об отказе
                try:
                    await _send_failure_notice(
                        bot,
                        user.telegram_id,
                        plan_dict["name"],
                        new_failures,
                        disable_now,
                        info,
                    )
                except Exception as exc:
                    logger.warning(
                        "renewal failure notify failed", user_id=user.id, error=str(exc)[:200]
                    )
                logger.warning("renewal failed", user_id=user.id, failures=new_failures, info=info)

    if notified or renewed or failed:
        logger.info(
            "subscription renewals done",
            notified=notified,
            renewed=renewed,
            failed=failed,
            auto_disabled=disabled,
        )


async def _send_failure_notice(
    bot, user_tg_id: int, plan_name: str, failures: int, disabled_now: bool, reason: str | None
) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if disabled_now:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton.model_construct(
                        text="Оформить подписку заново",
                        callback_data="subscription",
                        icon_custom_emoji_id=PE_AU_GOLD,
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton.model_construct(
                        text="В главное меню",
                        callback_data="main_menu",
                        icon_custom_emoji_id=PE_HOME,
                        style="danger",
                    )
                ],
            ]
        )
        text = (
            f"❌ <b>Автопродление подписки {plan_name} отключено</b>\n\n"
            f"Не удалось списать оплату {failures} раз подряд — "
            f"возможно, истёк срок карты или недостаточно средств.\n\n"
            f"Подписка осталась активна до окончания периода. Чтобы продлить — "
            f"оформи заново."
        )
    else:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton.model_construct(
                        text="Проверить карту в YooKassa",
                        url="https://yookassa.ru/my/",
                        icon_custom_emoji_id=PE_CARD,
                    )
                ],
                [
                    InlineKeyboardButton.model_construct(
                        text="В главное меню",
                        callback_data="main_menu",
                        icon_custom_emoji_id=PE_HOME,
                        style="danger",
                    )
                ],
            ]
        )
        text = (
            f"⚠️ <b>Не удалось списать за подписку {plan_name}</b>\n\n"
            f"Попытка {failures}/3. Повторим завтра.\n\n"
            f"Возможные причины: недостаточно средств, лимит на покупки, "
            f"истёк срок карты. Проверь карту в YooKassa."
        )
    await bot.send_message(user_tg_id, text, parse_mode="HTML", reply_markup=kb)

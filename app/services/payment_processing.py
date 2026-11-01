"""Единая транзакция активации платежа для webhook, поллинга и админ-команды.

Платёж блокируется для защиты от повторной обработки, пользователь — для
сериализации разных покупок и изменений баланса. Уведомления отправляются
после commit. Без сохранённого payment_method подписка работает без автопродления.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import update as sa_update

from app.database.base import AsyncSessionFactory
from app.database.models import Payment, PaymentStatus, Subscription, User
from app.database.repo.credits import SUBSCRIPTION_PLANS, add_credits
from app.database.repo.payments import update_payment
from app.database.repo.subscriptions import extend_subscription

logger = structlog.get_logger(__name__)


async def process_succeeded_payment(
    payment_db_id: int,
    external_id: str | None = None,
    pm_payload: dict[str, Any] | None = None,
    bot=None,
) -> bool:
    """Активирует SUCCEEDED-платёж: зачисляет кредиты, активирует подписку,
    шлёт уведомление, помечает payment как SUCCEEDED.

    Идемпотентна: если payment уже SUCCEEDED — возвращает True без повторов.

    Args:
        payment_db_id: id из таблицы payments
        external_id:   YooKassa payment_id (опционально, для записи)
        pm_payload:    payment_method объект из webhook (для recurring card).
                       None если активация через checker — auto_renew не включится.
        bot:           aiogram Bot для отправки уведомления (опционально)

    Returns: True если активация прошла или уже была, False при ошибке.
    """
    async with AsyncSessionFactory() as session:
        # 24.05: SELECT FOR UPDATE — блокируем строку платежа на время
        # обработки. Без лока два одновременных вызова (webhook + поллер-
        # checker) могли оба прочитать PENDING и оба начислить кредиты до
        # коммита SUCCEEDED → двойное начисление. Теперь второй ждёт первого
        # и видит уже SUCCEEDED → выходит идемпотентно.
        from sqlalchemy import select as _sel

        payment = await session.scalar(
            _sel(Payment)
            .where(Payment.id == payment_db_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not payment:
            logger.warning("process_payment: not found", payment_db_id=payment_db_id)
            return False

        if payment.status == PaymentStatus.SUCCEEDED:
            logger.info("process_payment: already succeeded", payment_db_id=payment_db_id)
            return True

        credits = payment.extra.get("credits", 0)
        user_tg_id = payment.extra.get("user_tg_id")
        kind = payment.extra.get("kind") or (
            "subscription" if payment.payment_method == "yookassa_sub" else "pack"
        )

        if not credits or not user_tg_id:
            logger.error(
                "process_payment: missing credits/user_tg_id",
                payment_db_id=payment_db_id,
                extra=payment.extra,
            )
            return False

        user = await session.scalar(
            _sel(User)
            .where(User.id == payment.user_id, User.telegram_id == int(user_tg_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not user:
            logger.error(
                "process_payment: user not found",
                payment_db_id=payment_db_id,
                user_tg_id=user_tg_id,
            )
            return False

        # 1. Кредиты (burn для пака с expires_days, eternal иначе/для подписки)
        expires_days = payment.extra.get("expires_days")
        burn_until = None
        if expires_days:
            try:
                burn_until = datetime.now(UTC) + timedelta(days=int(expires_days))
            except (ValueError, TypeError):
                burn_until = None
        await add_credits(session, user, credits, burn_expires_at=burn_until)

        # 2. Subscription активация (только для subscription-платежей)
        sub_notice = None
        if kind == "subscription":
            sub_plan = payment.extra.get("sub_plan")
            plan_name = payment.extra.get("plan_name", sub_plan or "PRO")
            try:
                subscription = await extend_subscription(session, user.id, sub_plan, days=30)
                # Recurring card — только если webhook передал pm_payload с saved=True
                pm_id = None
                pm_saved = False
                if pm_payload:
                    pm_id = pm_payload.get("id")
                    pm_saved = pm_payload.get("saved", False)
                if pm_id and pm_saved:
                    await session.execute(
                        sa_update(Subscription)
                        .where(Subscription.user_id == user.id)
                        .values(
                            payment_method_id=pm_id,
                            auto_renew=True,
                            renewal_failures=0,
                            last_renewal_attempt_at=None,
                            renewal_reminded_at=None,
                        )
                    )
                    logger.info("process_payment: pm saved", user_id=user.id, pm_id=pm_id[:12])

                expires_str = subscription.expires_at.strftime("%d.%m.%Y")
                plan_dict = next(
                    (p for p in SUBSCRIPTION_PLANS if p["plan_enum"] == sub_plan),
                    None,
                )
                daily_bonus = plan_dict["daily_bonus"] if plan_dict else 30
                auto_renew_note = (
                    f"\n\n🔁 Автопродление включено — следующее списание {expires_str}. "
                    f"Отключить: <code>/sub</code>."
                    if (pm_id and pm_saved)
                    else ""
                )
                sub_notice = (
                    f"🎉 <b>Подписка {plan_name} активна!</b>\n\n"
                    f"+<b>{credits:,} кредитов</b> на счёт.\n"
                    f"Действует до: <b>{expires_str}</b>\n"
                    f"Ежедневный бонус: <b>+{daily_bonus} кр</b>" + auto_renew_note
                )
            except Exception as exc:
                logger.error(
                    "process_payment: subscription activation failed",
                    payment_db_id=payment_db_id,
                    error=str(exc)[:200],
                )
                await session.rollback()
                return False

        # Реф-кэшбэк (вариант B): фиксируем ДО пометки SUCCEEDED — был ли это
        # ПЕРВЫЙ платёж юзера (тогда его рефереру капнёт кэшбэк) и сколько своего
        # кэшбэка юзер потратил на эту покупку (спишем после commit).
        from sqlalchemy import func as _func
        from sqlalchemy import select as _sel

        from app.database.models import Payment as _Pay

        _prior_succeeded = (
            await session.scalar(
                _sel(_func.count())
                .select_from(_Pay)
                .where(_Pay.user_id == user.id, _Pay.status == PaymentStatus.SUCCEEDED)
            )
            or 0
        )
        _rc_referrer_tg = user.referrer_id
        _rc_cashback_used = payment.extra.get("cashback_used", 0)
        _rc_paid_amount = float(payment.amount)

        # 3. Помечаем платёж SUCCEEDED (после кредитов и sub — чтобы при сбое
        # на предыдущих шагах при следующем поллинге был ретрай)
        await update_payment(
            session,
            payment.id,
            PaymentStatus.SUCCEEDED,
            external_id=external_id or payment.external_id,
        )
        await session.commit()

    # 4. Уведомление юзеру (после commit — даже если упадёт, активация уже в БД)
    notice = sub_notice
    if not notice and kind != "subscription":
        if burn_until:
            days = max(1, (burn_until - datetime.now(UTC)).days)
            exp_str = burn_until.strftime("%d.%m.%Y")
            notice = (
                f"🎉 <b>Оплачено! +{credits:,} срочных кредитов на счёт</b>\n\n"
                f"🔥 <b>Срок:</b> {days} дней (до {exp_str})\n"
                f"<i>Эти кредиты тратятся первыми. После окончания срока "
                f"остаток сгорит — успей использовать на сцены и стилизации.</i>"
                f"\n\n📎 Жми /edit и пришли селфи."
            )
        else:
            notice = (
                f"🎉 <b>Оплачено! +{credits:,} кредитов на счёт</b>\n\n"
                f"♾ <b>Без срока</b> — лежат вечно, тратятся когда нужно.\n"
                f"✓ <b>30 дней без водяного знака</b> на всех фото.\n\n"
                f"📎 Жми /edit и пришли селфи."
            )

    if notice:
        try:
            bot_instance = bot
            if bot_instance is None:
                from app.bot import get_bot

                bot_instance = get_bot()
            if bot_instance:
                await bot_instance.send_message(int(user_tg_id), notice, parse_mode="HTML")
        except Exception as exc:
            logger.error(
                "process_payment: notification failed", user_tg_id=user_tg_id, error=str(exc)[:200]
            )

    # 4b. Реф-кэшбэк (вариант B) — после commit, best-effort, не валит активацию.
    try:
        from app.services import referral_cashback as _rc

        # (a) юзер потратил свой кэшбэк на эту покупку → списываем баланс
        if _rc_cashback_used and int(_rc_cashback_used) > 0:
            await _rc.redeem(int(user_tg_id), int(_rc_cashback_used))
        # (b) ПЕРВЫЙ платёж приглашённого → рефереру 30% как скидка + пуш
        if _prior_succeeded == 0 and _rc_referrer_tg:
            granted = await _rc.grant(int(_rc_referrer_tg), _rc_paid_amount)
            if granted > 0:
                try:
                    bot_instance = bot
                    if bot_instance is None:
                        from app.bot import get_bot

                        bot_instance = get_bot()
                    if bot_instance:
                        from app.utils import premium_text

                        await bot_instance.send_message(
                            int(_rc_referrer_tg),
                            premium_text(
                                f"🎉 <b>Друг оформил покупку — тебе скидка {granted}₽!</b>\n\n"
                                f"Скидка на покупку кредитов (паки), действует 14 дней.\n"
                                f"Жми <b>/shop</b> — применится при оплате картой."
                            ),
                            parse_mode="HTML",
                        )
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("referral cashback hook failed", error=str(exc)[:150])

    # 5. Admin alert + experiment conversion (best-effort)
    try:
        from app.services.admin_notify import alert_big_payment

        await alert_big_payment(
            user_id=user.id,
            telegram_id=int(user_tg_id),
            username=user.username,
            amount=float(payment.amount),
            currency=payment.currency,
            method=payment.payment_method,
        )
    except Exception as exc:
        logger.warning("process_payment: admin alert failed", error=str(exc)[:200])

    try:
        from app.database.repo.experiments import mark_conversion

        await mark_conversion(user.id, "first_payment")
    except Exception as exc:
        logger.warning("process_payment: experiment mark failed", error=str(exc)[:200])

    logger.info(
        "process_payment: completed",
        payment_db_id=payment_db_id,
        user_tg_id=user_tg_id,
        credits=credits,
        kind=kind,
        source="helper",
    )
    return True

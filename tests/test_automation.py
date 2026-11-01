"""Business scenarios must select eligible users and exclude ineligible ones."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app.database.base import AsyncSessionFactory
from app.database.models import PaymentStatus, Plan, PromoCode, Subscription, User
from app.database.repo.payments import create_payment
from app.database.repo.promos import get_promo
from app.database.repo.subscriptions import count_by_plan
from app.database.repo.users import get_all_telegram_ids
from app.services import payment_recovery, subscription_renewal, trial_conversion


@pytest.mark.parametrize("banned", [False, True])
async def test_broadcast_audience_excludes_banned(db_user, banned):
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        user.is_banned = banned
        await session.commit()
        audience = await get_all_telegram_ids(session)
    assert (db_user.telegram_id in audience) is not banned


@pytest.mark.parametrize(
    "case,expected",
    [
        ("eligible", True),
        ("banned", False),
        ("already_sent", False),
        ("recent", False),
        ("paid", False),
    ],
)
async def test_trial_followup_targets_only_eligible(db_user, case, expected):
    now = datetime.now(UTC)
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        user.trial_ended_at = now - timedelta(days=2)
        user.last_active_at = now - timedelta(days=1)
        user.is_banned = case == "banned"
        user.trial_end_push_sent = case == "already_sent"
        if case == "recent":
            user.trial_ended_at = now - timedelta(hours=1)
        if case == "paid":
            payment = await create_payment(
                session, user.id, Decimal("99"), "RUB", "test", Plan.FREE
            )
            payment.status = PaymentStatus.SUCCEEDED
        await session.commit()
    bot = AsyncMock()
    await trial_conversion.run_trial_end_push(bot)
    assert bot.send_message.await_count == int(expected)
    if expected:
        # A second scheduler tick must not repeat the notification.
        await trial_conversion.run_trial_end_push(bot)
        bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("banned", [False, True])
async def test_payment_recovery_selection_and_cooldown(db_user, banned):
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        user.is_banned = banned
        for _ in range(2):
            payment = await create_payment(
                session, user.id, Decimal("99"), "RUB", "test", Plan.FREE
            )
            payment.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    bot = AsyncMock()
    await payment_recovery.run_payment_recovery(bot)
    await payment_recovery.run_payment_recovery(bot)
    assert bot.send_message.await_count == int(not banned)


@pytest.mark.parametrize("banned", [False, True])
async def test_subscription_scheduler_selects_due_users(db_user, banned, monkeypatch):
    notice = AsyncMock()
    charge = AsyncMock()
    monkeypatch.setattr(subscription_renewal, "_send_pre_charge_notice", notice)
    monkeypatch.setattr(subscription_renewal, "_attempt_recurring_charge", charge)
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        user.is_banned = banned
        session.add(
            Subscription(
                user_id=user.id,
                plan=Plan.BASIC,
                auto_renew=True,
                payment_method_id="test_saved_method",
                expires_at=datetime.now(UTC) + timedelta(hours=36),
            )
        )
        await session.commit()
    await subscription_renewal.run_subscription_renewals(AsyncMock())
    assert notice.await_count == int(not banned)
    charge.assert_not_awaited()


async def test_nonexpiring_subscriptions_are_counted(db_user):
    async with AsyncSessionFactory() as session:
        session.add(Subscription(user_id=db_user.id, plan=Plan.BASIC, expires_at=None))
        await session.commit()
        assert (await count_by_plan(session))["basic"] == 1


@pytest.mark.parametrize("days,available", [(None, True), (1, True), (-1, False)])
async def test_promos_with_optional_expiry(db_user, days, available):
    code = f"TEST_{db_user.telegram_id}"
    async with AsyncSessionFactory() as session:
        session.add(
            PromoCode(
                code=code,
                credits=100,
                expires_at=None if days is None else datetime.now(UTC) + timedelta(days=days),
            )
        )
        await session.commit()
        try:
            assert (await get_promo(session, code) is not None) == available
        finally:
            await session.execute(delete(PromoCode).where(PromoCode.code == code))
            await session.commit()

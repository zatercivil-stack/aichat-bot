"""Credit accounting and payment regression tests against real PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.base import AsyncSessionFactory
from app.database.models import Payment, PaymentStatus, Plan, Subscription, User
from app.database.repo.credits import add_credits, deduct_credits
from app.database.repo.payments import create_payment
from app.services.payment_processing import process_succeeded_payment


async def balance(user_id):
    async with AsyncSessionFactory() as session:
        user = await session.get(User, user_id)
        return user.credits, user.burn_credits


async def test_concurrent_credits_reload_stale_balances(db_user):
    barrier = asyncio.Barrier(2)

    async def credit(amount):
        async with AsyncSessionFactory() as session:
            user = await session.get(User, db_user.id)
            await barrier.wait()  # Both sessions have read the same old balance.
            await add_credits(session, user, amount)
            await session.commit()

    await asyncio.wait_for(asyncio.gather(credit(200), credit(300)), timeout=10)
    assert await balance(db_user.id) == (600, 0)


async def test_concurrent_spending_never_overdraws(db_user):
    barrier = asyncio.Barrier(2)

    async def spend():
        async with AsyncSessionFactory() as session:
            user = await session.get(User, db_user.id)
            await barrier.wait()
            ok = await deduct_credits(session, user, 80)
            await session.commit()
            return ok

    results = await asyncio.wait_for(asyncio.gather(spend(), spend()), timeout=10)
    assert sorted(results) == [False, True]
    assert await balance(db_user.id) == (20, 0)


async def test_burn_credits_are_spent_first(db_user):
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        await add_credits(session, user, 50, datetime.now(UTC) + timedelta(days=7))
        assert await deduct_credits(session, user, 70)
        await session.commit()
    assert await balance(db_user.id) == (80, 0)


async def test_expired_burn_is_not_spendable(db_user):
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        user.burn_credits = 500
        user.burn_expires_at = datetime.now(UTC) - timedelta(days=1)
        assert not await deduct_credits(session, user, 101)
        await session.commit()
    assert await balance(db_user.id) == (100, 0)


@pytest.mark.parametrize("operation", [add_credits, deduct_credits])
async def test_negative_amount_rejected(db_user, operation):
    async with AsyncSessionFactory() as session:
        user = await session.get(User, db_user.id)
        with pytest.raises(ValueError):
            await operation(session, user, -10)
    assert await balance(db_user.id) == (100, 0)


@pytest.fixture
def payment_effects(monkeypatch):
    # No Telegram/API calls; the accounting transaction remains real.
    monkeypatch.setattr("app.services.admin_notify.alert_big_payment", AsyncMock())
    monkeypatch.setattr("app.database.repo.experiments.mark_conversion", AsyncMock())
    monkeypatch.setattr("app.services.referral_cashback.grant", AsyncMock(return_value=0))
    return AsyncMock()


async def make_payment(user, credits=200, **extra):
    async with AsyncSessionFactory() as session:
        payment = await create_payment(
            session,
            user.id,
            Decimal("290.00"),
            "RUB",
            "yookassa",
            Plan.BASIC,
            extra={"credits": credits, "user_tg_id": user.telegram_id, **extra},
        )
        await session.commit()
        return payment.id


async def test_duplicate_payment_is_applied_once(db_user, payment_effects):
    payment_id = await make_payment(db_user)
    results = await asyncio.wait_for(
        asyncio.gather(
            *(process_succeeded_payment(payment_id, bot=payment_effects) for _ in range(3))
        ),
        timeout=10,
    )
    assert all(results)
    assert await balance(db_user.id) == (300, 0)
    payment_effects.send_message.assert_awaited_once()
    async with AsyncSessionFactory() as session:
        assert (await session.get(Payment, payment_id)).status == PaymentStatus.SUCCEEDED


async def test_distinct_payments_for_same_user_both_apply(db_user, payment_effects):
    first = await make_payment(db_user, 200)
    second = await make_payment(db_user, 300)
    results = await asyncio.wait_for(
        asyncio.gather(
            process_succeeded_payment(first, bot=payment_effects),
            process_succeeded_payment(second, bot=payment_effects),
        ),
        timeout=10,
    )
    assert all(results)
    assert await balance(db_user.id) == (600, 0)


async def test_subscription_failure_rolls_back_credits(db_user, payment_effects):
    payment_id = await make_payment(db_user, kind="subscription", sub_plan="invalid")
    assert not await process_succeeded_payment(payment_id, bot=payment_effects)
    assert await balance(db_user.id) == (100, 0)
    async with AsyncSessionFactory() as session:
        assert (await session.get(Payment, payment_id)).status == PaymentStatus.PENDING


async def test_subscription_notice_uses_extended_expiry(db_user, payment_effects):
    expiry = datetime.now(UTC) + timedelta(days=20)
    async with AsyncSessionFactory() as session:
        session.add(Subscription(user_id=db_user.id, plan=Plan.BASIC, expires_at=expiry))
        await session.commit()
    payment_id = await make_payment(db_user, kind="subscription", sub_plan="basic")
    assert await process_succeeded_payment(payment_id, bot=payment_effects)
    async with AsyncSessionFactory() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == db_user.id))
        assert sub.expires_at == expiry + timedelta(days=30)
    assert (expiry + timedelta(days=30)).strftime(
        "%d.%m.%Y"
    ) in payment_effects.send_message.call_args.args[1]


async def test_legacy_stars_repeated_charge_is_applied_once(db_user, payment_effects):
    from types import SimpleNamespace

    from app.handlers.payments import _handle_credits_payment

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=db_user.telegram_id),
        bot=payment_effects,
        answer=AsyncMock(),
    )
    await asyncio.wait_for(
        asyncio.gather(
            *(
                _handle_credits_payment(message, 200, 10, f"test-charge-{db_user.id}")
                for _ in range(2)
            )
        ),
        timeout=10,
    )
    assert await balance(db_user.id) == (300, 0)
    payment_effects.send_message.assert_awaited_once()


async def test_manual_recovery_paths_share_idempotent_activation(
    db_user, payment_effects, monkeypatch
):
    from types import SimpleNamespace

    from app.config import settings
    from app.handlers import admin, shop

    monkeypatch.setattr(settings, "ADMIN_IDS", str(db_user.telegram_id))
    await make_payment(db_user)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=db_user.telegram_id),
        bot=payment_effects,
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=message.from_user,
        message=message,
        bot=payment_effects,
        answer=AsyncMock(),
    )
    await asyncio.wait_for(
        asyncio.gather(
            shop.cb_complete_pending(callback),
            admin.cmd_fix_payments(message),
        ),
        timeout=10,
    )
    assert await balance(db_user.id) == (300, 0)
    payment_effects.send_message.assert_awaited_once()

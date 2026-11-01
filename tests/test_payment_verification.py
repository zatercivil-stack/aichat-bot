"""Provider verification uses server data, not webhook claims; no live API calls."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Plan
from app.database.repo.payments import create_payment
from app.services.payment_verify import verify_yookassa_payment


@pytest.mark.parametrize(
    "case,expected",
    [
        ("valid", True),
        ("pending", False),
        ("wrong_metadata", False),
        ("wrong_amount", False),
        ("wrong_currency", False),
        ("missing_currency", False),
        ("invalid_amount", False),
        ("api_error", False),
        ("disabled", False),
    ],
)
async def test_provider_verification_rejects_mismatches(db_user, monkeypatch, case, expected):
    async with AsyncSessionFactory() as session:
        payment = await create_payment(
            session,
            db_user.id,
            Decimal("290"),
            "RUB",
            "yookassa",
            Plan.BASIC,
        )
        await session.commit()
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "" if case == "disabled" else "test-shop")
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "test-not-a-real-secret")
    remote = SimpleNamespace(
        status="pending" if case == "pending" else "succeeded",
        metadata={"payment_db_id": str(payment.id + (case == "wrong_metadata"))},
        amount=SimpleNamespace(
            value="bad" if case == "invalid_amount" else "291" if case == "wrong_amount" else "290",
            currency=None
            if case == "missing_currency"
            else "USD"
            if case == "wrong_currency"
            else "RUB",
        ),
    )
    lookup = Mock(return_value=remote)
    if case == "api_error":
        lookup.side_effect = ConnectionError("test provider unavailable")
    monkeypatch.setattr("yookassa.Payment.find_one", lookup)
    assert await verify_yookassa_payment("test-external-id", payment.id) is expected
    if case == "disabled":
        lookup.assert_not_called()

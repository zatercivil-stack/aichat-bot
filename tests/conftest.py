"""Tests require explicitly selected, disposable services (see compose.test.yml)."""

import os

import pytest
import pytest_asyncio

# Check before application imports: never let .env select a production database.
if os.environ.get("ENV") != "test" or not os.environ.get("POSTGRES_DB", "").endswith("_test"):
    raise pytest.UsageError(
        "Tests require ENV=test and an explicit POSTGRES_DB ending in _test. "
        "Use docker compose -f compose.test.yml run --build --rm test."
    )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def dispose_engine():
    from app.database.base import engine

    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db_user():
    """A committed user, visible to concurrent sessions, removed after each test."""
    import secrets

    from sqlalchemy import delete

    from app.database.base import AsyncSessionFactory
    from app.database.models import BotEvent, Payment, Subscription, User

    async with AsyncSessionFactory() as session:
        user = User(telegram_id=secrets.randbelow(10**12) + 10**12, credits=100)
        session.add(user)
        await session.commit()
    try:
        yield user
    finally:
        async with AsyncSessionFactory() as session:
            for model in (BotEvent, Payment, Subscription):
                await session.execute(delete(model).where(model.user_id == user.id))
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()

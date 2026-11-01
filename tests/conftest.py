import asyncio

import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Единый event loop на всю сессию.

    Иначе async-движок SQLAlchemy (пул соединений asyncpg, созданный при импорте
    app.database.base) живёт дольше per-test loop'а, и на teardown следующего
    теста получаем 'RuntimeError: Event loop is closed'. Один loop на сессию —
    пул остаётся валидным для всех интеграционных тестов.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

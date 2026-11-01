from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

import structlog
import uvicorn
from redis.asyncio import Redis

from app.bot import create_bot, create_dispatcher
from app.config import settings
from app.database.base import AsyncSessionFactory, engine
from app.handlers.admin import (
    ensure_default_promos,
    start_daily_push_scheduler,
    start_payment_checker,
    stop_daily_push_scheduler,
    stop_payment_checker,
)
from app.webserver.webhook import app as fastapi_app

# ── Logging ────────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

logger = structlog.get_logger(__name__)


def run_migrations() -> None:
    logger.info("Running alembic migrations...")
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Migration failed", stdout=result.stdout, stderr=result.stderr)
        sys.exit(1)
    logger.info("Migrations complete", output=result.stdout.strip())


async def start_webhook_server() -> None:
    config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=settings.WEBHOOK_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    run_migrations()

    # Redis
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)

    # Bot + Dispatcher
    bot = create_bot()
    dp = create_dispatcher(redis)

    me = await bot.get_me()
    logger.info("Bot started", username=me.username, id=me.id)

    # Меню команд в Telegram (кнопка «Меню» / список после /). Порядок = funnel:
    # visual-first → главная фишка фото, остальное вторично.
    from aiogram.types import BotCommand

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="edit", description="Сделать фото со своим лицом"),
                BotCommand(command="gallery", description="Каталог настроений — листай примеры"),
                BotCommand(command="faceswap", description="Подставить лицо в чужое фото"),
                BotCommand(command="img", description="Нарисовать картинку с нуля"),
                BotCommand(command="status", description="Мой баланс"),
                BotCommand(command="sub", description="Подписка и тарифы"),
                BotCommand(command="promo", description="Активировать промокод"),
                BotCommand(command="ref", description="Пригласить друга и заработать кр"),
                BotCommand(command="help", description="Как пользоваться"),
            ]
        )
        logger.info("Bot commands menu updated")
    except Exception as exc:
        logger.warning("set_my_commands failed", error=str(exc)[:200])

    # Описание бота (видно в карточке @NeyraChatBot и при первом /start у новичка)
    # Visual-first: одно обещание, без AI-помощника-универсала.
    # ВАЖНО: ставим описание на default И на каждый язык явно — иначе TG-клиент
    # с русской локалью показывает старое ru-описание которое переопределяет
    # default (нашли что было сохранено старое «AI-помощник, тексты/посты/...»).
    # Лид от 1-го лица: «ты в сцене» сильнее, чем «бот превратит». Сцены —
    # только свежие из новых категорий (дерзкие портреты / магия / стихии).
    # ВЕРСИЯ 02.06: убрано «13 000+» (накрутка, не бьётся с реальными ~цифрами и
    # рискованно); крючок «окажись где захочешь» + конкретные сцены. Без
    # иностранных слов (правило владельца). ⚠️ Этот текст — ИСТОЧНИК ПРАВДЫ:
    # ставится на КАЖДОМ старте, перетирает ручные правки через API. Менять тут.
    _LONG_DESC = (
        "🪄 Окажись где захочешь, не вставая с дивана.\n"
        "Неон ночного города, обложка глянца, кадр из кино, полночь в Париже — "
        "выбираешь сцену, а лицо в кадре остаётся точно твоим, не похожим.\n\n"
        "Пришли одно селфи → через минуту готовое фото. "
        "Десятки сцен, новые добавляются каждую неделю.\n\n"
        "🎁 500 кредитов в подарок — хватит на пару кадров. "
        "Плюс 15 бесплатных каждый день.\n\n"
        "Жми «Начать» и попробуй на себе 👇"
    )
    _SHORT_DESC = (
        "Пришли селфи — получи фото с собой в любой сцене за минуту. "
        "Лицо точно твоё. Старт бесплатно."
    )
    for lc in (None, "ru", "en"):
        try:
            await bot.set_my_description(description=_LONG_DESC, language_code=lc)
            await bot.set_my_short_description(short_description=_SHORT_DESC, language_code=lc)
        except Exception as exc:
            logger.warning("set_my_description failed", lc=lc, error=str(exc)[:200])
    logger.info("Bot description updated for default/ru/en")

    # Создаём промокоды по умолчанию
    await ensure_default_promos()

    # Загружаем дефолты для bot_config
    from app.database.repo.bot_config import ensure_defaults as ensure_bot_config

    async with AsyncSessionFactory() as session:
        await ensure_bot_config(session)
    logger.info("Bot config defaults ensured")

    # Активный токен админ-панели: если ранее ротировали через TG-кнопку —
    # подтянуть из bot_config (иначе сбросился бы на .env при рестарте).
    try:
        from app.webserver.admin import load_active_token

        await load_active_token()
    except Exception as exc:
        logger.warning("load_active_token at startup failed", error=str(exc)[:150])

    # Мёржим custom-пресеты из дашборда в рантайм-каталог (без правки кода).
    try:
        from app.handlers.image_edit import reload_custom_presets

        n_custom = await reload_custom_presets()
        logger.info("Custom presets loaded", count=n_custom)
    except Exception as exc:
        logger.warning("reload_custom_presets at startup failed", error=str(exc)[:200])

    # Запускаем планировщик проверки платежей
    start_payment_checker(bot)
    logger.info("Payment checker started")

    # Запускаем daily push для неактивных юзеров (10:00 МСК ежедневно)
    start_daily_push_scheduler(bot)

    # Запускаем webhook-сервер в фоне
    webhook_task = asyncio.create_task(start_webhook_server())
    logger.info("Webhook server started", port=settings.WEBHOOK_PORT)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        stop_payment_checker()
        stop_daily_push_scheduler()
        webhook_task.cancel()
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())

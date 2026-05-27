from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from app.config import settings
from app.handlers import admin, channel_admin, channel_traffic, chat, image, image_edit, payments, promo, shop, start, subscription, templates, vision, voice, wheel
from app.middlewares.command_reset import CommandResetStateMiddleware
from app.middlewares.rate_limit import RateLimitMiddleware
from app.middlewares.user import UserMiddleware


_bot_instance: Bot | None = None


def create_bot() -> Bot:
    global _bot_instance
    # Default aiogram timeout = 60s. Это мало для send_photo с большими
    # картинками (1-2 MB letterbox-PNG из gpt-5-image-mini) — апload
    # таких изображений в Telegram в ~5% случаев упирается в timeout.
    # Поднимаем до 180s — Telegram-MAX для media uploads.
    session = AiohttpSession(timeout=180)
    _bot_instance = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return _bot_instance


def get_bot() -> Bot | None:
    return _bot_instance


def create_dispatcher(redis: Redis) -> Dispatcher:
    storage = RedisStorage(redis=redis)
    dp = Dispatcher(storage=storage)

    dp.workflow_data.update({'redis': redis})

    # Middlewares (order: rate-limit first to drop abuse before any DB work)
    dp.message.outer_middleware(RateLimitMiddleware(lambda: redis))
    # /команды должны срабатывать как команды даже если юзер в FSM-state
    # (без сброса state ImageState.waiting_prompt поймал бы /start как промпт)
    dp.message.outer_middleware(CommandResetStateMiddleware())
    dp.update.outer_middleware(UserMiddleware())

    # Роутеры (порядок важен: payments перед subscription, start последний)
    dp.include_router(admin.router)
    dp.include_router(channel_admin.router)
    dp.include_router(payments.router)
    dp.include_router(shop.router)
    dp.include_router(subscription.router)
    dp.include_router(image.router)
    dp.include_router(image_edit.router)  # photo edit + face swap (FSM-aware) — до vision
    dp.include_router(wheel.router)  # колесо удачи (/spin + callback)
    dp.include_router(templates.router)
    dp.include_router(promo.router)
    dp.include_router(voice.router)   # voice (F.voice | F.audio) — до chat
    dp.include_router(vision.router)  # vision (F.photo) — до chat
    dp.include_router(chat.router)
    dp.include_router(start.router)

    # chat_member listener для @conturD (join/leave с invite-link атрибуцией).
    # Регистрация именно здесь — чтобы dp.resolve_used_update_types() авто-
    # добавил 'chat_member' в allowed_updates при start_polling.
    dp.include_router(channel_traffic.router)

    return dp

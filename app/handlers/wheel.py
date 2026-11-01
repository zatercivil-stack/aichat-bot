"""Колесо удачи — Этап 2 геймификации. Раз в сутки юзер крутит и выигрывает
кредиты. Главный «крючок» retention: повод зайти даже без конкретной цели.

Анимация — подмена сообщения (3 кадра «вращения» → результат). Приз контролируем
сами (взвешенный roll), поэтому экономика предсказуема: EV ≈ 26 кр/крутка.
Лимит 1 крутка / 24ч через Redis-лок (без миграций БД).
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import add_credits
from app.database.repo.events import log_event_bg
from app.database.repo.users import get_user
from app.keyboards.main import PE_HOME, PE_SPARKLES
from app.utils import premium_text

logger = structlog.get_logger(__name__)
router = Router()

# Призы и веса. EV = 0.60·10 + 0.25·25 + 0.10·50 + 0.04·100 + 0.01·500 ≈ 26 кр.
_PRIZES = (10, 25, 50, 100, 500)
_WEIGHTS = (60, 25, 10, 4, 1)
_COOLDOWN = 86400  # 24 часа
# ZSET для отложенных нотификаций «колесо готово». member=tg_id, score=unix_ts
# когда cooldown истечёт. Cron-job в handlers/admin.py каждые 30мин выбирает
# due (score<=now) и шлёт уведомление с CTA на повторную крутку.
WHEEL_READY_ZSET = "wheel:ready_at"


def _redis():
    from redis.asyncio import Redis

    from app.config import settings

    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _roll_prize() -> int:
    return random.choices(_PRIZES, weights=_WEIGHTS, k=1)[0]


async def _claim_or_ttl(redis, user_tg_id: int) -> int | None:
    """Атомарно занимает крутку на 24ч. None — можно крутить; иначе секунды до
    следующей крутки."""
    key = f"wheel:{user_tg_id}"
    ok = await redis.set(key, "1", nx=True, ex=_COOLDOWN)
    if ok:
        return None
    ttl = await redis.ttl(key)
    return ttl if (ttl and ttl > 0) else 1


def _fmt_left(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h >= 1:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def _result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text="Потратить на фото с моим лицом",
                    callback_data="gallery_open",
                    icon_custom_emoji_id=PE_SPARKLES,
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


async def _run_spin(anchor: Message, user_tg_id: int, redis) -> None:
    """anchor — сообщение, через .answer() которого создаём «крутящееся» сообщение."""
    if redis is None:
        redis = _redis()

    left = await _claim_or_ttl(redis, user_tg_id)
    if left is not None:
        await anchor.answer(
            premium_text(
                "🎡 <b>Колесо удачи уже крутили сегодня.</b>\n\n"
                f"Заходи через <b>{_fmt_left(left)}</b> — и крути снова. "
                "Каждый день новый шанс на джекпот 🎁"
            ),
            parse_mode="HTML",
        )
        return

    prize = _roll_prize()

    # Анимация «вращения» — подмена одного сообщения.
    msg = await anchor.answer(premium_text("🎡 <b>Крутим колесо удачи…</b>"), parse_mode="HTML")
    for frame in ("🎰  ❓ ✨ ❓  🎰", "🎡  ✨ 🎁 ✨  🎡", "🥁 <b>…почти…</b>"):
        await asyncio.sleep(0.7)
        try:
            await msg.edit_text(premium_text(frame), parse_mode="HTML")
        except Exception:
            pass
    await asyncio.sleep(0.6)

    # Начисление приза. При сбое — освобождаем дневной лок, чтобы юзер не
    # потерял крутку без приза (иначе «занял слот, кредитов нет»).
    new_balance = prize
    granted = False
    try:
        async with AsyncSessionFactory() as session:
            user = await get_user(session, user_tg_id)
            if user is None:
                await redis.delete(f"wheel:{user_tg_id}")  # вернуть крутку
                await msg.edit_text("Нажми /start для начала.", parse_mode="HTML")
                return
            await add_credits(session, user, prize)
            await session.commit()
            uid = user.id
            new_balance = (user.credits or 0) + (user.burn_credits or 0)
        log_event_bg(uid, "wheel_spin", credits_spent=-prize, prize=prize)
        granted = True
        # Запоминаем когда колесо снова будет доступно — для авто-нотификации.
        # ZSET wheel:ready_at member=tg_id score=unix_ts. Cron-job каждые ~30мин
        # выбирает due (score<=now) и шлёт «колесо готово».
        try:
            import time

            await redis.zadd(WHEEL_READY_ZSET, {str(user_tg_id): time.time() + _COOLDOWN})
        except Exception as exc:
            logger.warning(
                "wheel ready-zset add failed", user_tg_id=user_tg_id, error=str(exc)[:200]
            )
    except Exception as exc:
        logger.warning("wheel grant failed", user_tg_id=user_tg_id, error=str(exc)[:200])
    if not granted:
        try:
            await redis.delete(f"wheel:{user_tg_id}")  # вернуть крутку при сбое
        except Exception:
            pass
        await msg.edit_text(
            premium_text("😔 <b>Не удалось начислить приз — попробуй ещё раз через минуту.</b>"),
            parse_mode="HTML",
        )
        return

    if prize >= 500:
        head = "🎉🎰 <b>ДЖЕКПОТ! +500 кредитов!</b> 🎰🎉\nСегодня твой день!"
    elif prize >= 100:
        head = f"🔥 <b>Крупно повезло — +{prize} кредитов!</b>"
    else:
        head = f"🎁 <b>Выпало: +{prize} кредитов!</b>"

    try:
        await msg.edit_text(
            premium_text(
                f"{head}\n\n"
                f"💳 На счету: <b>{new_balance:,} кр</b>\n"
                f"<i>Возвращайся завтра — колесо крутится каждый день.</i>"
            ),
            parse_mode="HTML",
            reply_markup=_result_kb(),
        )
    except Exception:
        await anchor.answer(
            premium_text(f"{head}\n\n💳 На счету: <b>{new_balance:,} кр</b>"),
            parse_mode="HTML",
            reply_markup=_result_kb(),
        )


_WHEEL_READY_MSGS = [
    "🎡 <b>Колесо удачи снова готово!</b>\n\n"
    "Сегодня может выпасть <b>+500 кр</b> — джекпот недели.\n"
    "30 секунд на крутку.",
    "🎰 <b>Кто-то крутит — и срывает 500 кр.</b>\n\n"
    "Колесо снова доступно. Один спин в день — не пропусти свой шанс.",
    "🎁 <b>Твой бесплатный спин ждёт.</b>\n\n"
    "Минимум +10 кр, максимум — <b>+500 кр на счёт</b>. "
    "Деньги от подарка тратятся как обычные.",
]


def _wheel_ready_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text="🎡 Крутить колесо",
                    callback_data="wheel_spin",
                    icon_custom_emoji_id=PE_SPARKLES,
                    style="success",
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Каталог сцен",
                    callback_data="gallery_open",
                    icon_custom_emoji_id=PE_HOME,
                )
            ],
        ]
    )


async def notify_wheel_ready(bot) -> None:
    """Cron-job (handlers/admin.py scheduler). Берёт юзеров с истёкшим
    cooldown'ом из ZSET и шлёт push «колесо готово». Защита от ночного спама:
    с 23:00 до 09:00 МСК шлём НЕ шлём — отложенные ждут утра."""
    import time
    from datetime import datetime, timedelta

    msk_hour = (datetime.now(UTC) + timedelta(hours=3)).hour
    if msk_hour < 9 or msk_hour >= 23:
        return  # ночь — не спамим, нотификы накопятся и пойдут утром

    redis = _redis()
    now = time.time()
    try:
        due = await redis.zrangebyscore(WHEEL_READY_ZSET, 0, now, start=0, num=200)
    except Exception as exc:
        logger.warning("notify_wheel_ready zrange failed", error=str(exc)[:200])
        return
    if not due:
        return

    sent = blocked = 0
    for raw in due:
        try:
            tg_id = int(raw)
        except Exception:
            await redis.zrem(WHEEL_READY_ZSET, raw)
            continue
        text = random.choice(_WHEEL_READY_MSGS)
        try:
            await bot.send_message(
                tg_id, premium_text(text), parse_mode="HTML", reply_markup=_wheel_ready_kb()
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as exc:
            err = str(exc).lower()
            if "forbidden" in err or "blocked" in err or "deactivated" in err:
                blocked += 1
            else:
                logger.warning("wheel ready notify send failed", tg_id=tg_id, error=str(exc)[:200])
        finally:
            # Снимаем независимо от результата (заблокировавшего юзера повторно
            # пытаться слать бессмысленно).
            try:
                await redis.zrem(WHEEL_READY_ZSET, raw)
            except Exception:
                pass
    logger.info("wheel-ready notifications done", sent=sent, blocked=blocked, total=len(due))


@router.message(Command("spin"))
async def cmd_spin(message: Message, redis=None) -> None:
    await _run_spin(message, message.from_user.id, redis)


@router.callback_query(F.data == "wheel_spin")
async def cb_wheel_spin(callback: CallbackQuery, redis=None) -> None:
    await callback.answer()
    await _run_spin(callback.message, callback.from_user.id, redis)

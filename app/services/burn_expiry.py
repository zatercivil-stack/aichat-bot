"""Burn-credit expiry: ежедневная очистка сгоревших burn-кредитов + pre-burn
уведомления за 2 дня до сгорания.

Запускается scheduler'ом раз в день (admin.py: 04:00 МСК — спокойное время, юзеров
не разбудим).

Логика:
  1. Найти юзеров где burn_expires_at < now AND burn_credits > 0
     → обнулить burn_credits, отправить «сгорело N кр» если > 100 (мелочи не шлём)
  2. Найти юзеров где burn_expires_at < now + 2 days AND burn_credits > 100
     AND (burn_reminded_at IS NULL OR < now - 5 days)
     → отправить «осталось 2 дня, осталось N кр» + обновить burn_reminded_at
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.database.base import AsyncSessionFactory
from app.database.models import User
from app.keyboards.main import PE_CLIP, PE_FIRE, PE_FRAME, PE_UNLOCK

logger = structlog.get_logger(__name__)


PRE_BURN_DAYS = 2  # за сколько дней до сгорания шлём напоминание
MIN_BURN_FOR_REMINDER = 100  # если осталось <100 кр — не шлём (мелочи)


async def run_burn_expiry(bot_instance) -> None:
    now = datetime.now(UTC)
    expired_cutoff = now
    pre_burn_cutoff = now + timedelta(days=PRE_BURN_DAYS)
    reminder_cooldown = now - timedelta(days=5)

    burned = reminded = 0

    async with AsyncSessionFactory() as session:
        # 1. EXPIRED — обнуляем + уведомляем
        expired_users = (
            (
                await session.execute(
                    select(User).where(
                        User.burn_expires_at.isnot(None),
                        User.burn_expires_at < expired_cutoff,
                        User.burn_credits > 0,
                        not User.is_banned,
                    )
                )
            )
            .scalars()
            .all()
        )

        for u in expired_users:
            lost = u.burn_credits
            u.burn_credits = 0
            u.burn_expires_at = None
            u.burn_reminded_at = None
            if lost >= MIN_BURN_FOR_REMINDER:
                try:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton.model_construct(
                                    text="Купить новый срочный пак",
                                    callback_data="shop",
                                    icon_custom_emoji_id=PE_FIRE,
                                    style="success",
                                )
                            ],
                            [
                                InlineKeyboardButton.model_construct(
                                    text="Купить вечные кредиты",
                                    callback_data="shop",
                                    icon_custom_emoji_id=PE_UNLOCK,
                                    style="primary",
                                )
                            ],
                        ]
                    )
                    await bot_instance.send_message(
                        u.telegram_id,
                        f"⏱ <b>Срочные кредиты сгорели</b>\n\n"
                        f"Срок твоих срочных кредитов истёк — <b>{lost:,} кр</b> "
                        f"обнулились.\n\n"
                        f"Хочешь продолжить — возьми пак кредитов и сделай фото "
                        f"с твоим лицом за минуту.",
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception as exc:
                    logger.warning("burn-expired notify failed", user_id=u.id, error=str(exc)[:200])
            burned += 1

        # 2. PRE-BURN — напоминаем за 2 дня
        soon_users = (
            (
                await session.execute(
                    select(User).where(
                        User.burn_expires_at.isnot(None),
                        User.burn_expires_at > expired_cutoff,
                        User.burn_expires_at < pre_burn_cutoff,
                        User.burn_credits >= MIN_BURN_FOR_REMINDER,
                        not User.is_banned,
                    )
                )
            )
            .scalars()
            .all()
        )

        for u in soon_users:
            # Cooldown — не шлём чаще раз в 5 дней
            if u.burn_reminded_at:
                rem = u.burn_reminded_at
                if rem.tzinfo is None:
                    rem = rem.replace(tzinfo=UTC)
                if rem > reminder_cooldown:
                    continue
            exp = u.burn_expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            days_left = max(1, (exp - now).days)
            try:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton.model_construct(
                                text="Сделать фото сейчас",
                                callback_data="edit_start",
                                icon_custom_emoji_id=PE_CLIP,
                                style="success",
                            )
                        ],
                        [
                            InlineKeyboardButton.model_construct(
                                text="Каталог настроений",
                                callback_data="gallery_open",
                                icon_custom_emoji_id=PE_FRAME,
                                style="primary",
                            )
                        ],
                    ]
                )
                scenes = u.burn_credits // 200
                await bot_instance.send_message(
                    u.telegram_id,
                    f"⏱ <b>Через {days_left} дн. сгорят {u.burn_credits:,} срочных кредитов</b>\n\n"
                    f"Хватит на <b>{scenes}</b> фото с твоим лицом "
                    f"(Балет, старинное авто, Париж, неон, кино-нуар…)\n\n"
                    f"<i>Если не успеешь — кредиты обнулятся. Сделай фото сейчас 👇</i>",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                u.burn_reminded_at = now
                reminded += 1
            except Exception as exc:
                logger.warning("pre-burn notify failed", user_id=u.id, error=str(exc)[:200])

        await session.commit()

    if burned or reminded:
        logger.info("burn expiry done", burned=burned, reminded=reminded)

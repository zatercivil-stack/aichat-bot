"""Trial-end conversion: when user used all 3 trial Claude messages,
wait ~24h and send a soft conversion push if they haven't bought yet
and haven't been active in the last 6h (to not interrupt active sessions).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, select

from app.database.base import AsyncSessionFactory
from app.database.models import Payment, PaymentStatus, User
from app.keyboards.main import PE_AU_GOLD, PE_BACK, PE_CARD

logger = structlog.get_logger(__name__)


_TRIAL_END_TEXT = (
    "🎁 <b>3 бесплатных ответа Claude — закончились</b>\n\n"
    "Ты уже попробовал — теперь знаешь, на что я способна.\n\n"
    "⚡ Чтобы продолжать получать качественные ответы:\n"
    "— Бесплатно: <b>15 кр / день</b> (~5–15 простых вопросов)\n"
    "— Пакет: <b>500 кред за 99₽</b> — на месяц неспешного использования\n"
    "— Подписка <b>СТАРТ за 149₽</b> — 1000 кред/мес + ежедневные бонусы\n\n"
    "Если ещё не готов — заходи бесплатно завтра, я начислю ещё кредитов."
)


async def run_trial_end_push(bot_instance) -> None:
    """One-shot per user: send conversion push 24h after trial ran out."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    now = datetime.now(UTC)
    threshold = now - timedelta(hours=24)
    activity_cutoff = now - timedelta(hours=6)  # пропускаем активных

    sent = 0
    try:
        async with AsyncSessionFactory() as session:
            stmt = (
                select(User)
                .where(
                    and_(
                        User.trial_ended_at.is_not(None),
                        User.trial_ended_at <= threshold,
                        User.trial_end_push_sent.is_(False),
                        User.is_banned.is_(False),
                        User.last_active_at < activity_cutoff,  # давайте активным дочитать
                    )
                )
                .limit(500)
            )
            users = (await session.execute(stmt)).scalars().all()

            for user in users:
                # Уже купил пакет — конверсия совершена, push не нужен
                has_paid = await session.scalar(
                    select(Payment.id)
                    .where(
                        Payment.user_id == user.id,
                        Payment.status == PaymentStatus.SUCCEEDED,
                    )
                    .limit(1)
                )
                if has_paid:
                    user.trial_end_push_sent = True
                    continue

                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton.model_construct(
                                text="Оформить подписку — 149₽",
                                callback_data="subscription",
                                icon_custom_emoji_id=PE_AU_GOLD,
                                style="success",
                            )
                        ],
                        [
                            InlineKeyboardButton.model_construct(
                                text="Пакет 500 кр за 99₽",
                                callback_data="shop",
                                icon_custom_emoji_id=PE_CARD,
                                style="primary",
                            )
                        ],
                        [
                            InlineKeyboardButton.model_construct(
                                text="Пока подожду",
                                callback_data="main_menu",
                                icon_custom_emoji_id=PE_BACK,
                                style="danger",
                            )
                        ],
                    ]
                )
                try:
                    await bot_instance.send_message(
                        user.telegram_id,
                        _TRIAL_END_TEXT,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    user.trial_end_push_sent = True
                    sent += 1
                except Exception as exc:
                    logger.warning("trial-end push failed", user_id=user.id, error=str(exc)[:200])
                    # Также помечаем чтобы не долбить блокировавших
                    user.trial_end_push_sent = True

            await session.commit()
    except Exception as exc:
        logger.error("trial_end_push job failed", error=str(exc)[:200])
        return

    if sent:
        logger.info("trial_end_push complete", sent=sent)

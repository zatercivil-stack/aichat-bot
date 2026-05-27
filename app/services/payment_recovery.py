"""Payment recovery: nudge users who started but didn't finish payment.

Targets PENDING YooKassa/Platega payments aged 1h–24h. Анти-спам:
- Максимум **1 сообщение на юзера за run** независимо от того, сколько у него
  pending платежей. Все его pending помечаются как «уведомлены» одним мазком,
  чтобы и следующий run не повторил.
- Не шлём повторно, если последнее уведомление было <24ч назад (по любому
  из его pending платежей).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.database.base import AsyncSessionFactory
from app.database.models import Payment, PaymentStatus, User

logger = structlog.get_logger(__name__)


_RECOVERY_TEXT = (
    '💳 <b>Незавершённая оплата</b>\n\n'
    'Платёж был начат, но не завершён.\n'
    'Если передумали — это сообщение можно проигнорировать.\n'
    'Если просто отвлеклись — продолжите в магазине 👇'
)

# Не шлём повторное recovery если уже отправляли в последние 24 часа
RECOVERY_COOLDOWN = timedelta(hours=24)


async def run_payment_recovery(bot_instance) -> None:
    now = datetime.now(timezone.utc)
    too_recent = now - timedelta(hours=1)   # дайте 1 час на завершение
    too_old = now - timedelta(hours=24)     # >24ч — поздно, отметим failed
    cooldown_cutoff = now - RECOVERY_COOLDOWN

    sent = expired = skipped_dup = 0
    try:
        async with AsyncSessionFactory() as session:
            stmt = (
                select(Payment, User)
                .join(User, User.id == Payment.user_id)
                .where(
                    Payment.status == PaymentStatus.PENDING,
                    Payment.created_at < too_recent,
                    User.is_banned == False,
                )
                .order_by(Payment.user_id, Payment.created_at.desc())
                .limit(500)
            )
            result = await session.execute(stmt)
            rows = result.all()

            # Группируем pending по юзеру: один юзер → max 1 нотификация за run.
            by_user: dict[int, list[Payment]] = {}
            for payment, user in rows:
                by_user.setdefault(user.telegram_id, []).append((payment, user))

            for tg_id, items in by_user.items():
                user = items[0][1]
                # 1. Все «too old» помечаем failed
                user_payments_to_notify: list[Payment] = []
                last_recovery_dt: datetime | None = None
                for payment, _u in items:
                    created = payment.created_at
                    if created and created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created and created < too_old:
                        payment.status = PaymentStatus.FAILED
                        payment.updated_at = now
                        expired += 1
                        continue
                    user_payments_to_notify.append(payment)
                    # Самое свежее recovery_sent_at среди его pending'ов
                    rs = (payment.extra or {}).get('recovery_sent_at')
                    if rs:
                        try:
                            rs_dt = datetime.fromisoformat(rs)
                            if rs_dt.tzinfo is None:
                                rs_dt = rs_dt.replace(tzinfo=timezone.utc)
                            if last_recovery_dt is None or rs_dt > last_recovery_dt:
                                last_recovery_dt = rs_dt
                        except (ValueError, TypeError):
                            pass

                if not user_payments_to_notify:
                    continue

                # 2. Cooldown — если уже шлёт-ли в последние 24ч, пропускаем
                if last_recovery_dt and last_recovery_dt > cooldown_cutoff:
                    skipped_dup += 1
                    continue

                # 3. Шлём ОДНО сообщение юзеру и помечаем ВСЕ его pending'и
                try:
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                    from app.keyboards.main import PE_CARD, PE_BACK
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton.model_construct(
                            text='Вернуться в магазин',
                            callback_data='shop',
                            icon_custom_emoji_id=PE_CARD, style='success',
                        )],
                        [InlineKeyboardButton.model_construct(
                            text='Не сейчас',
                            callback_data='main_menu',
                            icon_custom_emoji_id=PE_BACK, style='danger',
                        )],
                    ])
                    await bot_instance.send_message(
                        tg_id, _RECOVERY_TEXT,
                        parse_mode='HTML', reply_markup=kb,
                    )
                    sent += 1
                except Exception as exc:
                    logger.warning('recovery send failed',
                                   user_id=user.id, error=str(exc)[:200])
                    continue

                # Помечаем ВСЕ pending этого юзера — чтобы будущие run'ы тоже
                # уважали cooldown даже если юзер создаст новый pending позже
                for payment in user_payments_to_notify:
                    extra = dict(payment.extra or {})
                    extra['recovery_sent_at'] = now.isoformat()
                    payment.extra = extra
                    flag_modified(payment, 'extra')

            await session.commit()
    except Exception as exc:
        logger.error('payment_recovery failed', error=str(exc)[:200])
        return

    if sent or expired or skipped_dup:
        logger.info('payment_recovery done',
                    recovered=sent, expired=expired, dedup_skipped=skipped_dup)

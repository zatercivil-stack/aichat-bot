from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import AsyncSessionFactory
from app.database.models import BotEvent, Payment, PaymentStatus, User

logger = structlog.get_logger(__name__)


async def log_event(
    user_id: int | None,
    event_type: str,
    model_key: str | None = None,
    credits_spent: int = 0,
    **extra,
) -> None:
    """Fire-and-forget event logger — never raises."""
    try:
        async with AsyncSessionFactory() as session:
            event = BotEvent(
                user_id=user_id,
                event_type=event_type,
                model_key=model_key,
                credits_spent=credits_spent,
                extra=extra or {},
            )
            session.add(event)
            await session.commit()
    except Exception as exc:
        logger.error('log_event failed', event_type=event_type, error=str(exc)[:200])


_pending_event_tasks: set[asyncio.Task] = set()


def log_event_bg(
    user_id: int | None,
    event_type: str,
    model_key: str | None = None,
    credits_spent: int = 0,
    **extra,
) -> None:
    """Schedule event logging as background task (non-blocking)."""
    try:
        task = asyncio.create_task(
            log_event(user_id, event_type, model_key, credits_spent, **extra)
        )
        # Keep a strong reference so the task is not garbage-collected mid-run.
        _pending_event_tasks.add(task)
        task.add_done_callback(_pending_event_tasks.discard)
    except RuntimeError as exc:
        logger.error('log_event_bg: no running loop', event_type=event_type, error=str(exc))


async def count_user_events(
    session: AsyncSession, user_id: int, event_type: str,
) -> int:
    """Сколько событий event_type у юзера за всё время. Используется для
    photo-milestones (3/7/15 фото = бонусы). Дёшево — индекс по user_id+type."""
    return await session.scalar(
        select(func.count()).select_from(BotEvent).where(
            BotEvent.user_id == user_id,
            BotEvent.event_type == event_type,
        )
    ) or 0


async def get_excluded_user_ids(session: AsyncSession) -> list[int]:
    """Внутренние user.id админов и test-юзера (из bot_config.test_broadcast_username,
    по дефолту @grelok) — нужно исключать из ВСЕХ stat-запросов, чтобы тестовая
    активность не врала в дашборде. Возвращает пустой список если ничего."""
    from app.config import settings
    from app.database.repo.bot_config import get_config_value
    tg_ids = list(settings.admin_ids_list or [])
    try:
        test_un = (await get_config_value(session, 'test_broadcast_username') or '').lstrip('@').strip()
        if test_un:
            tg = await session.scalar(
                select(User.telegram_id).where(User.username.ilike(test_un))
            )
            if tg:
                tg_ids.append(tg)
    except Exception:
        pass
    if not tg_ids:
        return []
    return list((await session.execute(
        select(User.id).where(User.telegram_id.in_(set(tg_ids)))
    )).scalars().all())


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """All stats needed for admin dashboard overview."""
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    # Тех-юзеры (админ + grelok) — исключаем из всех фото-метрик. Иначе тестер
    # с сотнями генераций забивает топ и завышает счётчики.
    exclude_uids = await get_excluded_user_ids(session)

    # Total + new users
    total_users = await session.scalar(select(func.count()).select_from(User))
    new_today = await session.scalar(
        select(func.count()).select_from(User).where(User.created_at >= today)
    )
    new_week = await session.scalar(
        select(func.count()).select_from(User).where(User.created_at >= week_ago)
    )
    new_month = await session.scalar(
        select(func.count()).select_from(User).where(User.created_at >= month_ago)
    )

    # Active users (sent at least one message)
    active_today = await session.scalar(
        select(func.count(func.distinct(BotEvent.user_id)))
        .where(BotEvent.event_type == 'message_sent', BotEvent.created_at >= today)
    )
    active_week = await session.scalar(
        select(func.count(func.distinct(BotEvent.user_id)))
        .where(BotEvent.event_type == 'message_sent', BotEvent.created_at >= week_ago)
    )

    # Messages
    messages_today = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'message_sent', BotEvent.created_at >= today)
    )
    messages_week = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'message_sent', BotEvent.created_at >= week_ago)
    )

    # Revenue
    revenue_today = await session.scalar(
        select(func.sum(Payment.amount))
        .where(Payment.status == PaymentStatus.SUCCEEDED, Payment.created_at >= today)
    ) or 0
    revenue_month = await session.scalar(
        select(func.sum(Payment.amount))
        .where(Payment.status == PaymentStatus.SUCCEEDED, Payment.created_at >= month_ago)
    ) or 0

    # Model usage (7d)
    model_rows = await session.execute(
        select(BotEvent.model_key, func.count().label('cnt'))
        .where(BotEvent.event_type == 'message_sent', BotEvent.created_at >= week_ago, BotEvent.model_key.isnot(None))
        .group_by(BotEvent.model_key)
        .order_by(func.count().desc())
    )
    model_usage = {row.model_key: row.cnt for row in model_rows}

    # Voice + vision + images (7d)
    voice_week = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'voice_sent', BotEvent.created_at >= week_ago)
    )
    vision_week = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'vision_analyzed', BotEvent.created_at >= week_ago)
    )
    images_week = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'image_generated', BotEvent.created_at >= week_ago)
    )

    # Фото-правки (главная фишка после пивота). Тех-юзеры исключены через
    # exclude_uids — иначе тестер @grelok с сотнями генераций забивает топ.
    image_edits_today = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'image_edited', BotEvent.created_at >= today,
               BotEvent.user_id.notin_(exclude_uids))
    ) or 0
    image_edits_week = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'image_edited', BotEvent.created_at >= week_ago,
               BotEvent.user_id.notin_(exclude_uids))
    ) or 0
    face_swaps_week = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'face_swapped', BotEvent.created_at >= week_ago,
               BotEvent.user_id.notin_(exclude_uids))
    ) or 0
    daily_edits_rows = await session.execute(
        select(
            func.date_trunc('day', BotEvent.created_at).label('day'),
            func.count().label('cnt'),
        )
        .where(BotEvent.event_type == 'image_edited',
               BotEvent.created_at >= today - timedelta(days=13),
               BotEvent.user_id.notin_(exclude_uids))
        .group_by(text('1'))
        .order_by(text('1'))
    )
    daily_image_edits = [{'day': str(r.day)[:10], 'cnt': r.cnt} for r in daily_edits_rows]
    # Топ пресетов за 7 дней. preset_key пишется в extra c 22.05 — старые
    # события вернут NULL → пропустим. BotEvent.extra — JSON (не JSONB), поэтому
    # для текстового доступа используем PG-оператор `->>` через .op().
    preset_key_expr = BotEvent.extra.op('->>')('preset_key')
    top_preset_rows = await session.execute(
        select(preset_key_expr.label('preset_key'), func.count().label('cnt'))
        .where(BotEvent.event_type == 'image_edited',
               BotEvent.created_at >= week_ago,
               preset_key_expr.isnot(None),
               BotEvent.user_id.notin_(exclude_uids))
        .group_by(preset_key_expr)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_presets_week = [{'key': r.preset_key, 'cnt': r.cnt} for r in top_preset_rows]

    # Daily new users (last 14 days for chart)
    daily_rows = await session.execute(
        select(
            func.date_trunc('day', User.created_at).label('day'),
            func.count().label('cnt'),
        )
        .where(User.created_at >= today - timedelta(days=13))
        .group_by(text('1'))
        .order_by(text('1'))
    )
    daily_new_users = [{'day': str(r.day)[:10], 'cnt': r.cnt} for r in daily_rows]

    # Daily messages (last 14 days for chart)
    daily_msg_rows = await session.execute(
        select(
            func.date_trunc('day', BotEvent.created_at).label('day'),
            func.count().label('cnt'),
        )
        .where(BotEvent.event_type == 'message_sent', BotEvent.created_at >= today - timedelta(days=13))
        .group_by(text('1'))
        .order_by(text('1'))
    )
    daily_messages = [{'day': str(r.day)[:10], 'cnt': r.cnt} for r in daily_msg_rows]

    # Banned users count
    banned_count = await session.scalar(
        select(func.count()).select_from(User).where(User.is_banned == True)
    )

    # Errors (24h)
    errors_24h = await session.scalar(
        select(func.count()).select_from(BotEvent)
        .where(BotEvent.event_type == 'error', BotEvent.created_at >= today)
    )

    # ── Главные KPI воронки Нейры: активация (регистр→≥1 фото) и continuation
    # (≥1→≥2 фото). Считаем по реальным юзерам (без тех и забаненных).
    total_real = await session.scalar(
        select(func.count()).select_from(User)
        .where(User.id.notin_(exclude_uids), User.is_banned == False)
    ) or 0
    users_1plus_gen = await session.scalar(
        select(func.count(func.distinct(BotEvent.user_id)))
        .where(BotEvent.event_type == 'image_edited',
               BotEvent.user_id.notin_(exclude_uids))
    ) or 0
    _gen2_subq = (
        select(BotEvent.user_id)
        .where(BotEvent.event_type == 'image_edited',
               BotEvent.user_id.notin_(exclude_uids))
        .group_by(BotEvent.user_id)
        .having(func.count() >= 2)
        .subquery()
    )
    users_2plus_gen = await session.scalar(
        select(func.count()).select_from(_gen2_subq)
    ) or 0
    activation_rate = round(100.0 * users_1plus_gen / total_real, 1) if total_real else 0.0
    second_gen_rate = round(100.0 * users_2plus_gen / users_1plus_gen, 1) if users_1plus_gen else 0.0

    return {
        'total_users': total_users or 0,
        'new_today': new_today or 0,
        'new_week': new_week or 0,
        'new_month': new_month or 0,
        'active_today': active_today or 0,
        'active_week': active_week or 0,
        'messages_today': messages_today or 0,
        'messages_week': messages_week or 0,
        'revenue_today': float(revenue_today),
        'revenue_month': float(revenue_month),
        'model_usage': model_usage,
        'voice_week': voice_week or 0,
        'vision_week': vision_week or 0,
        'images_week': images_week or 0,
        'image_edits_today': image_edits_today,
        'image_edits_week': image_edits_week,
        'face_swaps_week': face_swaps_week,
        'daily_image_edits': daily_image_edits,
        'top_presets_week': top_presets_week,
        'daily_new_users': daily_new_users,
        'daily_messages': daily_messages,
        'banned_count': banned_count or 0,
        'errors_24h': errors_24h or 0,
        'total_real': total_real,
        'users_1plus_gen': users_1plus_gen,
        'users_2plus_gen': users_2plus_gen,
        'activation_rate': activation_rate,
        'second_gen_rate': second_gen_rate,
    }


async def get_recent_events(session: AsyncSession, limit: int = 50) -> list[dict]:
    rows = await session.execute(
        select(BotEvent, User.username, User.telegram_id)
        .outerjoin(User, User.id == BotEvent.user_id)
        .order_by(BotEvent.created_at.desc())
        .limit(limit)
    )
    result = []
    for event, username, tg_id in rows:
        result.append({
            'id': event.id,
            'event_type': event.event_type,
            'model_key': event.model_key,
            'credits_spent': event.credits_spent,
            'username': username,
            'telegram_id': tg_id,
            'created_at': event.created_at.strftime('%d.%m %H:%M') if event.created_at else '',
            'extra': event.extra,
        })
    return result

"""Расчёт оставшегося баланса fal.ai через локальный учёт.

У fal.ai нет публичного API для запроса баланса аккаунта — приходится считать
самим. Логика: админ вручную сообщает «пополнил на $X» (команда /falbalance),
сохраняем baseline в bot_config, а текущий баланс = baseline − расход с тех
пор (по bot_events).

Это позволяет:
- Защищать дорогие операции (precise) при низком балансе
- Давать юзерам корректные сообщения если fal недоступен
- Шлёть админу alert при падении ниже порога
- Daily-отчёт о расходе

Себестоимость взята из реальных счетов fal.ai (см. скрин 18.05.2026):
  nano-banana-2/edit  = $0.08
  nano-banana-2       = $0.08 (image_gen + face_swap fallback)
  face-swap dedicated = $0.001
  gemini-25-flash     = $0.0398
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from app.database.base import AsyncSessionFactory
from app.database.models import BotEvent
from app.database.repo.bot_config import get_config_value, set_config_value

logger = structlog.get_logger(__name__)

# ── Тарифы fal.ai (USD per call) — реальные ставки на 18.05.2026 ───────────
# Если ставки меняются — обновить здесь.
COST_EDIT_QUALITY = 0.12           # quality = nano-banana-2/edit @ 2K (×1.5 от 1K $0.08)
COST_EDIT_PRECISE = 0.12 + 0.001   # precise = edit @2K + face-swap (dedicated)
COST_FACE_SWAP = 0.001             # standalone face-swap
COST_GENERATE = 0.08               # channel posts @ 1K (resolution не задаём → дефолт)

# Пороги защиты (USD)
PRECISE_BLOCK_THRESHOLD = 0.20      # ниже — выключаем precise
ALL_FAL_BLOCK_THRESHOLD = 0.05      # ниже — выключаем все fal-операции
LOW_BALANCE_ALERT_THRESHOLD = 0.30  # ниже — шлём админу alert (1 раз/день)

# Keys в bot_config
KEY_BASELINE_USD = 'fal_topup_baseline_usd'
KEY_BASELINE_AT = 'fal_topup_baseline_at'  # ISO datetime
KEY_LAST_ALERT_AT = 'fal_low_balance_last_alert'  # ISO datetime, чтобы не спамить


async def set_baseline(balance_usd: float) -> None:
    """Админ зовёт это после пополнения fal.ai.
    Сохраняет текущий баланс и время — это точка отсчёта для расчёта расхода.
    """
    now_iso = datetime.now(UTC).isoformat()
    async with AsyncSessionFactory() as session:
        await set_config_value(session, KEY_BASELINE_USD, str(balance_usd))
        await set_config_value(session, KEY_BASELINE_AT, now_iso)
        # Сбрасываем last_alert чтобы при новом падении шёл alert
        await set_config_value(session, KEY_LAST_ALERT_AT, '')
        await session.commit()
    logger.info('fal baseline set', balance=balance_usd, at=now_iso)


async def fetch_real_balance() -> float | None:
    """Опрашивает официальное fal.ai API (/v1/account/billing) и возвращает
    точный баланс в USD. Требует FAL_BILLING_API_KEY (admin-scoped, отличный
    от модельного FAL_AI_API_KEY). None если ключ не задан или API не ответил.

    Это «источник истины» — обходит дрейф локального трекера, который не видит
    админ-скриптовые генерации (они идут мимо bot_events).
    """
    from app.config import settings
    key = (settings.FAL_BILLING_API_KEY or '').strip()
    if not key:
        return None
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                'https://api.fal.ai/v1/account/billing?expand=credits',
                headers={'Authorization': f'Key {key}'},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    logger.warning('fetch_real_balance HTTP %d: %s', resp.status, body)
                    return None
                data = await resp.json()
        bal = float(((data or {}).get('credits') or {}).get('current_balance', 0))
        return bal
    except Exception as exc:
        logger.warning('fetch_real_balance failed: %s', str(exc)[:200])
        return None


async def sync_baseline_from_real() -> float | None:
    """Опрашивает реальный баланс и переустанавливает baseline под него.
    Возвращает реальный баланс или None если не удалось получить.

    Используется scheduled-job'ом раз в N часов — таким образом локальный
    трекер не дрейфует от реальности из-за неучтённых генераций.
    """
    bal = await fetch_real_balance()
    if bal is None:
        return None
    await set_baseline(bal)
    logger.info('fal baseline auto-synced from API', balance=bal)
    return bal


async def _get_baseline() -> tuple[float, datetime | None]:
    """Возвращает (baseline_usd, timestamp_utc). (0, None) если не задан."""
    async with AsyncSessionFactory() as session:
        raw_bal = await get_config_value(session, KEY_BASELINE_USD)
        raw_at = await get_config_value(session, KEY_BASELINE_AT)
    if not raw_bal or not raw_at:
        return 0.0, None
    try:
        bal = float(raw_bal)
        at = datetime.fromisoformat(raw_at)
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return bal, at
    except Exception as exc:
        logger.warning('fal baseline parse failed', error=str(exc)[:200])
        return 0.0, None


async def _calculate_spent_since(since: datetime) -> float:
    """Считает $ потраченные с момента since по bot_events.

    Подход: смотрим events типа image_edited / face_swapped / image_generated.
    Для каждого определяем стоимость по quality_mode / event_type.
    """
    async with AsyncSessionFactory() as session:
        # Только события use fal: image_edited (quality/precise/без mode = quality),
        # face_swapped (standalone), image_generated (мы редко юзаем, обычно OpenRouter).
        rows = await session.execute(
            select(BotEvent.event_type, BotEvent.extra)
            .where(BotEvent.created_at >= since)
            .where(BotEvent.event_type.in_(
                ['image_edited', 'face_swapped', 'image_generated']
            ))
        )
        events = rows.all()

    total = 0.0
    for event_type, extra in events:
        if event_type == 'image_edited':
            mode = (extra or {}).get('quality_mode', 'quality')
            if mode == 'precise':
                total += COST_EDIT_PRECISE
            elif mode == 'fast':
                # fast идёт через openrouter, не fal — не считаем
                continue
            else:
                # quality или без mode (default quality)
                total += COST_EDIT_QUALITY
        elif event_type == 'face_swapped':
            total += COST_FACE_SWAP
        elif event_type == 'image_generated':
            # Может идти через fal или OpenRouter — пессимистично считаем как fal
            total += COST_GENERATE
    return total


async def get_balance() -> float | None:
    """Текущий оценочный баланс fal.ai в USD.
    None если baseline не установлен (админ ещё не сказал сколько).
    """
    baseline, baseline_at = await _get_baseline()
    if baseline_at is None:
        return None
    spent = await _calculate_spent_since(baseline_at)
    return max(0.0, baseline - spent)


async def is_precise_allowed() -> bool:
    """precise операции разрешены если baseline неизвестен (failsafe — лучше дать
    чем ошибочно заблокировать) или если баланс выше threshold."""
    bal = await get_balance()
    if bal is None:
        return True
    return bal > PRECISE_BLOCK_THRESHOLD


async def is_fal_allowed() -> bool:
    """Все fal операции разрешены если баланс выше abs-threshold или неизвестен."""
    bal = await get_balance()
    if bal is None:
        return True
    return bal > ALL_FAL_BLOCK_THRESHOLD


async def should_alert_admin() -> bool:
    """Проверяет: нужно ли админу шлёт alert о низком балансе?
    Шлём 1 раз в сутки максимум, чтобы не спамить.
    """
    bal = await get_balance()
    if bal is None or bal > LOW_BALANCE_ALERT_THRESHOLD:
        return False
    async with AsyncSessionFactory() as session:
        last = await get_config_value(session, KEY_LAST_ALERT_AT)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        hours_since = (datetime.now(UTC) - last_dt).total_seconds() / 3600
        return hours_since >= 24
    except Exception:
        return True


async def mark_alert_sent() -> None:
    async with AsyncSessionFactory() as session:
        await set_config_value(session, KEY_LAST_ALERT_AT,
                               datetime.now(UTC).isoformat())
        await session.commit()


async def get_daily_stats() -> dict:
    """Для daily-отчёта. Считает расход за последние 24 часа и тренд."""
    from datetime import timedelta
    now = datetime.now(UTC)
    today_start = now - timedelta(hours=24)
    yesterday_start = now - timedelta(hours=48)

    spent_today = await _calculate_spent_since(today_start)
    # Расход за позавчерашние 24ч (от 48ч до 24ч назад) сравнить с сегодня
    async with AsyncSessionFactory() as session:
        rows = await session.execute(
            select(BotEvent.event_type, BotEvent.extra)
            .where(BotEvent.created_at >= yesterday_start)
            .where(BotEvent.created_at < today_start)
            .where(BotEvent.event_type.in_(['image_edited', 'face_swapped', 'image_generated']))
        )
        events = rows.all()
    spent_yesterday = 0.0
    counts = {'quality': 0, 'precise': 0, 'face_swap': 0, 'gen': 0, 'fast': 0}
    for et, extra in events:
        if et == 'image_edited':
            mode = (extra or {}).get('quality_mode', 'quality')
            if mode == 'precise':
                spent_yesterday += COST_EDIT_PRECISE
            elif mode == 'fast':
                continue
            else:
                spent_yesterday += COST_EDIT_QUALITY
        elif et == 'face_swapped':
            spent_yesterday += COST_FACE_SWAP
        elif et == 'image_generated':
            spent_yesterday += COST_GENERATE

    # Подсчёт операций за сегодня для отчёта
    async with AsyncSessionFactory() as session:
        rows = await session.execute(
            select(BotEvent.event_type, BotEvent.extra)
            .where(BotEvent.created_at >= today_start)
            .where(BotEvent.event_type.in_(['image_edited', 'face_swapped', 'image_generated']))
        )
        events = rows.all()
    for et, extra in events:
        if et == 'image_edited':
            mode = (extra or {}).get('quality_mode', 'quality')
            counts[mode if mode in counts else 'quality'] += 1
        elif et == 'face_swapped':
            counts['face_swap'] += 1
        elif et == 'image_generated':
            counts['gen'] += 1

    balance = await get_balance()
    days_left = None
    if balance is not None and spent_today > 0.01:
        days_left = balance / spent_today
    return {
        'balance': balance,
        'spent_today': spent_today,
        'spent_yesterday': spent_yesterday,
        'counts': counts,
        'days_left': days_left,
    }

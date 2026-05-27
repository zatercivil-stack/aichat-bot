"""Model health tracker (Redis-based).

For each model ID we maintain two Redis sorted sets:
  - success_count (sliding 5 min window of successful calls)
  - failure_count (sliding 5 min window of failed calls)

A model is "unhealthy" when:
  - >= 3 failures AND failure_rate > 60% in the window
  - or 5 consecutive failures (no successes between)

Unhealthy models are skipped for `COOLDOWN_S` seconds, then auto-retried.
This avoids hammering a stuck OpenRouter endpoint while still giving it
recovery chances.

All operations are best-effort: any Redis error is swallowed and logged —
we'd rather have slow degraded health-tracking than to break the actual bot.
"""
from __future__ import annotations

import time

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

WINDOW_S = 300            # 5-minute sliding window
COOLDOWN_S = 600          # 10-minute cooldown once flagged unhealthy
MIN_SAMPLES = 3           # need at least N events to make a decision
UNHEALTHY_FAIL_RATIO = 0.6
HARD_FAIL_STREAK = 5      # consecutive failures => instant cooldown


def _success_key(model_id: str) -> str:
    return f'model_health:{model_id}:ok'


def _failure_key(model_id: str) -> str:
    return f'model_health:{model_id}:fail'


def _cooldown_key(model_id: str) -> str:
    return f'model_health:{model_id}:cooldown'


def _streak_key(model_id: str) -> str:
    return f'model_health:{model_id}:streak'


async def record_success(redis: Redis, model_id: str) -> None:
    """Записывает успешный вызов модели."""
    if redis is None:
        return
    now = int(time.time() * 1000)
    try:
        pipe = redis.pipeline()
        pipe.zadd(_success_key(model_id), {f'{now}:s': now})
        pipe.zremrangebyscore(_success_key(model_id), 0, now - WINDOW_S * 1000)
        pipe.expire(_success_key(model_id), WINDOW_S + 60)
        pipe.set(_streak_key(model_id), 0, ex=WINDOW_S + 60)
        await pipe.execute()
    except Exception as exc:
        logger.warning('model_health record_success failed',
                      model=model_id, error=str(exc)[:200])


async def record_failure(redis: Redis, model_id: str) -> None:
    """Записывает неуспешный вызов; при условии — ставит cooldown."""
    if redis is None:
        return
    now = int(time.time() * 1000)
    try:
        pipe = redis.pipeline()
        pipe.zadd(_failure_key(model_id), {f'{now}:f': now})
        pipe.zremrangebyscore(_failure_key(model_id), 0, now - WINDOW_S * 1000)
        pipe.expire(_failure_key(model_id), WINDOW_S + 60)
        pipe.incr(_streak_key(model_id))
        pipe.expire(_streak_key(model_id), WINDOW_S + 60)
        results = await pipe.execute()
        streak = int(results[-2] or 0)

        if streak >= HARD_FAIL_STREAK:
            await redis.set(_cooldown_key(model_id), '1', ex=COOLDOWN_S)
            logger.warning('model marked unhealthy (streak)',
                          model=model_id, streak=streak)
            return

        # Check rate-based unhealthy
        ok_cnt = await redis.zcard(_success_key(model_id))
        fail_cnt = await redis.zcard(_failure_key(model_id))
        total = ok_cnt + fail_cnt
        if total >= MIN_SAMPLES and (fail_cnt / total) > UNHEALTHY_FAIL_RATIO:
            await redis.set(_cooldown_key(model_id), '1', ex=COOLDOWN_S)
            logger.warning('model marked unhealthy (rate)',
                          model=model_id, fails=fail_cnt, total=total)
    except Exception as exc:
        logger.warning('model_health record_failure failed',
                      model=model_id, error=str(exc)[:200])


async def is_unhealthy(redis: Redis, model_id: str) -> bool:
    """True если модель сейчас в cooldown'е."""
    if redis is None:
        return False
    try:
        v = await redis.get(_cooldown_key(model_id))
        return v is not None
    except Exception:
        return False


async def get_stats(redis: Redis, model_ids: list[str]) -> list[dict]:
    """Per-model snapshot для /admin/health."""
    out = []
    if redis is None:
        return [{'model': m, 'success': 0, 'failure': 0, 'unhealthy': False,
                 'cooldown_left': 0} for m in model_ids]
    for model_id in model_ids:
        try:
            success = await redis.zcard(_success_key(model_id))
            failure = await redis.zcard(_failure_key(model_id))
            cool_ttl = await redis.ttl(_cooldown_key(model_id))
            total = success + failure
            rate = (success / total * 100) if total else 0.0
            out.append({
                'model': model_id,
                'success': int(success),
                'failure': int(failure),
                'success_rate': round(rate, 1),
                'unhealthy': cool_ttl > 0,
                'cooldown_left': max(0, cool_ttl) if cool_ttl else 0,
            })
        except Exception as exc:
            logger.warning('model_health stats failed', model=model_id, error=str(exc)[:200])
            out.append({'model': model_id, 'success': 0, 'failure': 0,
                       'success_rate': 0, 'unhealthy': False, 'cooldown_left': 0})
    return out

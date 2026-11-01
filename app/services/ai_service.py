from __future__ import annotations

import structlog
from openai import AsyncOpenAI

from app.config import settings
from app.services import model_health

logger = structlog.get_logger(__name__)

# Приоритетный список моделей по каждому tier (от лучшей к Good-enough fallback)
# Все модели проверены на доступность 4 мая 2026
# Стоимость указана примерно за стандартное сообщение (~100 in + 50 out токенов)
TIER_MODELS: dict[str, list[str]] = {
    "free": [
        "deepseek/deepseek-v3.2",  # основной: $0.25/М in, $0.38/М out, 1M ctx
        "deepseek/deepseek-chat-v3.1",  # fallback: $0.15/М in
        "deepseek/deepseek-v4-flash",  # fallback: $0.14/М in
    ],
    "basic": [
        "openai/gpt-5-nano",  # основной: $0.05/М in, $0.4/М out
        "openai/gpt-4.1-mini",  # fallback: $0.4/М in
        "openai/gpt-4o-mini",  # fallback: $0.15/М in
    ],
    "mid": [
        "openai/gpt-5-mini",  # основной: $0.25/М in — работает стабильно, дешевле всех
        "deepseek/deepseek-v4-pro",  # fallback: $0.435/М in
        "google/gemini-3-flash-preview",  # fallback: $0.5/М in — сейчас 403 от Google AI Studio, оставлен на будущее
    ],
    "pro": [
        "anthropic/claude-sonnet-4.6",  # основной: $3/М in, $15/М out, 1M ctx
        "openai/gpt-5.1",  # fallback: $1.25/М in
        "x-ai/grok-4.3",  # fallback: $1.25/М in
    ],
    "ultra": [
        "anthropic/claude-opus-4.7",  # основной: $5/М in, $25/М out — топ
        "openai/gpt-5.5",  # fallback: $5/М in, $30/М out
        "google/gemini-3.1-pro-preview",  # fallback: $2/М in (дешевле, сильный)
    ],
}

TIER_COST_PER_MILLION: dict[str, float] = {
    "free": 0.30,  # DeepSeek V3.2
    "basic": 0.20,  # GPT-5 nano
    "mid": 0.4,  # GPT-5 mini (primary)
    "pro": 8.0,  # Claude Sonnet 4.6
    "ultra": 15.0,  # Claude Opus 4.7
}

SYSTEM_PROMPT = (
    "Ты умный AI-ассистент. Отвечай чётко, по делу, на том языке, "
    "на котором пишет пользователь. Если вопрос на русском — отвечай на русском."
)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://t.me/",
                "X-Title": "AI Bot",
            },
        )
    return _client


def _get_redis():
    """Lazy redis client for health tracking. Best-effort — None if unavailable."""
    try:
        from redis.asyncio import Redis

        return Redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


_health_redis = None


async def chat(messages: list[dict], tier: str) -> str:
    """Выполняет запрос к первой доступной модели из tier-списка.
    tier: 'free' | 'basic' | 'pro' | 'ultra'
    """
    global _health_redis
    if _health_redis is None:
        _health_redis = _get_redis()

    models = TIER_MODELS.get(tier, TIER_MODELS["free"])
    client = get_client()

    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    last_error = None
    skipped: list[str] = []

    for model_id in models:
        # Health-tracker: skip models that are currently in cooldown
        if _health_redis is not None and await model_health.is_unhealthy(_health_redis, model_id):
            skipped.append(model_id)
            logger.info("Skipping unhealthy model", model=model_id, tier=tier)
            continue
        try:
            logger.info("Trying AI model", model=model_id, tier=tier)

            response = await client.chat.completions.create(
                model=model_id,
                messages=full_messages,
                max_tokens=2048,
                temperature=0.7,
            )

            text = response.choices[0].message.content or ""
            usage = response.usage

            # Логируем стоимость (приблизительно)
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
            approx_cost = (prompt_tokens / 1_000_000) * TIER_COST_PER_MILLION.get(tier, 0)

            logger.info(
                "AI response",
                model=model_id,
                tier=tier,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                approx_cost_usd=round(approx_cost, 6),
            )
            if _health_redis is not None:
                await model_health.record_success(_health_redis, model_id)
            return text.strip()

        except Exception as exc:
            error_str = str(exc).lower()
            last_error = exc
            if _health_redis is not None:
                await model_health.record_failure(_health_redis, model_id)

            # Ошибка временная или провайдер-специфичная → пробуем следующую модель.
            should_retry = any(
                kw in error_str
                for kw in [
                    "403",
                    "blocked",
                    "429",
                    "rate limit",
                    "quota",
                    "exceeded",
                    "500",
                    "502",
                    "503",
                    "504",
                    "timeout",
                    "connection",
                    "network",
                    "service unavailable",
                    "overloaded",
                ]
            )

            logger.warning(
                "AI model failed, trying next",
                model=model_id,
                tier=tier,
                error_type=type(exc).__name__,
                retry=should_retry,
            )

            if should_retry:
                continue
            else:
                logger.error(
                    "Non-retryable AI error", model=model_id, tier=tier, error=str(exc)[:200]
                )
                break

    # Если ВСЕ модели tier'а в cooldown'е — снимаем cooldown с первой и пробуем
    # ещё раз (даём шанс восстановиться, лучше чем глухая ошибка)
    if skipped and len(skipped) == len(models) and _health_redis is not None:
        first = models[0]
        try:
            await _health_redis.delete(f"model_health:{first}:cooldown")
            await _health_redis.delete(f"model_health:{first}:streak")
            logger.warning("All models cooled down — force-retrying first", tier=tier, model=first)
            response = await client.chat.completions.create(
                model=first,
                messages=full_messages,
                max_tokens=2048,
                temperature=0.7,
            )
            text = response.choices[0].message.content or ""
            await model_health.record_success(_health_redis, first)
            return text.strip()
        except Exception as exc:
            await model_health.record_failure(_health_redis, first)
            last_error = exc

    logger.error(
        "All AI models failed", tier=tier, last_error=str(last_error)[:200], skipped=skipped
    )
    return "❌ Произошла ошибка при обращении к AI. Попробуй ещё раз через несколько секунд."

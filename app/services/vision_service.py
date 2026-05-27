from __future__ import annotations

import base64
import structlog
from openai import AsyncOpenAI

from app.config import settings

logger = structlog.get_logger(__name__)

VISION_MODELS = [
    'google/gemini-3-flash-preview',
    'anthropic/claude-sonnet-4.6',
    'openai/gpt-4o-2024-11-20',
]

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url='https://openrouter.ai/api/v1',
            default_headers={'HTTP-Referer': 'https://t.me/', 'X-Title': 'AI Bot'},
        )
    return _client


async def analyze(image_bytes: bytes, prompt: str = '', mime: str = 'image/jpeg') -> str:
    """Анализирует картинку через vision-модель.
    prompt: текст-вопрос/caption. Если пусто — описать картинку.
    Возвращает текст-ответ или пустую строку при ошибке."""
    img_b64 = base64.b64encode(image_bytes).decode('utf-8')
    user_text = prompt.strip() or 'Опиши что на этой картинке. Будь подробным и точным.'

    client = _get_client()
    last_error = None
    for model_id in VISION_MODELS:
        try:
            logger.info('Vision analyze', model=model_id,
                       prompt_len=len(user_text), img_size=len(image_bytes))
            response = await client.chat.completions.create(
                model=model_id,
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': user_text},
                        {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{img_b64}'}},
                    ],
                }],
                max_tokens=2048,
                temperature=0.5,
            )
            text = response.choices[0].message.content or ''
            return text.strip()
        except Exception as exc:
            last_error = exc
            logger.warning('Vision model failed', model=model_id,
                          error_type=type(exc).__name__,
                          error=str(exc)[:200])
            continue
    logger.error('All vision models failed', error=str(last_error)[:200] if last_error else 'unknown')
    return ''

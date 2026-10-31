from __future__ import annotations

import base64

import structlog
from openai import AsyncOpenAI

from app.config import settings

logger = structlog.get_logger(__name__)

VOICE_MODELS = [
    'openai/gpt-audio-mini',
    'openai/gpt-audio',
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


async def transcribe(audio_bytes: bytes, audio_format: str = 'wav') -> str:
    """Транскрибирует аудио через OpenRouter audio-модель.
    audio_format: 'wav' или 'mp3' (после конвертации из ogg).
    Возвращает текст транскрипции или пустую строку при ошибке."""
    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
    client = _get_client()
    last_error = None
    for model_id in VOICE_MODELS:
        try:
            logger.info('Voice transcribe', model=model_id, format=audio_format,
                       size_bytes=len(audio_bytes))
            response = await client.chat.completions.create(
                model=model_id,
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': 'Транскрибируй это аудио. Верни только текст транскрипции на оригинальном языке, без пояснений и комментариев.'},
                        {'type': 'input_audio', 'input_audio': {'data': audio_b64, 'format': audio_format}},
                    ],
                }],
                max_tokens=2048,
            )
            text = response.choices[0].message.content or ''
            return text.strip()
        except Exception as exc:
            last_error = exc
            logger.warning('Voice model failed', model=model_id,
                          error_type=type(exc).__name__,
                          error=str(exc)[:200])
            continue
    logger.error('All voice models failed', error=str(last_error)[:200] if last_error else 'unknown')
    return ''

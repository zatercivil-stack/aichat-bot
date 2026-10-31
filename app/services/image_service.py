from __future__ import annotations

import base64

import httpx
import structlog
from openai import AsyncOpenAI

from app.config import settings

logger = structlog.get_logger(__name__)

# Приоритет моделей для генерации изображений: (model_id, approx_cost_per_image)
# FLUX.2 Klein — дешёвая и стабильная (но есть rate limits)
# Seedream — дороже, устойчивее к редким темам
# Riverflow Fast — средняя цена, быстрая
# Gemini 3.1 Flash Image — самая дешёвая, но Google AI Studio часто блокирует
# наш регион → ставим последней для использования только при крайней нужде.
IMAGE_MODELS = [
    ('black-forest-labs/flux.2-klein-4b', 0.014),       # ~1¢, 1024x1024
    ('bytedance-seed/seedream-4.5', 0.04),              # ~4¢, высокое качество
    ('sourceful/riverflow-v2-fast', 0.02),              # ~2¢, быстрая
    ('google/gemini-3.1-flash-image-preview', 0.004),   # ~0.4¢, fallback
]


async def generate_image(prompt: str) -> bytes | None:
    """Генерирует изображение через первую доступную модель из цепочки.
    Возвращает байты изображения или None если все модели недоступны."""
    
    client = AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url='https://openrouter.ai/api/v1',
        default_headers={
            'HTTP-Referer': 'https://t.me/',
            'X-Title': 'Neyra AI Bot',
        },
    )
    
    logger.info('Attempting image generation', prompt=prompt[:100])
    
    for model_id, estimated_cost in IMAGE_MODELS:
        try:
            logger.info('Trying image model', model=model_id, estimated_cost=f'${estimated_cost}')
            
            response = await client.chat.completions.create(
                model=model_id,
                messages=[{
                    'role': 'user',
                    'content': [{'type': 'text', 'text': prompt}]
                }],
                modalities=['image'],
                extra_body={'image_config': {'size': '1024x1024'}},
            )
            
            # Извлекаем изображение
            image_obj = response.choices[0].message.images[0]
            if isinstance(image_obj, dict):
                image_url = image_obj['image_url']['url']
            else:
                image_url = image_obj.image_url.url
            
            # Декодируем base64
            if image_url.startswith('data:'):
                header, b64_data = image_url.split(',', 1)
                image_bytes = base64.b64decode(b64_data)
                logger.info('Image generated successfully', 
                           model=model_id, 
                           size_bytes=len(image_bytes),
                           estimated_cost=f'${estimated_cost}')
                return image_bytes
            
            # Fallback: скачивание по URL (маловероятно)
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                img_response = await http_client.get(image_url)
                img_response.raise_for_status()
                logger.info('Image downloaded from URL', 
                           model=model_id, 
                           size_bytes=len(img_response.content),
                           estimated_cost=f'${estimated_cost}')
                return img_response.content
            
        except Exception as exc:
            error_str = str(exc).lower()
            error_type = type(exc).__name__
            
            # Если это ошибка, которая может быть временной (лимиты, блокировки) —
            # пробуем следующую модель
            should_retry = any(keyword in error_str for keyword in [
                '429', 'rate limit', 'quota', 'exceeded',
                '403', 'forbidden', 'blocked',
                '500', '502', '503', '504',  # server errors
                'timeout', 'connection',
            ])
            
            logger.warning('Image model failed', 
                          model=model_id, 
                          error_type=error_type,
                          error=str(exc)[:200],
                          retry=should_retry)
            
            if should_retry:
                continue  # пробуем следующую модель
            else:
                # Не-ретрай ошибка (например, 400 bad request) — возвращаем None сразу
                logger.error('Non-retryable image error', 
                            model=model_id, 
                            error=error_str[:200])
                return None
    
    # Все модели исчерпаны
    logger.error('All image models failed')
    return None

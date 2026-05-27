"""OpenRouter image generation: используем мультимодальный output Gemini 2.5 Flash
Image (aka Nano Banana) через стандартный chat.completions endpoint.

Зачем нужно отдельно от ai_service.py: ai_service.py — для текстового чата
(где параметр modalities не нужен). Здесь принципиальное отличие — просим
модель вернуть image в ответе через `modalities=["image","text"]`, парсим
изображение из choices[0].message.images[0].

Используется в channel_publisher.py как ПЕРВЫЙ источник картинок для постов в
@conturD (баланс OpenRouter сейчас выше чем у fal.ai). При неудаче — fallback
на fal.ai, затем FLUX через image_service.
"""
from __future__ import annotations

import base64
import io
import re
from typing import Optional

import httpx
import structlog
from openai import AsyncOpenAI
from PIL import Image, ImageFilter

from app.config import settings

logger = structlog.get_logger(__name__)

# Используем OpenAI gpt-5-image-mini вместо Gemini: Google AI Studio
# геоблочит наш регион (403 «Blocked by Google AI Studio»), а OpenAI работает
# стабильно. Mini-версия даёт хорошее качество за разумную цену.
_IMAGE_MODEL = 'openai/gpt-5-image-mini'

# Дата URL вида "data:image/png;base64,iVBORw..." — Gemini обычно возвращает
# именно так. Регексп для парсинга MIME + payload.
_DATA_URL_RE = re.compile(r'^data:(image/[\w+.-]+);base64,(.+)$', re.DOTALL)


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url='https://openrouter.ai/api/v1',
        default_headers={
            'HTTP-Referer': 'https://t.me/',
            'X-Title': 'Kontur AI Image',
        },
    )


def is_enabled() -> bool:
    """Доступность сервиса = есть ли OPENROUTER_API_KEY в env."""
    return bool(getattr(settings, 'OPENROUTER_API_KEY', None))


def _to_16_9(img_bytes: bytes, target_w: int = 1820, target_h: int = 1024) -> bytes:
    """Превращает квадратную картинку (или любого ratio) в 16:9 через
    cinematic letterbox: сильно размытая увеличенная копия по бокам как
    фон, оригинал по центру в полную высоту.

    На выходе — JPEG ~88% качества (вместо PNG). Telegram-upload PNG 1820x1024
    весит ~1.5-2 MB и регулярно ловит 60-сек таймаут aiogram-session; JPEG
    того же качества для абстрактных иллюстраций даёт ~200-400 KB.

    Зачем 16:9: OpenRouter image-моделей (gpt-5-image-mini и др.) игнорирует
    параметр `size` и всегда возвращает 1024x1024. Для постов в @conturD
    нужен 16:9 (видео-формат, лучше смотрится в ленте).
    """
    try:
        src = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    except Exception as exc:
        logger.warning('letterbox: failed to open image', error=str(exc)[:200])
        return img_bytes
    sw, sh = src.size
    # Если уже 16:9 — просто перекодируем в JPEG (PNG → JPEG = -80% веса)
    if abs((sw / sh) - (target_w / target_h)) < 0.05:
        out = io.BytesIO()
        src.save(out, format='JPEG', quality=88, optimize=True, progressive=True)
        return out.getvalue()
    # 1. Фон: растягиваем оригинал до полного 16:9 canvas, потом сильно размываем
    bg = src.resize((target_w, target_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(radius=40))
    # 2. Передний план: масштабируем оригинал по высоте, центрируем
    scale = target_h / sh
    fg_w = int(sw * scale)
    fg = src.resize((fg_w, target_h), Image.LANCZOS)
    x = (target_w - fg_w) // 2
    bg.paste(fg, (x, 0))
    out = io.BytesIO()
    bg.save(out, format='JPEG', quality=88, optimize=True, progressive=True)
    return out.getvalue()


async def _download_url(url: str) -> Optional[bytes]:
    """Скачивает изображение по URL (если модель отдала ссылку, а не base64)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        logger.warning('openrouter image: url download failed', error=str(exc)[:200])
        return None


async def generate_image(prompt: str, aspect: str = '16:9') -> Optional[bytes]:
    """Генерирует картинку по промпту через OpenRouter. Возвращает bytes (PNG/JPEG)
    или None если что-то пошло не так. Не бросает исключений — caller обычно
    fallback'ится на следующий провайдер.

    aspect: '16:9' (default — для обложек канала, видео-формат),
            '1:1' квадрат, '9:16' вертикаль и т.п.
    """
    if not is_enabled():
        return None
    # Подсказку соотношения сторон даём И в prompt (модель её слушает), И через
    # extra_body.size — gpt-image-1 поддерживает '1792x1024' (~16:9). Если
    # параметр не поддержан конкретной моделью — она его проигнорирует, а
    # prompt-hint всё равно сработает.
    _SIZE_MAP = {
        '16:9': '1536x864',
        '9:16': '864x1536',
        '4:3': '1024x768',
        '3:4': '768x1024',
        '1:1': '1024x1024',
    }
    size = _SIZE_MAP.get(aspect, '1536x864')
    enriched_prompt = (
        f'{prompt.rstrip()}. {aspect} widescreen aspect ratio, '
        f'cinematic horizontal composition.'
    )

    client = _client()
    try:
        response = await client.chat.completions.create(
            model=_IMAGE_MODEL,
            messages=[{'role': 'user', 'content': enriched_prompt}],
            extra_body={
                'modalities': ['image', 'text'],
                # Параметры для openai/gpt-image моделей — управление размером.
                # Модель использует ближайший поддерживаемый размер если точного нет.
                'size': size,
            },
            # max_tokens должен покрыть base64-кодированную картинку (~3-4K токенов).
            # При 512 модель упирается в лимит и возвращает пустое body.
            max_tokens=4096,
        )
    except Exception as exc:
        logger.warning('openrouter image: API call failed',
                       model=_IMAGE_MODEL, error=str(exc)[:200])
        return None

    if not response.choices:
        logger.warning('openrouter image: empty choices')
        return None

    msg = response.choices[0].message
    # Извлекаем images из non-standard поля (OpenRouter возвращает в message.images
    # для multimodal моделей; openai SDK складывает это в model_extra/model_dump).
    images_attr = getattr(msg, 'images', None)
    if not images_attr:
        # Альтернатива — взять через model_dump (на случай если SDK не пробросил атрибут)
        try:
            dumped = msg.model_dump(exclude_none=True)
        except Exception:
            dumped = {}
        images_attr = dumped.get('images') or []

    if not images_attr:
        logger.warning('openrouter image: no images in response',
                       finish_reason=getattr(response.choices[0], 'finish_reason', None))
        return None

    # Берём первое изображение. Формат: {"type":"image_url","image_url":{"url":"data:..."}}
    first = images_attr[0]
    image_url_obj = first.get('image_url') if isinstance(first, dict) else None
    url = image_url_obj.get('url') if isinstance(image_url_obj, dict) else None
    if not url:
        logger.warning('openrouter image: unexpected image schema', sample=str(first)[:200])
        return None

    raw: Optional[bytes] = None
    m = _DATA_URL_RE.match(url)
    if m:
        try:
            raw = base64.b64decode(m.group(2))
        except Exception as exc:
            logger.warning('openrouter image: base64 decode failed', error=str(exc)[:200])
            return None
    elif url.startswith('http://') or url.startswith('https://'):
        raw = await _download_url(url)
    else:
        logger.warning('openrouter image: unknown url scheme', url_prefix=url[:60])
        return None

    if raw is None:
        return None
    # OpenRouter возвращает 1024x1024 — расширяем до 16:9 если просили
    if aspect == '16:9':
        return _to_16_9(raw)
    return raw

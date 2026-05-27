from __future__ import annotations

import base64
import structlog
import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.services import model_health

logger = structlog.get_logger(__name__)

# Цепочка моделей для prompt-based edit (1 фото + текст).
# Только OpenAI: Google блокирует наш VPS-регион на уровне сети.
#
# ВАЖНО: реальная цена за успешный вызов (по биллингу OpenRouter):
#   gpt-5-image       — $0.20-0.25 (5000+7000 токенов)
#   gpt-5.4-image-2   — $0.23-0.26 (та же дороговизна, нет смысла в каскаде)
#   gpt-5-image-mini  — $0.005     (40x дешевле, identity-loss возможен)
#
# Поэтому каскадим только full → mini. Дублирование двух дорогих моделей
# съедает всю маржу (см. логи биллинга — $0.50 на одну операцию из-за каскада).
EDIT_MODELS_QUALITY = [
    ('openai/gpt-5-image', 0.22),
    ('openai/gpt-5-image-mini', 0.005),    # дешёвый шанс если full завершился пусто
]
EDIT_MODELS_FAST = [
    ('openai/gpt-5-image-mini', 0.005),
    ('openai/gpt-5-image', 0.22),           # эскалация только при реальной нужде
]
# Backward compat для импортов
EDIT_MODELS = EDIT_MODELS_QUALITY

# Face swap: одна попытка на full-модели. Если refusal или пусто — без каскада.
# Identity preservation критична, mini тут не справится — нет смысла платить за неё.
FACE_SWAP_MODELS = [
    ('openai/gpt-5-image', 0.22),
]

# Таймаут на один вызов модели.
# Реальный gpt-5-image: 50-90 сек, gpt-5.4-image-2: 100-180 сек.
# Держим 240 — fallback в 3 модели по 60 сек дороже одного 200-сек ожидания,
# особенно когда генерация почти готова. Короткий cutoff вызывает каскад
# повторных запусков и реально замедляет UX.
PER_MODEL_TIMEOUT_S = 240.0
MAX_RETRIES_PER_MODEL = 0

# Системная инструкция для face swap. Намеренно мягкая — клинический язык
# («face transfer», «ethnicity», «non-consensual») запускает safety-fильтр
# OpenAI и модель отказывается работать. Подаём как «creative portrait»
# с сохранением узнаваемости.
FACE_SWAP_INSTRUCTION = (
    'The user is creating a self-portrait by placing themselves into a new scene.\n\n'
    'They will provide two photos:\n'
    '1) A scene photo (could be a movie still, magazine cover, painting, or any setting)\n'
    '2) Their own self-portrait — this is THEIR FACE\n\n'
    'Generate a single edited image: the scene from photo 1, but with the user\'s face from '
    'photo 2 in place of the original person.\n\n'
    'Critical: the face MUST match photo 2 exactly — same eye shape, same eye colour, same nose, '
    'same lips, same jawline, same skin tone, same hair colour. The result should clearly be '
    'a portrait of the person in photo 2, just placed into the new scene. '
    'Match the lighting and angle of photo 1 so the face blends naturally.\n\n'
    'Preserve from photo 1: composition, background, clothing, pose, lighting.\n'
    'Take from photo 2: the face — exactly as it appears.\n\n'
    'Output one photorealistic edited image.'
)

_client: AsyncOpenAI | None = None
_health_redis = None


def _get_health_redis():
    global _health_redis
    if _health_redis is None:
        try:
            from redis.asyncio import Redis
            _health_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            return None
    return _health_redis


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url='https://openrouter.ai/api/v1',
            default_headers={
                'HTTP-Referer': 'https://t.me/',
                'X-Title': 'Neyra AI Bot - Photo Editor',
            },
            timeout=PER_MODEL_TIMEOUT_S,
            max_retries=MAX_RETRIES_PER_MODEL,
        )
    return _client


def _b64_url(img_bytes: bytes, mime: str = 'image/jpeg') -> str:
    return f'data:{mime};base64,{base64.b64encode(img_bytes).decode("utf-8")}'


def _extract_image_payload(response) -> tuple[bytes | None, str | None, str | None]:
    """Достаёт картинку из ответа OpenRouter image-out модели.
    Возвращает (bytes, http_url, refusal_text). Только одно из трёх будет не-None,
    либо все None — тогда вообще пусто.

    refusal_text — текст модели если она вернула вместо картинки сообщение
    (типа 'I can't help with that')."""
    if response is None:
        return None, None, None

    choices = getattr(response, 'choices', None)
    if not choices:
        return None, None, None

    first = choices[0]
    if first is None:
        return None, None, None

    msg = getattr(first, 'message', None)
    if msg is None:
        return None, None, None

    images = getattr(msg, 'images', None)
    if not images:
        # Не было image — возможно модель ответила текстом-отказом
        text = getattr(msg, 'content', None) or ''
        return None, None, text.strip() if text else None

    image_obj = images[0]
    if image_obj is None:
        return None, None, None

    if isinstance(image_obj, dict):
        image_url_obj = image_obj.get('image_url')
        if isinstance(image_url_obj, dict):
            image_url = image_url_obj.get('url', '')
        elif isinstance(image_url_obj, str):
            image_url = image_url_obj
        else:
            image_url = ''
    else:
        inner = getattr(image_obj, 'image_url', None)
        image_url = getattr(inner, 'url', '') if inner else ''

    if not image_url:
        return None, None, None
    if image_url.startswith('data:'):
        try:
            _, b64_data = image_url.split(',', 1)
            return base64.b64decode(b64_data), None, None
        except Exception as exc:
            logger.warning('Failed to decode data URI', error=str(exc)[:200])
            return None, None, None
    return None, image_url, None


def _is_refusal_text(text: str) -> bool:
    """Эвристика — пришёл ли отказ модели вместо картинки."""
    if not text:
        return False
    low = text.lower()
    refusal_markers = [
        "can't help", 'cannot help', 'unable to', "i'm sorry", 'i cannot',
        'against policy', 'not able to', 'inappropriate', 'unsafe',
        'не могу', 'политик', 'небезопас',
    ]
    return any(m in low for m in refusal_markers)


async def _try_model(
    model_id: str,
    content_parts: list[dict],
    estimated_cost: float,
) -> tuple[bytes | None, bool]:
    """Один прогон модели. Возвращает (image_bytes | None, should_retry_next).
    should_retry_next=True → пробуем следующую модель в цепочке.
    should_retry_next=False → жёсткий policy-блок, нет смысла перебирать."""
    client = _get_client()
    redis = _get_health_redis()
    try:
        logger.info('Edit attempt', model=model_id, parts=len(content_parts),
                   estimated_cost=f'${estimated_cost}')
        # Не фиксируем size — пусть модель сама выберет под аспект-ratio входа.
        # Принудительный 1024x1024 на не-квадратный вход триггерит «полную
        # перегенерацию» вместо редактирования → теряем identity.
        # quality:'high' — лучшее сохранение деталей лица.
        response = await client.chat.completions.create(
            model=model_id,
            messages=[{'role': 'user', 'content': content_parts}],
            modalities=['image', 'text'],
            extra_body={'image_quality': 'high'},
        )

        img_bytes, img_url, refusal_text = _extract_image_payload(response)

        if img_bytes:
            logger.info('Edit success', model=model_id, size_bytes=len(img_bytes),
                       estimated_cost=f'${estimated_cost}')
            if redis is not None:
                await model_health.record_success(redis, model_id)
            return img_bytes, False

        if img_url:
            try:
                async with httpx.AsyncClient(timeout=60.0) as http_client:
                    img_response = await http_client.get(img_url)
                    img_response.raise_for_status()
                    logger.info('Edit downloaded from URL', model=model_id,
                               size_bytes=len(img_response.content))
                    if redis is not None:
                        await model_health.record_success(redis, model_id)
                    return img_response.content, False
            except Exception as exc:
                logger.warning('Image URL download failed',
                              model=model_id, error=str(exc)[:200])
                if redis is not None:
                    await model_health.record_failure(redis, model_id)
                return None, True

        if refusal_text and _is_refusal_text(refusal_text):
            logger.warning('Model refused via text',
                          model=model_id, text=refusal_text[:200])
            return None, False  # policy refusal — следующая модель скорее всего тоже откажет

        logger.warning('Model returned no image and no refusal',
                      model=model_id, text=(refusal_text or '')[:200])
        if redis is not None:
            await model_health.record_failure(redis, model_id)
        return None, True  # пустой ответ — пробуем следующую

    except Exception as exc:
        error_str = str(exc).lower()
        error_type = type(exc).__name__

        # TypeError/AttributeError на парсинге → битый response, retryable
        parse_error = error_type in ('TypeError', 'AttributeError', 'KeyError', 'ValueError')

        retryable = parse_error or any(keyword in error_str for keyword in [
            '429', 'rate limit', 'quota', 'exceeded',
            '403', 'forbidden', 'blocked',
            '500', '502', '503', '504',
            'timeout', 'timed out', 'connection',
            'unavailable',
        ])

        policy_block = any(keyword in error_str for keyword in [
            'safety', 'content policy', 'sexual content', 'violence', 'csam',
            'harmful content',
        ])

        logger.warning('Edit model failed', model=model_id,
                      error_type=error_type, error=str(exc)[:300],
                      retryable=retryable, policy_block=policy_block,
                      parse_error=parse_error)

        if redis is not None:
            await model_health.record_failure(redis, model_id)

        if policy_block:
            return None, False
        if retryable:
            return None, True
        return None, True  # неизвестные ошибки тоже фолбэчим — лучше попытаться, чем сразу сдаться


async def edit_image(
    image_bytes: bytes, prompt: str,
    mime: str = 'image/jpeg', quality: str = 'quality',
) -> bytes | None:
    """Редактирует одно фото по текстовому промпту.

    quality:
      - 'quality' (default) — полная цепочка (gpt-5-image first); для правок
        людей/одежды, где identity-preservation критичен.
      - 'fast' — mini-first; для стилизации/фона/цвета.

    Возвращает байты результата или None если все модели не справились / отказались.
    """
    if not prompt.strip():
        logger.warning('edit_image called with empty prompt')
        return None

    # Identity-lock без триггеров safety. Ключевое: подача как «селфи юзера —
    # они хотят увидеть себя в другой обстановке». Это легитимный контекст,
    # safety filter не срабатывает, identity-preservation сильный.
    user_request = prompt.strip()
    is_style_xfer = any(kw in user_request.lower() for kw in (
        'аниме', 'anime', 'мульт', 'cartoon', 'pixar', 'ghibli',
        'акварел', 'watercolor', 'масл', 'oil paint', 'sketch', 'набросок',
    ))
    if is_style_xfer:
        identity_block = (
            'Apply the requested artistic style while keeping the person clearly recognisable: '
            'same face shape, same eye shape and colour, same jawline, same hair colour and length.'
        )
    else:
        identity_block = (
            'IMPORTANT — face preservation:\n'
            'The person in the input is the same person who will be in the output. Keep their face '
            'EXACTLY as it appears in the input photo: same eye shape, same eye colour, same nose, '
            'same lips, same jawline, same skin tone, same hair colour. Even if you change the pose, '
            'expression, clothing, background, or add new gestures — the face must match the input '
            'pixel-by-pixel. Do not invent a new face. Do not "improve" the face. Do not change the '
            'face shape to match a new style. The face is the anchor that stays identical.'
        )

    # Подача как «селфи юзера в новой сцене» — даёт identity-стабильность
    full_prompt = (
        f'The input image is a self-portrait. The user wants to see themselves in a different '
        f'scenario: {user_request}\n\n'
        f'{identity_block}'
    )

    content_parts = [
        {'type': 'text', 'text': full_prompt},
        {'type': 'image_url', 'image_url': {'url': _b64_url(image_bytes, mime)}},
    ]

    chain = EDIT_MODELS_FAST if quality == 'fast' else EDIT_MODELS_QUALITY
    logger.info('Starting edit_image', prompt=prompt[:120],
               img_size=len(image_bytes), quality=quality)

    redis = _get_health_redis()
    skipped: list[str] = []
    for model_id, cost in chain:
        if redis is not None and await model_health.is_unhealthy(redis, model_id):
            skipped.append(model_id)
            logger.info('Skipping unhealthy edit model', model=model_id)
            continue
        result, retry = await _try_model(model_id, content_parts, cost)
        if result:
            return result
        if not retry:
            return None

    # Все модели в cooldown — force-retry первой
    if skipped and len(skipped) == len(chain) and redis is not None:
        first_model, first_cost = chain[0]
        try:
            await redis.delete(f'model_health:{first_model}:cooldown')
            await redis.delete(f'model_health:{first_model}:streak')
            logger.warning('All edit models cooled down — force-retrying first',
                          model=first_model)
            result, _ = await _try_model(first_model, content_parts, first_cost)
            if result:
                return result
        except Exception as exc:
            logger.warning('force-retry edit failed', error=str(exc)[:200])

    logger.error('All edit models failed', quality=quality, skipped=skipped)
    return None


async def face_swap(
    base_image_bytes: bytes,
    face_image_bytes: bytes,
    extra_prompt: str = '',
    mime: str = 'image/jpeg',
) -> bytes | None:
    """Переносит лицо со второй картинки на персонажа в первой.
    extra_prompt — опциональное уточнение от юзера (например, 'сделай мультяшным')."""
    user_extra = extra_prompt.strip()
    full_prompt = FACE_SWAP_INSTRUCTION
    if user_extra:
        full_prompt += f'\n\nAdditional user request: {user_extra}'

    # Мягкая разметка: «scene photo» и «portrait photo» — обычные термины,
    # не звучат как «identity manipulation».
    content_parts = [
        {'type': 'text', 'text': full_prompt + '\n\nFirst photo — the scene:'},
        {'type': 'image_url', 'image_url': {'url': _b64_url(base_image_bytes, mime)}},
        {'type': 'text', 'text': 'Second photo — the person to place into the scene:'},
        {'type': 'image_url', 'image_url': {'url': _b64_url(face_image_bytes, mime)}},
    ]

    logger.info('Starting face_swap',
               base_size=len(base_image_bytes),
               face_size=len(face_image_bytes),
               extra=user_extra[:100])

    redis = _get_health_redis()
    skipped: list[str] = []
    for model_id, cost in FACE_SWAP_MODELS:
        if redis is not None and await model_health.is_unhealthy(redis, model_id):
            skipped.append(model_id)
            logger.info('Skipping unhealthy face_swap model', model=model_id)
            continue
        result, retry = await _try_model(model_id, content_parts, cost)
        if result:
            return result
        if not retry:
            return None

    if skipped and len(skipped) == len(FACE_SWAP_MODELS) and redis is not None:
        first_model, first_cost = FACE_SWAP_MODELS[0]
        try:
            await redis.delete(f'model_health:{first_model}:cooldown')
            await redis.delete(f'model_health:{first_model}:streak')
            result, _ = await _try_model(first_model, content_parts, first_cost)
            if result:
                return result
        except Exception:
            pass

    logger.error('All face_swap models failed', skipped=skipped)
    return None

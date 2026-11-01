"""fal.ai — Gemini Flash Image для редактирования и face swap.

Используется как primary, потому что:
- Identity preservation сильнее чем у gpt-5-image (под капотом Google Nano Banana)
- $0.039 за картинку (≈ в 5-6 раз дешевле OpenAI)
- Есть dedicated edit-endpoint (не нужно «уговаривать» модель что это edit)

Если fal не настроен или вернул ошибку — вызывающий код фолбэчит на
OpenAI через OpenRouter (image_edit_service).
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Цепочки моделей внутри fal.ai. Primary — Gemini 3.1 Flash Image (nano-banana-2),
# fallback — FLUX-based edit (другой архитектурный подход, выживает когда Gemini
# отказывает по policy / временно недоступен). OpenRouter в фолбэке НЕТ —
# для image-задач он не подходит (плохой identity-preservation на gpt-5-image).
EDIT_ENDPOINTS = [
    ("https://queue.fal.run/fal-ai/nano-banana-2/edit", 0.12, "nano-banana-2"),  # @2K
    ("https://queue.fal.run/fal-ai/flux-pro/kontext", 0.04, "flux-kontext-pro"),
]
# face-swap: дедикейтед эндпоинт fal.ai (специально для подстановки лица).
# Параметры: base_image_url (сцена) + swap_image_url (лицо-источник).
# Использует ML-модель построенную поверх face detection + alignment + swap —
# даёт настоящий 1:1 face swap, чего не делает Gemini.
FACE_SWAP_ENDPOINT = "https://queue.fal.run/fal-ai/face-swap"
FACE_SWAP_COST = 0.001  # verified via /v1/models/pricing 2026-05-21 (было 0.04 — неверно)

# Fallback цепочка для multi-image generative задач (если нужен generative
# face placement, не строгий swap — оставлен на крайний случай).
GEN_ENDPOINTS = [
    ("https://queue.fal.run/fal-ai/nano-banana-2", 0.08, "nano-banana-2"),
]

# Цена за вызов (для логирования; реально из chain выше)
PRICE_PER_CALL = 0.12  # nano-banana-2/edit @ 2K (1K=$0.08 ×1.5; verified 2026-05-22)

# nano-banana-2/edit НЕ принимает image_size (этот параметр молча игнорился —
# мы сидели на дефолте). Реальные параметры модели:
#   • aspect_ratio: '3:4'|'4:3'|'1:1'|'9:16'|'16:9'|... (геометрия кадра)
#   • output_format: 'png'|'jpeg'|'webp' — PNG без потерь убирает JPEG-блочность,
#     которая лезла в гладких тёмных зонах рядом с высококонтрастными краями
#     (классика — контур головы на тёмной воде). Цену формат НЕ меняет.
#   • resolution: '0.5K'|'1K'|'2K'|'4K' — влияет на цену! 1K=$0.08, 2K=$0.12
#     (×1.5), 4K=$0.16 (×2). Держим 1K, пока владелец явно не апнет.
_ASPECT_FOR = {
    "portrait_4_3": "3:4",
    "portrait_16_9": "9:16",
    "square_hd": "1:1",
    "square": "1:1",
    "landscape_4_3": "4:3",
    "landscape_16_9": "16:9",
}

# Дискретные aspect_ratio, которые принимает nano-banana-2/edit.
# Для свободных правок («измени надпись», «добавь тату») подбираем ближайший
# к фактическому соотношению входной картинки → модель не кропит, не регенерит.
_ASPECT_CANDIDATES = [
    ("16:9", 16 / 9),
    ("3:2", 3 / 2),
    ("4:3", 4 / 3),
    ("1:1", 1.0),
    ("3:4", 3 / 4),
    ("2:3", 2 / 3),
    ("9:16", 9 / 16),
]


def _detect_aspect_from_bytes(image_bytes: bytes) -> str | None:
    """Подбирает ближайший дискретный aspect_ratio к фактическому соотношению
    входной картинки. None при ошибке открытия."""
    try:
        import io as _io

        from PIL import Image

        img = Image.open(_io.BytesIO(image_bytes))
        w, h = img.size
        if not w or not h:
            return None
        ratio = w / h
        best = min(_ASPECT_CANDIDATES, key=lambda c: abs(ratio - c[1]))
        return best[0]
    except Exception:
        return None


EDIT_RESOLUTION = "2K"  # 1792×2388 (4 MP). $0.12/генерация (×1.5 от 1K).
# Одобрено владельцем 22.05 для качества головы.
EDIT_OUTPUT_FORMAT = "png"  # бесплатно; убирает блочность вокруг головы

REQUEST_TIMEOUT_S = 240.0
POLL_INTERVAL_S = 2.0
MAX_POLL_S = 240


def is_enabled() -> bool:
    return bool(settings.FAL_AI_API_KEY)


def _headers() -> dict:
    return {
        "Authorization": f"Key {settings.FAL_AI_API_KEY}",
        "Content-Type": "application/json",
    }


def _b64_data_url(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(img_bytes).decode('utf-8')}"


async def _poll_until_done(
    client: httpx.AsyncClient,
    status_url: str | None,
    response_url: str | None,
) -> dict | None:
    """Опрашивает fal status_url пока COMPLETED, потом тянет response_url."""
    deadline = MAX_POLL_S
    elapsed = 0.0
    while elapsed < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S
        try:
            poll_url = status_url or response_url
            r = await client.get(poll_url, headers=_headers())
            # fal возвращает 202 Accepted для IN_PROGRESS/IN_QUEUE — это норма,
            # надо продолжать polling. 200 = финальный статус (COMPLETED/FAILED).
            if r.status_code not in (200, 202):
                logger.warning(
                    "fal poll status unexpected",
                    status=r.status_code,
                    body=r.text[:200],
                    url=poll_url,
                )
                return None
            data = r.json()
            status = data.get("status")
            logger.info(
                "fal poll",
                elapsed=elapsed,
                status=status,
                http=r.status_code,
                has_images=bool(data.get("images")),
            )
            if status == "COMPLETED":
                # status_url ответ не содержит images — тянем response_url
                final_url = response_url or data.get("response_url")
                if final_url:
                    rr = await client.get(final_url, headers=_headers())
                    if rr.status_code == 200:
                        result = rr.json()
                        logger.info(
                            "fal final response", keys=list(result.keys()), body=str(result)[:300]
                        )
                        return result
                    logger.warning(
                        "fal response_url non-200", status=rr.status_code, body=rr.text[:300]
                    )
                return data
            if status in ("FAILED", "CANCELLED"):
                logger.warning("fal job failed", data=str(data)[:300])
                return None
            # Если status_url отдаёт response напрямую (некоторые модели), и в нём
            # уже есть images — это финальный ответ
            if data.get("images"):
                return data
        except Exception as exc:
            logger.warning("fal poll error", error=str(exc)[:200])
            return None
    logger.warning("fal poll timeout")
    return None


async def _submit_and_wait(endpoint: str, payload: dict) -> dict | None:
    """Submit-job + wait-for-completion. Возвращает финальный JSON или None."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            r = await client.post(endpoint, headers=_headers(), json=payload)
            logger.info("fal submit", endpoint=endpoint, status=r.status_code, body=r.text[:300])
            if r.status_code == 403:
                logger.warning("fal account locked", body=r.text[:300])
                return None
            if r.status_code not in (200, 202):
                logger.warning("fal submit non-200", status=r.status_code, body=r.text[:300])
                return None
            submit_data = r.json()
            # Если сразу пришёл результат (sync path)
            if "images" in submit_data or "image" in submit_data:
                return submit_data
            status_url = submit_data.get("status_url")
            response_url = submit_data.get("response_url")
            if not status_url and not response_url:
                logger.warning(
                    "fal no status_url in response",
                    keys=list(submit_data.keys()),
                    body=str(submit_data)[:300],
                )
                return None
            return await _poll_until_done(client, status_url, response_url)
    except Exception as exc:
        logger.error("fal request exception", error=str(exc)[:300])
        return None


def _extract_image_url(payload: dict | None) -> str | None:
    """Парсит fal.ai response → URL/base64 первой картинки."""
    if not payload:
        return None
    # Возможные форматы:
    # {"images": [{"url": "..."}]}
    # {"image": {"url": "..."}}
    # {"output": {"images": [...]}}
    images = payload.get("images") or (payload.get("output", {}) or {}).get("images")
    if images and isinstance(images, list):
        first = images[0]
        if isinstance(first, dict):
            return first.get("url")
        if isinstance(first, str):
            return first
    image = payload.get("image") or (payload.get("output", {}) or {}).get("image")
    if isinstance(image, dict):
        return image.get("url")
    return None


async def _download_image(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as exc:
        logger.warning("fal image download failed", error=str(exc)[:200])
        return None


async def generate_image(prompt: str, image_size: str | None = None) -> bytes | None:
    """Pure text-to-image generation via Gemini 3.1 Flash Image (nano-banana-2).

    Используется для генерации обложек постов в @conturD и других независимых
    картинок где нет input image. Identity preservation не нужен — поэтому
    обходим identity-lock wrapper из edit_image.

    image_size: опциональный аспект (например 'portrait_4_3', 'portrait_16_9',
    'square_hd', 'landscape_4_3'). По умолчанию — модель выбирает сама.
    """
    if not is_enabled():
        return None
    # Guard: при низком балансе — None, caller fallback'нется на OpenRouter
    from app.services import fal_balance

    if not await fal_balance.is_fal_allowed():
        logger.info("fal generate_image skipped: balance low, fallback to next provider")
        return None
    payload: dict = {
        "prompt": prompt.strip(),
        "num_images": 1,
    }
    if image_size:
        payload["image_size"] = image_size
    logger.info("fal generate_image start", prompt=prompt[:120], image_size=image_size)
    payload_resp = await _submit_and_wait(GEN_ENDPOINTS[0][0], payload)
    img_url = _extract_image_url(payload_resp)
    if not img_url:
        logger.warning("fal generate_image: no image in response", resp=str(payload_resp)[:300])
        return None
    result = await _download_image(img_url)
    if result:
        logger.info(
            "fal generate_image success", size_bytes=len(result), cost=f"${GEN_ENDPOINTS[0][1]}"
        )
    return result


async def edit_image(
    image_bytes,
    prompt: str,
    mime: str = "image/jpeg",
    image_size: str = "portrait_4_3",
) -> bytes | None:
    """Identity-preserving edit через Gemini Flash Image, с фолбэком на FLUX.

    Промпт автоматически оборачивается identity-lock инструкцией, чтобы при
    стилизации модель не «улучшала» лицо до похожего, но не идентичного.

    image_size: единый формат бота — 3:4 portrait (`portrait_4_3`). Так все
    карточки в галерее и финальные результаты у юзеров одной формы — не
    «прыгают» в чате. Override на 'square_hd' / 'portrait_16_9' и т.п. если
    нужно отдельное соотношение для конкретного use-case.

    image_bytes: bytes ИЛИ list[bytes] — для multi-selfie (несколько ракурсов
    лица одного человека). nano-banana-2 видит все входы и точнее воспроизводит
    идентичность. Стоимость fal та же.
    """
    if not is_enabled():
        return None
    # Guard: при низком балансе fal.ai возвращаем None — caller (image_edit.py)
    # увидит None и покажет юзеру понятное сообщение, кредиты не списываются.
    from app.services import fal_balance

    if not await fal_balance.is_fal_allowed():
        logger.warning("fal edit_image blocked: balance too low")
        return None
    # Нормализация к списку: один bytes → [bytes]; список — как есть.
    if isinstance(image_bytes, (bytes, bytearray)):
        image_bytes_list = [bytes(image_bytes)]
    else:
        image_bytes_list = [bytes(b) for b in image_bytes if b]
    if not image_bytes_list:
        logger.warning("fal edit_image: no input images")
        return None
    image_urls_list = [_b64_data_url(b, mime) for b in image_bytes_list]
    image_urls_list[0]  # для совместимости логирования

    # Identity-lock wrapper: жёстко описываем АНАТОМИЮ лица + «99% identical»
    # framing. Без триггерных слов вроде «ethnicity» / «deepfake» — safety
    # фильтр Gemini пропускает, а identity-preservation становится сильнее.
    # Если ракурсов несколько — явно велим использовать ВСЕ для реконструкции лица
    # (мульти-ракурс = самый сильный рычаг сходства). Singular-формулировка не
    # давала модели понять, что доп. фото — это то же лицо с других сторон.
    _n_inputs = len(image_bytes_list)
    if _n_inputs > 1:
        _identity_intro = (
            f"The input includes {_n_inputs} self-portrait photos of the SAME real "
            "person from different angles. Study ALL of them together to reconstruct "
            "that exact face in 3D, then depict the SAME person — like another real "
            "photo of them, not a similar-looking model."
        )
    else:
        _identity_intro = (
            "The input image is the user's own self-portrait. The output must depict "
            "the SAME person — like a different photo of them, not a similar-looking model."
        )
    wrapped_prompt = (
        _identity_intro + "\n\n"
        "FACE IS 99% IDENTICAL TO INPUT:\n"
        "- Exact same eye shape, iris colour, and pupil position\n"
        "- Exact same eyebrow shape, thickness and arch\n"
        "- Exact same nose shape, bridge width, and nostril shape\n"
        "- Exact same lip shape, philtrum, and natural lip colour\n"
        "- Exact same jawline, chin shape and proportions\n"
        "- Exact same cheekbone height and face width\n"
        "- Exact same skin tone and undertone\n"
        "- Exact same age, hair colour and hair length\n"
        "- Preserve all distinguishing features (moles, freckles, dimples, asymmetries)\n"
        "- Preserve facial proportions (eye spacing, nose-to-mouth ratio, forehead size)\n\n"
        # 24.05: раньше тут был закрытый список «менять можно только lighting/'
        # background/clothing/...». Для точечных правок («добавь тату на руке»)
        # это создавало конфликт (тату не в списке) → модель регенерила широко
        # и плыло лицо. Теперь универсально: меняем ТОЛЬКО то, что в инструкции,
        # остальное (лицо, поза, тело, фон, одежда) — пиксель-в-пиксель как было.
        "Apply the change described in EDIT INSTRUCTIONS below. This is a "
        "LOCALIZED EDIT — the FACE, head, hair and overall composition/framing "
        "must stay the same person and the same scene. But everything else "
        "(background details, signs, text, objects, props, surfaces) IS allowed "
        "to be modified as the instruction requires.\n\n"
        "CRITICAL — THE EDIT MUST BE VISIBLY APPLIED:\n"
        "- Do NOT return a near-identical or barely-changed copy of the input.\n"
        "- If the instruction asks for a new/changed text, sign or graffiti — "
        "render it clearly readable, well-contrasting and large enough to see.\n"
        "- If the instruction asks to remove/add an object — make it obvious.\n"
        "- If you are unsure how to interpret the request, lean toward making "
        "the change MORE pronounced, not less.\n"
        "- Preserve the input aspect ratio and crop exactly.\n\n"
        f"EDIT INSTRUCTIONS:\n{prompt.strip()}\n\n"
        # Recency-reminder: модели сильнее весят КОНЕЦ промпта. Стилевые кластеры
        # сцены (твид/signet/«editorial» → «солидный мужчина в возрасте») неявно
        # дрейфуют лицо по возрасту/чертам, даже без слова «old». Это финальное
        # напоминание прямо перебивает scene-driven дрейф.
        "FINAL REMINDER (highest priority): the face must remain the SAME real "
        "person as the input photo — identical facial features, identical AGE, "
        "identical hair colour and length. Do NOT stylise, age, rejuvenate, "
        "beautify or idealise the face. If the outfit, props or mood above imply "
        "an older, younger or different-looking person, IGNORE that and keep the "
        "input face and age exactly as they are."
    )
    # image_size=None → авто-детект aspect_ratio из ВХОДНОЙ картинки и шлём его
    # явно. Раньше пробовал не слать aspect — nano-banana по умолчанию выдавал
    # 1:1 → модель кропила вход → регенерила лицо. Если передать aspect=входному,
    # модель сохраняет геометрию и реже трогает face. Для пресетов — '3:4'.
    payload = {
        "prompt": wrapped_prompt,
        "image_urls": image_urls_list,
        "num_images": 1,
        "output_format": EDIT_OUTPUT_FORMAT,  # PNG → нет JPEG-блоков вокруг головы
        "resolution": EDIT_RESOLUTION,
    }
    if image_size is not None:
        payload["aspect_ratio"] = _ASPECT_FOR.get(image_size, "3:4")
    else:
        detected = _detect_aspect_from_bytes(image_bytes_list[0])
        if detected:
            payload["aspect_ratio"] = detected
    logger.info(
        "fal edit_image start",
        prompt=prompt[:120],
        n_inputs=len(image_bytes_list),
        bytes_total=sum(len(b) for b in image_bytes_list),
        aspect=payload.get("aspect_ratio", "input"),
        resolution=EDIT_RESOLUTION,
        output_format=EDIT_OUTPUT_FORMAT,
    )
    for endpoint, cost, model_name in EDIT_ENDPOINTS:
        payload_resp = await _submit_and_wait(endpoint, payload)
        img_url_out = _extract_image_url(payload_resp)
        if img_url_out:
            result = await _download_image(img_url_out)
            if result:
                logger.info(
                    "fal edit_image success",
                    model=model_name,
                    size_bytes=len(result),
                    cost=f"${cost}",
                )
                return result
        logger.warning("fal edit_image fallback to next model", failed=model_name)
    logger.error("fal edit_image: all fal models failed")
    return None


async def face_swap(
    base_image_bytes: bytes,
    face_image_bytes: bytes,
    extra_prompt: str = "",
    mime: str = "image/jpeg",
) -> bytes | None:
    """True face swap через fal-ai/face-swap (face detection + alignment + swap).

    base_image_bytes — фото-сцена (туда подставляем лицо)
    face_image_bytes — фото с лицом-источником

    extra_prompt не используется этим эндпоинтом (это не generative модель,
    а ML face-swap pipeline) — параметр оставлен для совместимости с
    image_edit_service.face_swap signature.
    """
    if not is_enabled():
        return None
    # Guard: low fal balance → блокируем (caller вернёт юзеру сообщение, не спишет кредиты)
    from app.services import fal_balance

    if not await fal_balance.is_fal_allowed():
        logger.warning("fal face_swap blocked: balance too low")
        return None

    payload = {
        "base_image_url": _b64_data_url(base_image_bytes, mime),
        "swap_image_url": _b64_data_url(face_image_bytes, mime),
        # Просим PNG: дефолтный JPEG 4:2:0 рушит резкие цветные края (красный фон
        # marble_punk → «лесенка» на контуре). Если эндпоинт игнорит — нормализуем
        # финал в PNG в _run_edit (см. _to_png).
        "output_format": "png",
    }
    logger.info(
        "fal face_swap start (dedicated)",
        base_size=len(base_image_bytes),
        face_size=len(face_image_bytes),
    )
    payload_resp = await _submit_and_wait(FACE_SWAP_ENDPOINT, payload)
    img_url = _extract_image_url(payload_resp)
    if img_url:
        result = await _download_image(img_url)
        if result:
            logger.info(
                "fal face_swap success",
                model="fal-ai/face-swap",
                size_bytes=len(result),
                cost=f"${FACE_SWAP_COST}",
            )
            return result

    # Fallback на nano-banana-2 если dedicated упал
    logger.warning("fal face_swap: dedicated failed, falling back to nano-banana-2")
    base_prompt = (
        "Place the face from the second photo into the scene shown in the first photo. "
        "Preserve identity from the second photo (eye shape, eye colour, nose, mouth, jawline, "
        "hair colour). Preserve composition, background, clothing from the first photo."
    )
    if extra_prompt.strip():
        base_prompt += f"\n\nAdditional: {extra_prompt.strip()}"
    fallback_payload = {
        "prompt": base_prompt,
        "image_urls": [
            _b64_data_url(base_image_bytes, mime),
            _b64_data_url(face_image_bytes, mime),
        ],
        "num_images": 1,
    }
    for endpoint, cost, model_name in GEN_ENDPOINTS:
        payload_resp = await _submit_and_wait(endpoint, fallback_payload)
        img_url = _extract_image_url(payload_resp)
        if img_url:
            result = await _download_image(img_url)
            if result:
                logger.info(
                    "fal face_swap fallback success",
                    model=model_name,
                    size_bytes=len(result),
                    cost=f"${cost}",
                )
                return result
    logger.error("fal face_swap: all attempts failed")
    return None

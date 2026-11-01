"""Препроцессинг входного селфи перед отправкой в fal.ai nano-banana-2.

Цель: повысить качество результата за счёт стандартизованного входа.

Делаем:
  1. EXIF auto-rotate (часто iPhone-селфи перевёрнуты).
  2. Face detection через OpenCV Haar cascade (built-in, без сети).
  3. Crop с запасом вокруг лица (head + shoulders), приведение к 3:4 portrait.
  4. Resize до 1024×1365 (3:4) + JPEG quality 92.

Если лицо не нашлось — fallback на умный центральный crop (bias к верху,
где обычно лицо в селфи).

Зависимости: opencv-python-headless, numpy, Pillow.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Регистрируем HEIF/HEIC-опенер ОДИН раз при импорте. iPhone отправляет фото,
# присланные «как файл» (документом), в формате .heic — без этого плагина PIL
# их не открывает, prepare_face_image возвращает исходные байты, и они уходят
# в fal с ярлыком image/jpeg → модель не может декодировать вход и генерит
# «чушь» по одному тексту промпта. С опенером .heic читается как обычное фото.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_OK = True
except Exception as _exc:  # плагин не установлен / не загрузился — не падаем
    _HEIF_OK = False
    logger.warning("pillow-heif not available, .heic uploads will be rejected: %s", str(_exc)[:160])


def is_decodable_image(image_bytes: bytes) -> bool:
    """True если PIL может открыть и декодировать байты как растровое
    изображение. Используется как guard ПЕРЕД отправкой в fal: чтобы не списать
    кредиты за неоткрываемый файл (HEIC без плагина, RAW, битый файл) и не
    получить «чушь» вместо результата."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        img.load()  # фактически декодируем, не только читаем заголовок
        return True
    except Exception:
        return False


# Целевой формат: 3:4 portrait, совпадает с image_size='portrait_4_3' в fal.
_TARGET_SIZE = (1024, 1365)
_TARGET_RATIO = _TARGET_SIZE[0] / _TARGET_SIZE[1]  # ≈ 0.75


def _center_crop_to_ratio(img, target_ratio: float):
    """Crop к нужному соотношению. Bias к верхней трети (face обычно сверху)."""
    iw, ih = img.size
    current_ratio = iw / ih
    if abs(current_ratio - target_ratio) < 0.01:
        return img
    if current_ratio > target_ratio:
        # шире — режем по бокам, центрируем
        new_w = int(ih * target_ratio)
        x0 = (iw - new_w) // 2
        return img.crop((x0, 0, x0 + new_w, ih))
    # выше — режем по верху/низу, но bias к верху (лицо)
    new_h = int(iw / target_ratio)
    y0 = (ih - new_h) // 3  # bias upward
    return img.crop((0, y0, iw, y0 + new_h))


def prepare_face_image(image_bytes: bytes) -> bytes:
    """Подготовить селфи: EXIF-rotate + face-crop + resize до 1024×1365 JPEG.

    Никогда не бросает исключений: при любой ошибке возвращает исходные байты
    (caller получает «как есть» — fal попробует обработать сам).
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # EXIF auto-rotate
        img = img.convert("RGB")
        iw, ih = img.size

        # Face detection через Haar cascade (built-in opencv)
        cv_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(cascade_path)
        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.2,
            minNeighbors=5,
            minSize=(80, 80),
        )

        if len(faces) > 0:
            # самое крупное лицо
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            face_cx = x + fw / 2
            face_cy = y + fh / 2
            # Crop вокруг лица: голова + плечи (head ≈ 33% высоты кадра)
            crop_h = fh * 3.5
            crop_w = crop_h * _TARGET_RATIO
            crop_y0 = face_cy - fh * 1.2  # face чуть выше центра
            crop_x0 = face_cx - crop_w / 2
            crop_x1 = crop_x0 + crop_w
            crop_y1 = crop_y0 + crop_h
            # Клип к границам, потом ре-фит ratio
            crop_x0 = max(0, crop_x0)
            crop_y0 = max(0, crop_y0)
            crop_x1 = min(iw, crop_x1)
            crop_y1 = min(ih, crop_y1)
            cw, ch = crop_x1 - crop_x0, crop_y1 - crop_y0
            if cw > 0 and ch > 0:
                cropped = img.crop((int(crop_x0), int(crop_y0), int(crop_x1), int(crop_y1)))
                cropped = _center_crop_to_ratio(cropped, _TARGET_RATIO)
            else:
                cropped = _center_crop_to_ratio(img, _TARGET_RATIO)
            logger.info("image_prep: face crop applied (faces=%d)", len(faces))
        else:
            cropped = _center_crop_to_ratio(img, _TARGET_RATIO)
            logger.info("image_prep: no face detected, center crop")

        # Resize до целевого
        cropped = cropped.resize(_TARGET_SIZE, Image.LANCZOS)
        out = io.BytesIO()
        cropped.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()
    except Exception as exc:
        logger.warning("image_prep failed, returning original: %s", str(exc)[:200])
        return image_bytes

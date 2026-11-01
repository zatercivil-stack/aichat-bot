from __future__ import annotations

import io
import os
from functools import lru_cache

import structlog
from PIL import Image, ImageDraw, ImageFont

logger = structlog.get_logger(__name__)

# ✨ U+2728 — color emoji, нет монохромного глифа в DejaVu/Liberation/Arial.
# ★ U+2605 BLACK STAR — есть везде, читается как акцент, визуально близко.
WATERMARK_TEXT = "★ Нейра · t.me/NeyraChatBot"

# Кандидаты на шрифт — берём первый существующий. fonts-dejavu-core ставится
# в Dockerfile, на macOS dev-машинах может быть свой путь.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


@lru_cache(maxsize=8)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    logger.warning("No TTF font found, using PIL default (will be tiny)")
    return ImageFont.load_default()


def _measure(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _glyph_supported(font, text: str) -> bool:
    """Возвращает True, если в шрифте есть все нужные глифы.
    PIL рисует отсутствующие глифы как пустое пространство — проверяем что
    маска ненулевая для каждого символа."""
    try:
        # FreeType font supports getmask; bitmap default — нет.
        if not hasattr(font, "getmask"):
            return True
        for ch in text:
            if ch.isspace():
                continue
            mask = font.getmask(ch)
            if mask.size == (0, 0):
                return False
        return True
    except Exception:
        return True  # не блокируем рендер из-за проверки


def add_watermark(image_bytes: bytes) -> bytes:
    """Накладывает watermark на картинку. При любой ошибке возвращает оригинал."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGBA")
            w, h = img.size

            # Размер шрифта ~3.2% высоты, в пределах [18, 56]
            font_size = max(18, min(56, int(h * 0.032)))
            font = _load_font(font_size)

            text = WATERMARK_TEXT

            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            tw, th = _measure(draw, text, font)
            pad = max(10, font_size // 2)
            x = w - tw - pad
            y = h - th - pad

            # Тень — для читаемости на любом фоне
            shadow_offset = max(1, font_size // 14)
            draw.text(
                (x + shadow_offset, y + shadow_offset),
                text,
                font=font,
                fill=(0, 0, 0, 170),
            )
            # Основной текст — белый, ~82% непрозрачности
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 210))

            result = Image.alpha_composite(img, overlay)

            # Сохраняем как PNG (без потери качества) или JPEG (меньше размер).
            # PNG безопаснее — нет артефактов сжатия на тексте watermark.
            out_buf = io.BytesIO()
            result.convert("RGB").save(out_buf, format="PNG", optimize=True)
            return out_buf.getvalue()
    except Exception as exc:
        logger.warning(
            "Watermark failed — returning original",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return image_bytes

"""Генератор визуала для рекламных посевов в TG.

Делает 2 формата из 3 фото в data/launch_scenes/:
  • Анимированный GIF — 3 фото с плавным cross-fade и текстовыми оверлеями.
    Размер ~960×1280 (9:12, под TG-портретный кадр). 6 сек, 24 fps.
  • MP4-видео — то же самое в видео-формате (через ffmpeg), 15 сек.

Использование:
    docker compose exec bot python -m scripts.build_ad_visuals --gif
    docker compose exec bot python -m scripts.build_ad_visuals --video
    docker compose exec bot python -m scripts.build_ad_visuals --both

Результат:
    /app/data/launch_scenes/ad_preview.gif
    /app/data/launch_scenes/ad_preview.mp4
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PHOTOS_DIR = Path('/app/data/launch_scenes')
OUT_GIF = PHOTOS_DIR / 'ad_preview.gif'
OUT_MP4 = PHOTOS_DIR / 'ad_preview.mp4'

# 9:12 — оптимально для TG-портретного превью в ленте
TARGET_W = 720           # ужали с 960 — GIF получался >10MB
TARGET_H = 960
HOLD_FRAMES = 48          # 2 сек hold каждой сцены (24fps)
FADE_FRAMES = 14          # 0.6 сек cross-fade
FPS = 24
# Кружок (video_note) — round, square, fits anywhere
ROUND_SIZE = 480          # TG video_note рекомендованный размер

OVERLAY_LABELS = [
    'Балет у античной статуи',
    'Валькирия в доспехах',
    'Корт 70-х',
]
HEADER_TEXT = 'Селфи → ты в любой сцене'
FOOTER_TEXT = '@NeyraChatBot'


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """DejaVu есть в python:3.12-slim (поставили в Dockerfile)."""
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Cover-fit: масштабируем чтобы покрыть w×h, лишнее обрезаем по центру."""
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h = h
        new_w = int(h * src_ratio)
    else:
        new_w = w
        new_h = int(w / src_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def _draw_overlay(img: Image.Image, label: str) -> Image.Image:
    """Добавляет полу-прозрачный затемнённый низ + заголовок + lower-label + @brand."""
    img = img.convert('RGBA')
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Тёмная подложка снизу (gradient-имитация через 3 полупрозрачных полосы)
    bottom_band_h = 220
    for i, alpha in enumerate((40, 80, 160)):
        y0 = img.height - bottom_band_h + i * (bottom_band_h // 3)
        y1 = y0 + (bottom_band_h // 3) + 2
        draw.rectangle([0, y0, img.width, y1], fill=(0, 0, 0, alpha))

    # Header — наверху
    header_font = _load_font(52)
    bbox = draw.textbbox((0, 0), HEADER_TEXT, font=header_font)
    hx = (img.width - (bbox[2] - bbox[0])) // 2
    # Тёмная подложка под header
    pad = 16
    draw.rounded_rectangle(
        [hx - pad, 36, hx + (bbox[2] - bbox[0]) + pad, 36 + (bbox[3] - bbox[1]) + pad],
        radius=20, fill=(0, 0, 0, 140),
    )
    draw.text((hx, 44), HEADER_TEXT, fill=(255, 255, 255, 255), font=header_font)

    # Label сцены — внизу крупно
    label_font = _load_font(58)
    bbox = draw.textbbox((0, 0), label, font=label_font)
    lx = (img.width - (bbox[2] - bbox[0])) // 2
    ly = img.height - 160
    draw.text((lx, ly), label, fill=(255, 255, 255, 255), font=label_font)

    # Footer brand mark
    foot_font = _load_font(28)
    bbox = draw.textbbox((0, 0), FOOTER_TEXT, font=foot_font)
    fx = (img.width - (bbox[2] - bbox[0])) // 2
    fy = img.height - 60
    draw.text((fx, fy), FOOTER_TEXT, fill=(255, 255, 255, 220), font=foot_font)

    return Image.alpha_composite(img, overlay).convert('RGB')


def _load_scenes() -> list[Image.Image]:
    files = sorted(
        p for p in PHOTOS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
        and not p.name.startswith('ad_preview')
    )
    if len(files) < 2:
        raise FileNotFoundError(f'Нужно минимум 2 фото в {PHOTOS_DIR}')
    return [Image.open(f).convert('RGB') for f in files[:3]]


def _build_frames(scenes: list[Image.Image]) -> list[Image.Image]:
    """Подготавливает все кадры (hold + fade) с оверлеями."""
    framed = []
    for i, scene in enumerate(scenes):
        fitted = _fit(scene, TARGET_W, TARGET_H)
        label = OVERLAY_LABELS[i] if i < len(OVERLAY_LABELS) else ''
        framed.append(_draw_overlay(fitted, label))

    frames: list[Image.Image] = []
    for i, f in enumerate(framed):
        # Держим кадр
        for _ in range(HOLD_FRAMES):
            frames.append(f)
        # Cross-fade в следующий (если есть)
        if i < len(framed) - 1:
            nxt = framed[i + 1]
            for k in range(1, FADE_FRAMES):
                alpha = k / FADE_FRAMES
                blended = Image.blend(f, nxt, alpha)
                frames.append(blended)
    return frames


def build_gif() -> None:
    print(f'Loading {PHOTOS_DIR}…')
    scenes = _load_scenes()
    print(f'Got {len(scenes)} scenes, building frames…')
    frames = _build_frames(scenes)
    # PIL native GIF слишком толстый — гоним через ffmpeg с palettegen, файл будет в 5-10x меньше
    print(f'Saving GIF via ffmpeg ({len(frames)} frames @ {FPS}fps → {OUT_GIF})')
    tmp_dir = PHOTOS_DIR / '_gif_tmp'
    tmp_dir.mkdir(exist_ok=True)
    try:
        for i, f in enumerate(frames):
            f.save(tmp_dir / f'{i:04d}.png', 'PNG')
        palette = tmp_dir / 'palette.png'
        # Шаг 1: построить общую палитру
        subprocess.run([
            'ffmpeg', '-y', '-framerate', str(FPS),
            '-i', str(tmp_dir / '%04d.png'),
            '-vf', f'scale={TARGET_W}:{TARGET_H}:flags=lanczos,palettegen=stats_mode=diff',
            str(palette),
        ], check=True, capture_output=True)
        # Шаг 2: применить палитру, использовать diff-режим для меньшего размера
        result = subprocess.run([
            'ffmpeg', '-y', '-framerate', str(FPS),
            '-i', str(tmp_dir / '%04d.png'),
            '-i', str(palette),
            '-lavfi', f'scale={TARGET_W}:{TARGET_H}:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle',
            '-loop', '0',
            str(OUT_GIF),
        ], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f'ffmpeg gif failed: {result.stderr[-300:]}')
    finally:
        for p in tmp_dir.glob('*.png'):
            p.unlink()
        tmp_dir.rmdir()
    size_kb = OUT_GIF.stat().st_size // 1024
    print(f'✓ GIF готов: {OUT_GIF} ({size_kb} KB, {len(frames) // FPS}s)')


def build_mp4() -> None:
    """Использует ffmpeg чтобы сделать H.264 mp4 из тех же кадров через PNG-stream.
    TG лучше показывает mp4 чем большой GIF (есть автоплей, меньше размер)."""
    print('Building MP4 via ffmpeg…')
    scenes = _load_scenes()
    frames = _build_frames(scenes)
    tmp_dir = PHOTOS_DIR / '_frames_tmp'
    tmp_dir.mkdir(exist_ok=True)
    try:
        for i, f in enumerate(frames):
            f.save(tmp_dir / f'{i:04d}.png', 'PNG')
        cmd = [
            'ffmpeg', '-y',
            '-framerate', str(FPS),
            '-i', str(tmp_dir / '%04d.png'),
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-vf', f'scale={TARGET_W}:{TARGET_H}',
            '-crf', '22',
            '-preset', 'medium',
            '-movflags', '+faststart',
            str(OUT_MP4),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print('ffmpeg stderr:', result.stderr[-500:])
            raise RuntimeError(f'ffmpeg failed: exit {result.returncode}')
        size_kb = OUT_MP4.stat().st_size // 1024
        print(f'✓ MP4 готов: {OUT_MP4} ({size_kb} KB, {len(frames) // FPS}s)')
    finally:
        for p in tmp_dir.glob('*.png'):
            p.unlink()
        tmp_dir.rmdir()


OUT_ROUND = PHOTOS_DIR / 'ad_round.mp4'


def build_round() -> None:
    """Кружок (video_note): square 480×480, 7-8 сек, без водяных знаков.
    Подходит для отправки как sendVideoNote (но в каналах редко используется,
    основное применение — личные чаты)."""
    print('Building round video_note via ffmpeg…')
    scenes = _load_scenes()
    # Подгоняем под квадрат 480×480, без текстовых оверлеев (на кружке плохо читается)
    framed: list[Image.Image] = []
    for scene in scenes:
        sq = _fit(scene, ROUND_SIZE, ROUND_SIZE)
        framed.append(sq)
    frames: list[Image.Image] = []
    for i, f in enumerate(framed):
        for _ in range(HOLD_FRAMES):
            frames.append(f)
        if i < len(framed) - 1:
            for k in range(1, FADE_FRAMES):
                frames.append(Image.blend(f, framed[i + 1], k / FADE_FRAMES))

    tmp_dir = PHOTOS_DIR / '_round_tmp'
    tmp_dir.mkdir(exist_ok=True)
    try:
        for i, f in enumerate(frames):
            f.save(tmp_dir / f'{i:04d}.png', 'PNG')
        cmd = [
            'ffmpeg', '-y',
            '-framerate', str(FPS),
            '-i', str(tmp_dir / '%04d.png'),
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-vf', f'scale={ROUND_SIZE}:{ROUND_SIZE}',
            '-crf', '23', '-preset', 'medium',
            '-movflags', '+faststart',
            str(OUT_ROUND),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f'ffmpeg failed: {result.stderr[-300:]}')
        size_kb = OUT_ROUND.stat().st_size // 1024
        print(f'✓ Round готов: {OUT_ROUND} ({size_kb} KB, {len(frames) // FPS}s)')
    finally:
        for p in tmp_dir.glob('*.png'):
            p.unlink()
        tmp_dir.rmdir()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gif', action='store_true')
    parser.add_argument('--video', action='store_true')
    parser.add_argument('--round', action='store_true', help='video_note кружок')
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.all:
        args.gif = args.video = args.round = True
    if not (args.gif or args.video or args.round):
        print('Укажи --gif или --video или --round или --all')
        sys.exit(2)
    if args.gif:
        build_gif()
    if args.video:
        build_mp4()
    if args.round:
        build_round()


if __name__ == '__main__':
    main()

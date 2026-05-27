"""Repo для preset_previews — кеш TG file_id готовых демо-изображений каждого пресета.

Зачем: при показе превью юзеру (inline-preview перед списанием или в /gallery)
не хотим каждый раз грузить файл с диска. TG file_id живёт вечно, отдаётся
мгновенно, занимает ~150 байт в БД.

Workflow создания превью (в scripts/generate_preset_previews.py):
  1. Берём изображение (с диска или генерим через fal.ai)
  2. Отправляем boт.send_photo в админский чат — получаем message с file_id
  3. Сохраняем file_id в эту таблицу
  4. Subsequent .send_photo(file_id=...) — бесплатно и моментально
"""
from __future__ import annotations

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PresetPreview


async def get_preview_file_id(session: AsyncSession, preset_key: str,
                              position: int = 0) -> str | None:
    """Возвращает file_id для пресета (первая позиция по умолчанию), или None."""
    row = await session.scalar(
        select(PresetPreview)
        .where(PresetPreview.preset_key == preset_key,
               PresetPreview.position == position)
    )
    return row.file_id if row else None


async def get_all_previews(session: AsyncSession, preset_key: str) -> list[str]:
    """Все file_id для пресета по порядку position. Для /gallery в будущем."""
    result = await session.execute(
        select(PresetPreview)
        .where(PresetPreview.preset_key == preset_key)
        .order_by(PresetPreview.position)
    )
    return [r.file_id for r in result.scalars().all()]


async def get_previewed_keys(session: AsyncSession, position: int = 0) -> set[str]:
    """Множество preset_key, у которых есть превью в данной позиции. Нужно фичам
    «с картинкой» (Mystery «Подарок дня»), чтобы НЕ выбрать пресет без изображения
    и не уйти в рассылку текстом."""
    rows = await session.execute(
        select(PresetPreview.preset_key)
        .where(PresetPreview.position == position)
        .distinct()
    )
    return set(rows.scalars().all())


async def get_preview_pool(session: AsyncSession, limit: int = 60) -> list[str]:
    """Пул file_id готовых превью разных пресетов — для рассылок, где нужна
    РАЗНАЯ картинка каждый раз (напр. подписочные пуши). Берём по одному
    превью на пресет (position=0 — основной hero-кадр), чтобы пул был
    разнообразным по сценам, а не дублями одного пресета."""
    result = await session.execute(
        select(PresetPreview.file_id)
        .where(PresetPreview.position == 0)
        .limit(limit)
    )
    return [r for r in result.scalars().all() if r]


async def save_preview(session: AsyncSession, preset_key: str, file_id: str,
                       position: int = 0, source_file: str | None = None) -> None:
    """Upsert — если уже есть запись (preset_key, position), обновляет file_id."""
    stmt = pg_insert(PresetPreview).values(
        preset_key=preset_key,
        file_id=file_id,
        position=position,
        source_file=source_file,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=['preset_key', 'position'],
        set_={'file_id': file_id, 'source_file': source_file},
    )
    await session.execute(stmt)


async def has_preview(session: AsyncSession, preset_key: str) -> bool:
    """Быстрая проверка — есть ли превью для пресета."""
    return (await get_preview_file_id(session, preset_key)) is not None


async def list_presets_with_previews(session: AsyncSession) -> list[str]:
    """Все preset_key у которых есть превью. Для /gallery — пресеты без preview прячем."""
    result = await session.execute(
        select(PresetPreview.preset_key).distinct()
    )
    return [r[0] for r in result.all()]


async def delete_preview(session: AsyncSession, preset_key: str,
                         position: int | None = None) -> None:
    """Удалить превью (все позиции или одну конкретную)."""
    stmt = delete(PresetPreview).where(PresetPreview.preset_key == preset_key)
    if position is not None:
        stmt = stmt.where(PresetPreview.position == position)
    await session.execute(stmt)

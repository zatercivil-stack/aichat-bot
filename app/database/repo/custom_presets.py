"""Repo для custom_presets — пресеты, добавленные через дашборд.

В рантайме мёржатся с хардкод-пресетами (EDIT_PRESETS) функцией
reload_custom_presets() в app/handlers/image_edit.py. Превью этих пресетов
лежат в preset_previews по тому же key (position 0=жен, 1=муж).
"""
from __future__ import annotations

from sqlalchemy import select

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CustomPreset


async def list_custom_presets(session: AsyncSession,
                              only_enabled: bool = False) -> list[CustomPreset]:
    stmt = select(CustomPreset).order_by(CustomPreset.created_at.desc())
    if only_enabled:
        stmt = stmt.where(CustomPreset.enabled == True)  # noqa: E712
    return list((await session.execute(stmt)).scalars().all())


async def get_custom_preset(session: AsyncSession, preset_id: int) -> CustomPreset | None:
    return await session.get(CustomPreset, preset_id)


async def get_by_key(session: AsyncSession, key: str) -> CustomPreset | None:
    return await session.scalar(select(CustomPreset).where(CustomPreset.key == key))


async def create_custom_preset(
    session: AsyncSession, *, key: str, label: str, prompt: str,
    male_prompt: str | None = None, quality_mode: str = 'quality',
    category: str | None = None, female_only: bool = False,
) -> CustomPreset:
    preset = CustomPreset(
        key=key, label=label, prompt=prompt, male_prompt=male_prompt or None,
        quality_mode=quality_mode, category=category or None,
        female_only=female_only, enabled=True,
    )
    session.add(preset)
    await session.flush()
    return preset


async def update_custom_preset(session: AsyncSession, preset_id: int, **fields) -> bool:
    preset = await session.get(CustomPreset, preset_id)
    if not preset:
        return False
    allowed = {'label', 'prompt', 'male_prompt', 'quality_mode',
               'category', 'female_only', 'enabled', 'featured'}
    for k, v in fields.items():
        if k in allowed:
            setattr(preset, k, v)
    return True


async def delete_custom_preset(session: AsyncSession, preset_id: int) -> str | None:
    """Удаляет пресет. Возвращает key удалённого (для очистки превью/кеша) или None."""
    preset = await session.get(CustomPreset, preset_id)
    if not preset:
        return None
    key = preset.key
    await session.delete(preset)
    return key


async def toggle_custom_preset(session: AsyncSession, preset_id: int) -> bool | None:
    """Переключает enabled. Возвращает новое значение или None если не найден."""
    preset = await session.get(CustomPreset, preset_id)
    if not preset:
        return None
    preset.enabled = not preset.enabled
    return preset.enabled


async def toggle_featured(session: AsyncSession, preset_id: int) -> bool | None:
    """Переключает featured (хит — первым в категории каталога)."""
    preset = await session.get(CustomPreset, preset_id)
    if not preset:
        return None
    preset.featured = not preset.featured
    return preset.featured

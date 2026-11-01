"""Repo для preset_launch — единый реестр даты запуска пресета (код + DB).

Зачем: «новизна»/graduation раздела «🆕 Новинки» должна считаться от РЕАЛЬНОЙ
даты появления пресета, а не от generated_at превью (та сбивается при массовой
регенерации). Заполняется автоматически в reload_custom_presets:
  • DB-пресет  → custom_presets.created_at (точная дата);
  • хардкод    → now() при первом появлении (момент, когда бот впервые увидел ключ).
См. модель PresetLaunch и миграцию 024.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PresetLaunch


async def get_launch_map(session: AsyncSession) -> dict[str, datetime]:
    """{preset_key: launched_at} для всех зарегистрированных пресетов."""
    rows = await session.execute(select(PresetLaunch.preset_key, PresetLaunch.launched_at))
    return {k: ts for k, ts in rows.all()}


async def ensure_launch_rows(
    session: AsyncSession,
    keys: Iterable[str],
    created_at_map: dict[str, datetime] | None = None,
) -> int:
    """Регистрирует дату запуска для ключей, у которых её ещё НЕТ. Идемпотентно
    (ON CONFLICT DO NOTHING — существующие даты не трогаем). DB-пресеты берут
    дату из created_at_map (их custom_presets.created_at), хардкод-ключи без
    записи в карте → server_default now() (момент первого появления).
    Возвращает число вставленных строк."""
    created_at_map = created_at_map or {}
    inserted = 0
    for key in keys:
        dt = created_at_map.get(key)
        values: dict = {"preset_key": key}
        if dt is not None:
            values["launched_at"] = dt  # иначе сработает server_default now()
        stmt = (
            pg_insert(PresetLaunch)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["preset_key"],
            )
        )
        res = await session.execute(stmt)
        inserted += res.rowcount or 0
    await session.commit()
    return inserted


async def set_launch(session: AsyncSession, preset_key: str, launched_at: datetime) -> None:
    """Принудительно выставить дату запуска (для админ-оверрайда «снова сделать
    новым» или коррекции). Upsert."""
    stmt = (
        pg_insert(PresetLaunch)
        .values(
            preset_key=preset_key,
            launched_at=launched_at,
        )
        .on_conflict_do_update(
            index_elements=["preset_key"],
            set_={"launched_at": launched_at},
        )
    )
    await session.execute(stmt)
    await session.commit()

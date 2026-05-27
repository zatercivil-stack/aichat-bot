"""A/B experiment assignment & conversion tracking.

Variant selection is deterministic per user (hash-based) so users always see
the same variant. Conversions are recorded on the assignment row.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Mapping

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Experiment, ExperimentAssignment

logger = structlog.get_logger(__name__)


def _pick_variant(user_id: int, variants: Mapping[str, str]) -> str:
    """Deterministic variant pick: hash(user_id + exp.name) % len(variants)."""
    keys = sorted(variants.keys())
    if not keys:
        return ''
    h = int(hashlib.md5(f'neyra:{user_id}'.encode()).hexdigest(), 16)
    return keys[h % len(keys)]


async def get_active_experiment(session: AsyncSession, target: str) -> Experiment | None:
    """First active experiment for the given target slot."""
    return await session.scalar(
        select(Experiment).where(
            Experiment.target == target,
            Experiment.is_active == True,
        ).order_by(Experiment.created_at.desc()).limit(1)
    )


async def get_user_variant(
    session: AsyncSession, user_id: int, target: str,
) -> tuple[str | None, str | None, int | None]:
    """Returns (variant_text, variant_key, experiment_id) or (None, None, None).

    Stable: existing assignment is reused; new assignment is created and committed.
    """
    exp = await get_active_experiment(session, target)
    if not exp or not exp.variants:
        return None, None, None

    # Find existing assignment
    existing = await session.scalar(
        select(ExperimentAssignment).where(
            ExperimentAssignment.user_id == user_id,
            ExperimentAssignment.experiment_id == exp.id,
        )
    )
    if existing:
        variant_text = exp.variants.get(existing.variant)
        if variant_text is None:
            # Variant removed from experiment — fall back to deterministic re-pick
            variant_key = _pick_variant(user_id, exp.variants)
            variant_text = exp.variants.get(variant_key)
        return variant_text, existing.variant, exp.id

    # Assign new variant
    variant_key = _pick_variant(user_id, exp.variants)
    if not variant_key:
        return None, None, None
    try:
        stmt = insert(ExperimentAssignment).values(
            user_id=user_id, experiment_id=exp.id, variant=variant_key,
        ).on_conflict_do_nothing(index_elements=['user_id', 'experiment_id'])
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning('experiment assign failed', user_id=user_id, exp=exp.name, error=str(exc)[:200])
    return exp.variants.get(variant_key), variant_key, exp.id


async def mark_conversion(user_id: int, event: str) -> None:
    """Mark first-time conversion for any experiment the user is assigned to.

    `event` is one of: 'first_message', 'first_payment'.
    """
    from app.database.base import AsyncSessionFactory
    try:
        async with AsyncSessionFactory() as session:
            now = datetime.now(timezone.utc)
            rows = (await session.execute(
                select(ExperimentAssignment).where(
                    ExperimentAssignment.user_id == user_id,
                    ExperimentAssignment.converted_at.is_(None),
                )
            )).scalars().all()
            for row in rows:
                row.converted_at = now
                row.conversion_event = event
            if rows:
                await session.commit()
    except Exception as exc:
        logger.warning('mark_conversion failed', user_id=user_id, event=event, error=str(exc)[:200])


async def get_experiment_results(
    session: AsyncSession, experiment_id: int,
) -> list[dict]:
    """Per-variant stats: assigned count, conversions, conversion rate."""
    rows = (await session.execute(
        select(
            ExperimentAssignment.variant,
            func.count().label('assigned'),
            func.count(ExperimentAssignment.converted_at).label('converted'),
        )
        .where(ExperimentAssignment.experiment_id == experiment_id)
        .group_by(ExperimentAssignment.variant)
        .order_by(ExperimentAssignment.variant)
    )).all()
    out = []
    for variant, assigned, converted in rows:
        rate = (converted / assigned * 100) if assigned else 0.0
        out.append({
            'variant': variant,
            'assigned': assigned,
            'converted': converted,
            'rate': round(rate, 2),
        })
    return out

"""Cohort retention + activity heatmap queries."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_cohort_retention(session: AsyncSession, weeks_back: int = 12) -> dict:
    """Returns weekly cohorts × retention weeks.

    Cohort = week of signup (ISO week, truncated to Monday in UTC).
    Retention[N] = share of cohort users who sent ≥1 message in week N after signup.
    """
    sql = text("""
        WITH cohorts AS (
            SELECT
                date_trunc('week', created_at) AS cohort_week,
                id AS user_id
            FROM users
            WHERE created_at >= NOW() - (CAST(:weeks AS text) || ' weeks')::interval
        ),
        msg_weeks AS (
            SELECT
                user_id,
                date_trunc('week', created_at) AS msg_week
            FROM bot_events
            WHERE event_type = 'message_sent'
              AND created_at >= NOW() - (CAST(:weeks AS text) || ' weeks')::interval
            GROUP BY user_id, date_trunc('week', created_at)
        )
        SELECT
            c.cohort_week,
            EXTRACT(EPOCH FROM (m.msg_week - c.cohort_week)) / (7 * 24 * 3600) AS week_offset,
            COUNT(DISTINCT c.user_id) AS users_active
        FROM cohorts c
        LEFT JOIN msg_weeks m ON m.user_id = c.user_id AND m.msg_week >= c.cohort_week
        GROUP BY c.cohort_week, week_offset
        ORDER BY c.cohort_week DESC, week_offset
    """)
    rows = (await session.execute(sql, {'weeks': str(weeks_back)})).all()

    # cohort_sizes: total users per cohort
    cohort_sizes_sql = text("""
        SELECT date_trunc('week', created_at) AS cohort_week, COUNT(*)
        FROM users
        WHERE created_at >= NOW() - (CAST(:weeks AS text) || ' weeks')::interval
        GROUP BY cohort_week
        ORDER BY cohort_week DESC
    """)
    sizes = {r[0]: int(r[1]) for r in (await session.execute(cohort_sizes_sql, {'weeks': str(weeks_back)})).all()}

    # Build matrix
    cohorts: dict = {}
    max_offset = 0
    for cohort_week, week_offset, users in rows:
        if week_offset is None or week_offset < 0:
            continue
        off = int(week_offset)
        if off > max_offset:
            max_offset = off
        cohorts.setdefault(cohort_week, {})[off] = int(users)

    # Format for template (newest cohort first)
    matrix = []
    for cohort_week in sorted(sizes.keys(), reverse=True):
        size = sizes[cohort_week]
        row_data = {
            'week': cohort_week.strftime('%d.%m.%Y'),
            'size': size,
            'cells': [],
        }
        for off in range(0, min(max_offset + 1, 9)):
            cnt = cohorts.get(cohort_week, {}).get(off, 0)
            pct = (cnt / size * 100) if size else 0
            row_data['cells'].append({'cnt': cnt, 'pct': round(pct, 1)})
        matrix.append(row_data)

    return {'matrix': matrix, 'max_offset': min(max_offset, 8)}


async def get_activity_heatmap(session: AsyncSession, days: int = 30) -> dict:
    """Returns 7×24 heatmap: weekday (0=Mon) × hour (0-23) → message count."""
    sql = text("""
        SELECT
            EXTRACT(ISODOW FROM created_at)::int - 1 AS weekday,  -- 0..6 (Mon..Sun)
            EXTRACT(HOUR FROM created_at)::int AS hour,
            COUNT(*) AS cnt
        FROM bot_events
        WHERE event_type = 'message_sent'
          AND created_at >= NOW() - (CAST(:days AS text) || ' days')::interval
        GROUP BY weekday, hour
    """)
    rows = (await session.execute(sql, {'days': str(days)})).all()
    grid: list[list[int]] = [[0] * 24 for _ in range(7)]
    max_val = 0
    for weekday, hour, cnt in rows:
        c = int(cnt)
        grid[int(weekday)][int(hour)] = c
        if c > max_val:
            max_val = c
    return {'grid': grid, 'max': max_val, 'days': days}

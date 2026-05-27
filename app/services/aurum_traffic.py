"""Read-only доступ к Aurum-БД (синкается из @Shop_Aurum_Bot каждые 5 мин через cron).
Используется для админ-страницы /admin/aurum-traffic — атрибуция трафика
из канала @conturD на платежи Aurum.

База лежит локально в /app/data/aurum/users.db (mount из host через docker-compose),
запросы read-only через стандартный sqlite3 stdlib.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Внутри контейнера ./data из docker-compose мапится в /app/data
DB_PATH = Path('/app/data/aurum/users.db')


def is_available() -> bool:
    return DB_PATH.exists() and DB_PATH.stat().st_size > 0


def db_freshness_seconds() -> int | None:
    if not DB_PATH.exists():
        return None
    return int(datetime.now().timestamp() - DB_PATH.stat().st_mtime)


@contextmanager
def _conn():
    """Open Aurum sqlite read-only (mode=ro)."""
    uri = f'file:{DB_PATH}?mode=ro'
    c = sqlite3.connect(uri, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def get_traffic_funnel() -> list[dict[str, Any]]:
    """Воронка по traffic_source: пришли → оплатили → выручка."""
    if not is_available():
        return []
    sql = """
        SELECT
            u.traffic_source AS source,
            COUNT(DISTINCT u.telegram_id) AS users,
            SUM(CASE WHEN u.trial_used = 1 THEN 1 ELSE 0 END) AS trials_used,
            COUNT(DISTINCT t.user_id) AS payers,
            COALESCE(SUM(CASE WHEN t.status='paid' THEN t.amount_rub END), 0) AS revenue_rub,
            MIN(u.registration_date) AS first_signup,
            MAX(u.registration_date) AS last_signup
        FROM users u
        LEFT JOIN transactions t ON t.user_id = u.telegram_id
        WHERE u.traffic_source IS NOT NULL AND u.traffic_source != ''
        GROUP BY u.traffic_source
        ORDER BY revenue_rub DESC, users DESC
    """
    try:
        with _conn() as c:
            rows = c.execute(sql).fetchall()
        out = []
        for r in rows:
            users = r['users'] or 0
            payers = r['payers'] or 0
            revenue = float(r['revenue_rub'] or 0)
            conv = (payers / users * 100) if users else 0.0
            arppu = (revenue / payers) if payers else 0.0
            out.append({
                'source': r['source'],
                'users': users,
                'trials_used': r['trials_used'] or 0,
                'payers': payers,
                'revenue_rub': revenue,
                'conv_rate': round(conv, 1),
                'arppu': round(arppu, 0),
                'first_signup': r['first_signup'],
                'last_signup': r['last_signup'],
            })
        return out
    except Exception as exc:
        logger.warning('Aurum funnel query failed', error=str(exc)[:200])
        return []


def get_landings_log(limit: int = 50) -> list[dict[str, Any]]:
    """Последние клики (traffic_landed events) — каждый /start chan_* / utm_*."""
    if not is_available():
        return []
    sql = """
        SELECT user_id, data_json, created_at
        FROM events
        WHERE event_type = 'traffic_landed'
        ORDER BY id DESC
        LIMIT ?
    """
    try:
        with _conn() as c:
            rows = c.execute(sql, (limit,)).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r['data_json']) if r['data_json'] else {}
            except Exception:
                data = {}
            out.append({
                'user_id': r['user_id'],
                'source': data.get('source', '—'),
                'created_at': r['created_at'],
            })
        return out
    except Exception as exc:
        logger.warning('Aurum landings query failed', error=str(exc)[:200])
        return []


def get_summary() -> dict[str, Any]:
    """Сводные метрики Aurum (для top-cards админ-страницы)."""
    if not is_available():
        return {'available': False}
    try:
        with _conn() as c:
            total_users = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            paying = c.execute(
                "SELECT COUNT(DISTINCT user_id) FROM transactions WHERE status='paid'"
            ).fetchone()[0]
            revenue = c.execute(
                "SELECT COALESCE(SUM(amount_rub),0) FROM transactions WHERE status='paid'"
            ).fetchone()[0]
            tracked = c.execute(
                "SELECT COUNT(*) FROM users WHERE traffic_source IS NOT NULL AND traffic_source != ''"
            ).fetchone()[0]
            landings = c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='traffic_landed'"
            ).fetchone()[0]
        return {
            'available': True,
            'total_users': total_users,
            'paying_users': paying,
            'revenue_total': float(revenue or 0),
            'tracked_users': tracked,
            'landings_total': landings,
            'db_age_sec': db_freshness_seconds(),
        }
    except Exception as exc:
        logger.warning('Aurum summary failed', error=str(exc)[:200])
        return {'available': False, 'error': str(exc)[:200]}

"""Brave Search API — для актуальных фактов в постах канала.

Использование:
- Перед генерацией поста по теме где важны свежие факты (новости AI, статистика,
  цифры по сервисам) — делаем 1-2 запроса через Brave, получаем top-5 snippets,
  кладём в system-prompt как контекст.
- Sonnet 4.6 потом пишет пост с реальными фактами вместо галлюцинаций.

API: https://api.search.brave.com/res/v1/web/search
Бесплатный тариф: 1 запрос/сек, 2000 запросов/месяц.
"""
from __future__ import annotations

from typing import Optional

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'


def is_enabled() -> bool:
    return bool(getattr(settings, 'BRAVE_SEARCH_API_KEY', ''))


async def search(query: str, count: int = 5, freshness: Optional[str] = None) -> list[dict]:
    """Возвращает [{title, url, description, age}] или [] при ошибке.

    Args:
        query: поисковый запрос
        count: сколько результатов (1-20)
        freshness: 'pd' (день), 'pw' (неделя), 'pm' (месяц), 'py' (год).
                   None = без фильтра.
    """
    if not is_enabled():
        logger.warning('brave_search: API key not set')
        return []

    params = {'q': query, 'count': min(max(count, 1), 20)}
    if freshness:
        params['freshness'] = freshness

    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'X-Subscription-Token': settings.BRAVE_SEARCH_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_ENDPOINT, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning('brave_search: API call failed',
                       query=query[:80], error=str(exc)[:200])
        return []

    results = (data.get('web') or {}).get('results') or []
    out: list[dict] = []
    for r in results[:count]:
        out.append({
            'title': r.get('title', '').strip(),
            'url': r.get('url', ''),
            'description': r.get('description', '').strip(),
            'age': r.get('age', ''),  # «1 week ago», «2 days ago» и т.п.
        })
    return out


def format_for_context(results: list[dict]) -> str:
    """Форматирует результаты для вставки в system-prompt как контекст.
    Компактный markdown-like формат, минимум токенов."""
    if not results:
        return ''
    lines = ['АКТУАЛЬНЫЕ ФАКТЫ ИЗ ВЕБ-ПОИСКА:']
    for i, r in enumerate(results, 1):
        age = f' ({r["age"]})' if r['age'] else ''
        lines.append(f'[{i}] {r["title"]}{age}')
        if r['description']:
            lines.append(f'    {r["description"][:280]}')
    return '\n'.join(lines)

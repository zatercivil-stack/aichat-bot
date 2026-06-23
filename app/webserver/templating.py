from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / 'templates'

# Moscow time. БД хранит datetime в UTC (часть колонок tz-aware, часть naive).
# Шаблонам удобнее всегда видеть МСК — оператор смотрит на дашборд из России.
MSK_TZ = timezone(timedelta(hours=3))


def _to_msk(value):
    """Конвертирует datetime в МСК. None/некорректные значения отдаёт обратно как есть."""
    if not isinstance(value, datetime):
        return value
    # Naive datetime в нашей БД = UTC (server_default=func.now() в Postgres).
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MSK_TZ)


def _msk_strftime(value, fmt: str = '%d.%m.%Y %H:%M'):
    """Шорткат для шаблонов: {{ obj.created_at|msk_strftime('%d.%m %H:%M') }}."""
    converted = _to_msk(value)
    if not isinstance(converted, datetime):
        return ''
    return converted.strftime(fmt)


env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(['html', 'xml']),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters['msk'] = _to_msk
env.filters['msk_strftime'] = _msk_strftime

# Левая навигация. Секции (url=None) — заголовки. Перегруппировано под
# visual-first проект: «Маркетинг» (9 пунктов) разбит на Контент/Рассылки/Трафик;
# Поддержка поднята в «Главное»; logs слиты в «События» (одна таблица bot_events,
# /logs остался как вкладка «Живой поток»); мёртвые A/B-тесты (0 записей) убраны
# из меню (роут /experiments жив). Роуты НЕ удалялись — менялось только меню.
NAV_ITEMS = [
    ('_sec_main',    '',   '🟣 Нейра',        None),
    ('overview',     '📊', 'Дашборд',         '/admin/'),
    ('users',        '👥', 'Пользователи',   '/admin/users'),
    ('support',      '🆘', 'Поддержка',       '/admin/support'),

    ('_sec_aurelina', '',  '🔮 Aurelina', None),
    # отдельный дашборд вынесен на dashboard.example.com (см. astrobot/app/admin.py).
    # Старые /admin/aurelina* роуты ещё живы как фолбэк, но из меню убраны.
    ('aurelina_dash', '🚀', 'Дашборд ↗',      'https://dashboard.example.com'),

    ('_sec_money',   '',   'Деньги',         None),
    ('finance',      '💰', 'Финансы',         '/admin/finance'),
    ('payments',     '💳', 'Платежи',         '/admin/payments'),
    ('promos',       '🎟', 'Промокоды',       '/admin/promos'),

    ('_sec_content', '',   'Контент',        None),
    ('presets',      '🎨', 'Пресеты',         '/admin/presets'),
    ('gallery',      '🖼', 'Галерея превью',  '/admin/gallery'),
    ('sandbox',      '🧪', 'Песочница',       '/admin/sandbox'),

    ('_sec_send',    '',   'Рассылки',       None),
    ('broadcasts',   '📢', 'Рассылки',        '/admin/broadcasts'),
    ('photo_tpl',    '🎭', 'Фото-шаблоны',    '/admin/broadcasts/photo'),
    ('scheduled',    '⏰', 'Отложенные',      '/admin/scheduled'),
    ('bulk',         '🎁', 'Bulk-операции',   '/admin/bulk'),

    ('_sec_analyt',  '',   'Трафик и аналитика', None),
    ('channel_traffic','📈','Трафик канала',  '/admin/channel-traffic'),
    ('aurum_traffic','🛡', 'Aurum трафик',    '/admin/aurum-traffic'),
    ('cohorts',      '📊', 'Когорты (LTV)',   '/admin/cohorts'),
    ('heatmap',      '🔥', 'Heatmap часов',   '/admin/heatmap'),

    ('_sec_ops',     '',   'Операционка',    None),
    ('events',       '📋', 'События',         '/admin/events'),
    ('audit',        '📜', 'Журнал админов',  '/admin/audit'),
    ('health',       '🩺', 'Здоровье',        '/admin/health'),

    ('_sec_config',  '',   'Конфигурация',   None),
    ('pricing',      '🏷', 'Цены',            '/admin/pricing'),
    ('texts',        '📝', 'Тексты бота',     '/admin/texts'),
]


def render(template: str, **context) -> HTMLResponse:
    from app.webserver.csrf import current_session, csrf_for
    context.setdefault('nav_items', NAV_ITEMS)
    context.setdefault('active', '')
    context.setdefault('msg', '')
    context.setdefault('error', '')
    # csrf_token доступен в шаблонах ({{ csrf_token }}) — для JS-fetch POST'ов.
    context.setdefault('csrf_token', csrf_for(current_session.get()))
    html = env.get_template(template).render(**context)
    html = _inject_csrf(html)
    return HTMLResponse(html)


def _inject_csrf(html: str) -> str:
    """Серверно вставляет <input hidden name=csrf_token> сразу после каждого
    открывающего <form method="post">. Токен берём из contextvar (ставит
    middleware по сессии). Без сессии (login-страница) — не трогаем. Надёжнее
    JS — работает даже при отключённом JavaScript, покрывает все формы без
    правки шаблонов.

    ТОЛЬКО POST-формы: CSRF важен лишь для изменяющих состояние запросов, а
    инжект токена в GET-форму (поиск/фильтры) утёк бы в URL → Referer и
    access-логи. Токен детерминирован на сессию (живёт 7 дней), поэтому его
    утечка в query-строке = риск. Мутирующие формы объявляют method="post"
    явно, GET-формы — method="get" (проверено аудитом шаблонов)."""
    import re
    from app.webserver.csrf import current_session, csrf_for
    token = csrf_for(current_session.get())
    if not token:
        return html
    field = f'<input type="hidden" name="csrf_token" value="{token}">'
    return re.sub(
        r'(<form\b[^>]*\bmethod\s*=\s*["\']?post["\']?[^>]*>)',
        r'\1' + field, html, flags=re.IGNORECASE,
    )

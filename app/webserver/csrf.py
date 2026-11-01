"""CSRF-защита админ-дашборда (25.05).

Модель: токен = HMAC-SHA256(ADMIN_SECRET_KEY, admin_session). Атакующий с чужого
сайта НЕ может его вычислить (нет секрета) и НЕ может прочитать httponly-куку
сессии (same-origin policy) → не сможет вставить верный токен в поддельную
форму. Кука сессии браузером прикладывается автоматически (это и есть суть
CSRF), но без совпадающего form-поля csrf_token запрос отклоняется.

Внедрение — без правки 34 хендлеров и 20 шаблонов:
  • middleware кладёт текущую сессию в contextvar + валидирует POST
  • render() читает contextvar, вставляет <input hidden csrf_token> в каждую
    <form> серверно (надёжнее JS — работает даже без JavaScript)
"""

from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar

from app.config import settings

# Сессия текущего запроса — ставит middleware, читает render() для инжекта.
current_session: ContextVar[str | None] = ContextVar("csrf_session", default=None)


def csrf_for(session_token: str | None) -> str:
    """Детерминированный CSRF-токен для данной сессии. Пустая строка если нет
    сессии (неавторизован — на login csrf не нужен)."""
    if not session_token:
        return ""
    return hmac.new(
        settings.ADMIN_SECRET_KEY.encode(),
        session_token.encode(),
        hashlib.sha256,
    ).hexdigest()


def csrf_valid(session_token: str | None, submitted: str | None) -> bool:
    """Constant-time сверка присланного токена с ожидаемым для сессии."""
    expected = csrf_for(session_token)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)

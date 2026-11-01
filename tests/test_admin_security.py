"""Admin security checks against disposable test services only."""

import pytest

from app.webserver import admin as adm


# ── _client_ip: парсинг (чистая функция, без зависимостей) ──────────────────
class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeReq:
    def __init__(self, headers=None, client_host=None):
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host else None


@pytest.mark.parametrize(
    "headers, client_host, expected",
    [
        ({"x-forwarded-for": "5.6.7.8"}, "127.0.0.1", "5.6.7.8"),
        ({"x-forwarded-for": "5.6.7.8, 9.9.9.9"}, "127.0.0.1", "9.9.9.9"),
        ({"x-forwarded-for": "127.0.0.1"}, "203.0.113.5", "203.0.113.5"),
        ({"x-real-ip": "127.0.0.1"}, "203.0.113.5", "203.0.113.5"),
        ({"x-real-ip": "1.2.3.4"}, "127.0.0.1", "1.2.3.4"),
        ({}, "203.0.113.5", "203.0.113.5"),
        ({}, None, "unknown"),
    ],
)
def test_client_ip(headers, client_host, expected):
    assert adm._client_ip(_FakeReq(headers, client_host)) == expected


# ── _check_auth: только точное совпадение с активным токеном ────────────────
def test_check_auth(monkeypatch):
    monkeypatch.setattr(adm, "_ACTIVE_TOKEN", "SECRET123")
    assert adm._check_auth("SECRET123") is True
    assert adm._check_auth("wrong") is False
    assert adm._check_auth("") is False
    assert adm._check_auth(None) is False


# ── рейт-лимит входа (реальный Redis, изолированный тестовый IP) ─────────────
@pytest.mark.asyncio
async def test_login_rate_limit():
    ip = "test-rl-198.51.100.77"
    await adm._login_clear_fails(ip)
    assert await adm._login_blocked(ip) is False
    for _ in range(adm._LOGIN_FAIL_LIMIT):
        await adm._login_record_fail(ip)
    assert await adm._login_blocked(ip) is True  # лимит достигнут → блок
    await adm._login_clear_fails(ip)
    assert await adm._login_blocked(ip) is False  # сброс снимает блок


# ── детект нового IP (реальная БД, с восстановлением) ───────────────────────
@pytest.mark.asyncio
async def test_new_ip_detection():
    from app.database.base import AsyncSessionFactory
    from app.database.repo.bot_config import get_config_value, set_config_value

    async with AsyncSessionFactory() as s:
        original = await get_config_value(s, "admin_known_ips")
    try:
        async with AsyncSessionFactory() as s:
            await set_config_value(s, "admin_known_ips", "[]")
            await s.commit()
        ip = "test-203.0.113.200"
        assert await adm._is_new_admin_ip(ip) is True  # впервые → новый
        assert await adm._is_new_admin_ip(ip) is False  # повторно → известный
    finally:
        async with AsyncSessionFactory() as s:
            await set_config_value(s, "admin_known_ips", original or "")
            await s.commit()


# ── ротация токена (реальная БД, ПОЛНОЕ восстановление прод-состояния) ───────
@pytest.mark.asyncio
async def test_rotate_token():
    from app.database.base import AsyncSessionFactory
    from app.database.repo.bot_config import get_config_value, set_config_value

    async with AsyncSessionFactory() as s:
        original = await get_config_value(s, "admin_token_active")
    old_active = adm._ACTIVE_TOKEN
    try:
        new = await adm.rotate_admin_token()
        assert len(new) >= 40 and new != old_active  # новый, стойкий
        assert adm._check_auth(old_active) is False  # старый отвергнут
        assert adm._check_auth(new) is True  # новый принят
        async with AsyncSessionFactory() as s:
            assert await get_config_value(s, "admin_token_active") == new  # персист
    finally:
        async with AsyncSessionFactory() as s:
            await set_config_value(s, "admin_token_active", original or "")
            await s.commit()
        adm._ACTIVE_TOKEN = old_active


@pytest.mark.parametrize("allowed", [True, False])
def test_channel_admin_permissions_follow_configuration(monkeypatch, allowed):
    from app.config import settings
    from app.handlers.channel_admin import _is_admin_uid

    monkeypatch.setattr(settings, "ADMIN_IDS", "123456" if allowed else "")
    assert _is_admin_uid(123456) is allowed


@pytest.mark.parametrize("base_url", ["https://admin.example.com", ""])
async def test_token_rotation_uses_configured_admin_url(monkeypatch, base_url):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.handlers.admin import cb_sec_rotate

    monkeypatch.setattr(settings, "ADMIN_IDS", "123456")
    monkeypatch.setattr(settings, "ADMIN_BASE_URL", base_url)
    monkeypatch.setattr(adm, "rotate_admin_token", AsyncMock(return_value="test-rotation-value"))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123456),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_reply_markup=AsyncMock(), answer=AsyncMock()),
    )
    await cb_sec_rotate(callback)
    text = callback.message.answer.call_args.args[0]
    if base_url:
        assert base_url + "/admin/login" in text
    else:
        assert "https://" not in text

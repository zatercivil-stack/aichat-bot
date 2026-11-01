from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import secrets as _secrets
from datetime import UTC, datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Cookie, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import text as sa_text

from app.config import settings
from app.constants import HELP_TEXT, WELCOME_TEXT
from app.database.base import AsyncSessionFactory
from app.database.models import (
    AdminAction,
    BotEvent,
    Broadcast,
    ChatMessage,
    Experiment,
    ExperimentAssignment,
    Payment,
    PaymentStatus,
    PhotoTemplate,
    PromoCode,
    Subscription,
    User,
)
from app.database.repo.analytics import get_activity_heatmap, get_cohort_retention
from app.database.repo.audit import log_action_bg
from app.database.repo.bot_config import (
    ensure_defaults,
    get_all_config,
    set_config_value,
)
from app.database.repo.broadcasts import (
    get_broadcast_by_id,
    get_broadcasts,
    get_promo_conversion_funnel,
    get_promo_stats,
)
from app.database.repo.events import get_dashboard_stats
from app.database.repo.experiments import get_experiment_results
from app.database.repo.finance import (
    get_conversion_metrics,
    get_daily_revenue,
    get_payment_funnel,
    get_payment_status_breakdown,
    get_revenue_by_method,
    get_revenue_summary,
    get_top_spenders,
)
from app.services import context_service, model_health
from app.webserver.templating import render

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

COOKIE_NAME = "admin_session"

# Активный токен: дефолт из .env, но может быть заменён на лету через
# rotate_admin_token() (кнопка «сменить токен» в TG-алерте). Хранится в
# bot_config['admin_token_active'] → переживает рестарт (load_active_token).
# Бот и вебсервер — ОДИН процесс (webhook_task в main.py), поэтому модульная
# переменная видна и хендлеру бота, и роутам панели.
_ACTIVE_TOKEN: str = settings.ADMIN_TOKEN


# ── AUTH HELPERS ──────────────────────────────────────────────────────────────


def _check_auth(token: str | None) -> bool:
    if not token or not _ACTIVE_TOKEN:
        return False
    return _secrets.compare_digest(token, _ACTIVE_TOKEN)


async def load_active_token() -> None:
    """Подтянуть активный токен из bot_config (если ротировался ранее). Зовётся
    на старте приложения — чтобы ротация пережила рестарт."""
    global _ACTIVE_TOKEN
    try:
        from app.database.repo.bot_config import get_config_value

        async with AsyncSessionFactory() as s:
            v = await get_config_value(s, "admin_token_active")
        if v:
            _ACTIVE_TOKEN = v
    except Exception as exc:
        logger.warning("load_active_token failed", error=str(exc)[:150])


async def rotate_admin_token() -> str:
    """Новый токен → bot_config → активный. Старые cookie сразу не подходят."""
    global _ACTIVE_TOKEN
    import secrets

    new = secrets.token_urlsafe(36)
    async with AsyncSessionFactory() as s:
        await set_config_value(s, "admin_token_active", new)
        await s.commit()
    _ACTIVE_TOKEN = new
    logger.warning("ADMIN TOKEN ROTATED via TG button")
    return new


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=302)


# ── LOGIN SECURITY: реальный IP, рейт-лимит, детект нового IP, TG-алерт ──────────
_LOGIN_FAIL_LIMIT = 5
_LOGIN_FAIL_WINDOW = 900  # 15 минут


_TRUSTED_PROXIES = {"127.0.0.1", "::1", "localhost"}


def _client_ip(request: Request) -> str:
    """Реальный IP клиента. X-Forwarded-For доверяем только если запрос
    пришёл от доверенного proxy; иначе используем фактический peer IP.
    Берём ПОСЛЕДНИЙ элемент XFF (добавленный ближайшим доверенным proxy),
    а не первый, который контролируется клиентом."""
    peer = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for")
    if peer in _TRUSTED_PROXIES and xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    # x-real-ip тоже только от доверенного proxy
    if peer in _TRUSTED_PROXIES:
        xri = request.headers.get("x-real-ip")
        if xri:
            return xri.strip()
    return peer


async def _login_blocked(ip: str) -> bool:
    try:
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            n = await r.get(f"login_fails:{ip}")
            return int(n or 0) >= _LOGIN_FAIL_LIMIT
        finally:
            await r.aclose()
    except Exception:
        return False  # fail-open: не лочим владельца, если Redis недоступен


async def _login_record_fail(ip: str) -> None:
    try:
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            k = f"login_fails:{ip}"
            if await r.incr(k) == 1:
                await r.expire(k, _LOGIN_FAIL_WINDOW)
        finally:
            await r.aclose()
    except Exception:
        pass


async def _login_clear_fails(ip: str) -> None:
    try:
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await r.delete(f"login_fails:{ip}")
        finally:
            await r.aclose()
    except Exception:
        pass


async def _is_new_admin_ip(ip: str) -> bool:
    """True если вход с НОВОГО IP (и регистрирует его в списке известных)."""
    from app.database.repo.bot_config import get_config_value

    try:
        async with AsyncSessionFactory() as s:
            raw = await get_config_value(s, "admin_known_ips")
            known = json.loads(raw) if raw else []
            if ip in known:
                return False
            known = (known + [ip])[-50:]
            await set_config_value(s, "admin_known_ips", json.dumps(known))
            await s.commit()
        return True
    except Exception:
        return False


async def _alert_new_login(ip: str) -> None:
    """TG-алерт владельцу об успешном входе в панель с НОВОГО IP."""
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Это НЕ я — сменить токен", callback_data="sec:rotate"
                    )
                ],
                [InlineKeyboardButton(text="✅ Это я — доверять IP", callback_data="sec:trust")],
            ]
        )
        bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            for aid in settings.admin_ids_list:
                await bot.send_message(
                    aid,
                    "🔐 <b>Вход в админ-панель с НОВОГО IP</b>\n\n"
                    f"IP: <code>{ip}</code>\n"
                    f"Время: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n\n"
                    "Если это <b>не ты</b> — жми «сменить токен»: чужая сессия "
                    "сбросится, новый токен пришлю сюда же.",
                    reply_markup=kb,
                )
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.warning("new-login alert failed", error=str(exc)[:150])


# ── REDIS HELPER (lazy) ───────────────────────────────────────────────────────

_redis_client: Redis | None = None


def _get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


# ── LOGIN ──────────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return render("login.html", error="")


@router.post("/login")
async def login(request: Request, token: str = Form(...)):
    ip = _client_ip(request)
    # Рейт-лимит: 5 неудач / 15 мин с одного IP.
    if await _login_blocked(ip):
        logger.warning("admin login BLOCKED (rate limit)", ip=ip)
        return render("login.html", error="Слишком много попыток. Подожди 15 минут.")
    if not _secrets.compare_digest(token or "", _ACTIVE_TOKEN or ""):
        await _login_record_fail(ip)
        logger.warning("admin login FAILED", ip=ip)
        log_action_bg("unknown", "admin_login_failed", ip=ip)
        return render("login.html", error="Неверный токен")
    # Успех: чистим счётчик, аудит, алерт если IP новый.
    await _login_clear_fails(ip)
    logger.info("admin login OK", ip=ip)
    log_action_bg("owner", "admin_login_ok", ip=ip)
    if await _is_new_admin_ip(ip):
        await _alert_new_login(ip)
    resp = RedirectResponse("/admin/", status_code=302)
    # secure=True — кука уходит только по HTTPS (дашборд за Caddy/TLS на
    # admin.example.com). Без флага утекала бы по plain HTTP (24.05 фикс).
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=86400 * 7,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return resp


@router.get("/logout")
async def logout():
    resp = _redirect_login()
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── DASHBOARD ──────────────────────────────────────────────────────────────────


async def _collect_alerts(fal_balance_usd: float | None, errors_24h: int) -> list[dict]:
    """Собирает условия, которые админу важно увидеть СРАЗУ.
    level: 'danger' | 'warning' | 'info'."""
    alerts: list[dict] = []
    if fal_balance_usd is not None:
        if fal_balance_usd < 1.0:
            alerts.append(
                {
                    "level": "danger",
                    "text": f"⚠ fal-баланс критически низкий: ${fal_balance_usd:.2f}. Пополни срочно.",
                }
            )
        elif fal_balance_usd < 5.0:
            alerts.append(
                {
                    "level": "warning",
                    "text": f"fal-баланс ниже $5 (${fal_balance_usd:.2f}). Подумай про пополнение.",
                }
            )
    if errors_24h and errors_24h > 10:
        alerts.append(
            {
                "level": "warning",
                "text": f"За 24ч в логах {errors_24h} ошибок — стоит глянуть Live-логи.",
            }
        )
    try:
        from app.database.models import ScheduledBroadcast

        async with AsyncSessionFactory() as session:
            stuck = await session.scalar(
                select(func.count())
                .select_from(ScheduledBroadcast)
                .where(
                    ScheduledBroadcast.status == "pending",
                    ScheduledBroadcast.send_at < datetime.now(UTC) - timedelta(hours=1),
                )
            )
            if stuck and stuck > 0:
                alerts.append(
                    {
                        "level": "danger",
                        "text": f"⚠ {stuck} отложенных рассылок зависли (>1ч после времени).",
                    }
                )
    except Exception:
        pass  # scheduled_broadcasts может ещё не существовать на холодном старте
    return alerts


@router.get("/", response_class=HTMLResponse)
async def dashboard(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()

    async with AsyncSessionFactory() as session:
        await ensure_defaults(session)
        stats = await get_dashboard_stats(session)
        recent_broadcasts = await get_broadcasts(session, limit=5)
        top_promos_result = await session.execute(
            select(PromoCode.code, PromoCode.credits, PromoCode.used_count, PromoCode.max_uses)
            .order_by(PromoCode.used_count.desc())
            .limit(5)
        )
        top_promos = [dict(row) for row in top_promos_result.mappings().all()]

    # fal-баланс: оценённый (быстро, по baseline – spent). Кнопка sync ниже
    # запросит реальный у fal API. Если baseline не выставлен → None.
    from app.services import fal_balance

    try:
        fal_balance_usd = await fal_balance.get_balance()
    except Exception:
        fal_balance_usd = None

    alerts = await _collect_alerts(fal_balance_usd, stats.get("errors_24h", 0))

    days = [d["day"] for d in stats["daily_new_users"]]
    new_user_counts = [d["cnt"] for d in stats["daily_new_users"]]
    # Фото-правки за 14 дней — главный график после пивота.
    edit_days = [d["day"] for d in stats["daily_image_edits"]]
    edit_counts = [d["cnt"] for d in stats["daily_image_edits"]]
    # Подтягиваем человеко-читаемые названия пресетов из рантайм-каталога
    # (хардкод + DB-кастомы), чтобы в топе не висели голые ключи.
    from app.handlers.image_edit import EDIT_PRESETS

    top_presets = [
        {**p, "label": EDIT_PRESETS.get(p["key"], (p["key"], None))[0]}
        for p in stats["top_presets_week"]
    ]

    return render(
        "dashboard.html",
        title="Обзор",
        pretitle="Аналитика",
        active="overview",
        msg=msg,
        stats=stats,
        recent_broadcasts=recent_broadcasts,
        top_promos=top_promos,
        top_presets=top_presets,
        fal_balance_usd=fal_balance_usd,
        alerts=alerts,
        chart_days=days,
        chart_new_users=new_user_counts,
        chart_edit_days=edit_days,
        chart_edit_counts=edit_counts,
    )


@router.post("/api/fal-sync")
async def fal_sync(request: Request, admin_session: str | None = Cookie(default=None)):
    """Запрашивает реальный баланс fal и обновляет локальный baseline.
    Полезно после пополнения или когда оценка ушла."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.services import fal_balance

    new_b = None
    try:
        new_b = await fal_balance.sync_baseline_from_real()
        msg = (
            f"fal-баланс синхронизирован: ${new_b:.2f}"
            if new_b is not None
            else "Не удалось получить баланс"
        )
    except Exception as exc:
        msg = f"Ошибка sync: {str(exc)[:80]}"
    log_action_bg(
        "token",
        "fal.sync",
        ip=request.client.host if request.client else None,
        balance=f"{new_b:.4f}" if new_b is not None else None,
    )
    from urllib.parse import quote

    return RedirectResponse(f"/admin/?msg={quote(msg)}", status_code=302)


# ── USERS ──────────────────────────────────────────────────────────────────────


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    admin_session: str | None = Cookie(default=None),
    search: str = "",
    plan: str = "",
    activity: str = "",
    credits_min: str = "",
    credits_max: str = "",
    page: int = 1,
    msg: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()

    per_page = 25
    offset = (page - 1) * per_page

    async with AsyncSessionFactory() as session:
        q = select(User).order_by(User.created_at.desc())
        filters = []
        if search:
            try:
                tg_id = int(search)
                filters.append(User.telegram_id == tg_id)
            except ValueError:
                filters.append(
                    or_(
                        User.username.ilike(f"%{search}%"),
                        User.full_name.ilike(f"%{search}%"),
                    )
                )
        if plan == "free":
            sub_active = select(Subscription.user_id).where(Subscription.is_active)
            filters.append(~User.id.in_(sub_active))
        elif plan == "paid":
            sub_active = select(Subscription.user_id).where(Subscription.is_active)
            filters.append(User.id.in_(sub_active))
        elif plan == "banned":
            filters.append(User.is_banned)

        now = datetime.now(UTC)
        if activity == "24h":
            filters.append(User.last_active_at >= now - timedelta(hours=24))
        elif activity == "7d":
            filters.append(User.last_active_at >= now - timedelta(days=7))
        elif activity == "inactive":
            filters.append(User.last_active_at < now - timedelta(days=14))

        try:
            if credits_min.strip():
                filters.append(User.credits >= int(credits_min))
            if credits_max.strip():
                filters.append(User.credits <= int(credits_max))
        except ValueError:
            pass

        if filters:
            q = q.where(and_(*filters))

        total = await session.scalar(select(func.count()).select_from(q.subquery())) or 0
        users = (await session.execute(q.offset(offset).limit(per_page))).scalars().all()
        # Сколько каждый из показанных юзеров пригласил (бейдж «🤝N» в списке).
        # Один агрегат-запрос на страницу: referrer_id (= telegram_id) → count.
        invited_counts = {}
        _tg_ids = [u.telegram_id for u in users]
        if _tg_ids:
            _rows = (
                await session.execute(
                    select(User.referrer_id, func.count())
                    .where(User.referrer_id.in_(_tg_ids))
                    .group_by(User.referrer_id)
                )
            ).all()
            invited_counts = {rid: cnt for rid, cnt in _rows}

    total_pages = max(1, (total + per_page - 1) // per_page)
    qs = f"search={search}&plan={plan}&activity={activity}&credits_min={credits_min}&credits_max={credits_max}"

    return render(
        "users/list.html",
        title="Пользователи",
        active="users",
        msg=msg,
        users=users,
        total=total,
        page=page,
        total_pages=total_pages,
        qs=qs,
        search=search,
        plan=plan,
        activity=activity,
        credits_min=credits_min,
        credits_max=credits_max,
        invited_counts=invited_counts,
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(
    user_id: int,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
    event_type: str = "",
    show_dialog: int = 0,
):
    if not _check_auth(admin_session):
        return _redirect_login()

    async with AsyncSessionFactory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if not user:
            return HTMLResponse("User not found", status_code=404)

        ev_q = (
            select(BotEvent)
            .where(BotEvent.user_id == user_id)
            .order_by(BotEvent.created_at.desc())
            .limit(50)
        )
        if event_type:
            ev_q = ev_q.where(BotEvent.event_type == event_type)
        recent_events = (await session.execute(ev_q)).scalars().all()

        payments_result = await session.execute(
            select(Payment)
            .where(Payment.user_id == user_id)
            .order_by(Payment.created_at.desc())
            .limit(20)
        )
        payments = payments_result.scalars().all()

        total_paid = (
            await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.user_id == user_id,
                    Payment.status == PaymentStatus.SUCCEEDED,
                    Payment.currency == "RUB",
                )
            )
            or 0
        )

        model_usage_rows = await session.execute(
            select(
                BotEvent.model_key,
                func.count().label("cnt"),
                func.sum(BotEvent.credits_spent).label("cr"),
            )
            .where(BotEvent.user_id == user_id, BotEvent.event_type == "message_sent")
            .group_by(BotEvent.model_key)
            .order_by(func.count().desc())
        )
        model_usage = [
            (r.model_key or "unknown", int(r.cnt), int(r.cr or 0)) for r in model_usage_rows
        ]

        event_type_rows = await session.execute(
            select(BotEvent.event_type, func.count().label("cnt"))
            .where(BotEvent.user_id == user_id)
            .group_by(BotEvent.event_type)
            .order_by(func.count().desc())
        )
        event_type_counts = [(r.event_type, int(r.cnt)) for r in event_type_rows]

        # ── Реферальная картина ──────────────────────────────────────────
        # Кто пригласил (referrer_id хранит telegram_id реферера).
        referrer = None
        if user.referrer_id:
            referrer = await session.scalar(
                select(User).where(User.telegram_id == user.referrer_id)
            )
        # Кого пригласил этот юзер + активировался ли каждый (сделал ≥1 фото →
        # выплата сработала). Pending (без фото) = реферер ещё НЕ получил кр.
        invitees = []
        invited_rows = (
            (
                await session.execute(
                    select(User)
                    .where(User.referrer_id == user.telegram_id)
                    .order_by(User.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if invited_rows:
            edited_ids = set(
                (
                    await session.execute(
                        select(BotEvent.user_id)
                        .where(
                            BotEvent.user_id.in_([iu.id for iu in invited_rows]),
                            BotEvent.event_type == "image_edited",
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            for iu in invited_rows:
                invitees.append({"u": iu, "activated": iu.id in edited_ids})
        # Сколько реально получил за рефералов (сумма amount в referral_paid).
        ref_paid_rows = (
            (
                await session.execute(
                    select(BotEvent).where(
                        BotEvent.user_id == user_id, BotEvent.event_type == "referral_paid"
                    )
                )
            )
            .scalars()
            .all()
        )
        ref_earned_total = sum(int((e.extra or {}).get("amount", 0)) for e in ref_paid_rows)
        ref_paid_count = len(ref_paid_rows)

    dialog = None
    if show_dialog:
        try:
            redis = _get_redis()
            dialog = await context_service.get_context(redis, user.telegram_id)
        except Exception as e:
            logger.warning("failed to load dialog from redis", error=str(e)[:200])

    return render(
        "users/detail.html",
        title=f"Пользователь #{user_id}",
        active="users",
        msg=msg,
        user=user,
        recent_events=recent_events,
        payments=payments,
        total_paid=total_paid,
        model_usage=model_usage,
        event_type_counts=event_type_counts,
        event_type=event_type,
        dialog=dialog,
        referrer=referrer,
        invitees=invitees,
        ref_earned_total=ref_earned_total,
        ref_paid_count=ref_paid_count,
    )


@router.get("/users/{user_id}/dialog", response_class=HTMLResponse)
async def user_dialog(user_id: int, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    return RedirectResponse(f"/admin/users/{user_id}?show_dialog=1", status_code=302)


@router.get("/users/{user_id}/chat", response_class=HTMLResponse)
async def user_chat_history(
    user_id: int,
    admin_session: str | None = Cookie(default=None),
    page: int = 1,
):
    if not _check_auth(admin_session):
        return _redirect_login()
    per_page = 100
    offset = (page - 1) * per_page
    async with AsyncSessionFactory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if not user:
            return HTMLResponse("User not found", status_code=404)
        total = (
            await session.scalar(
                select(func.count()).select_from(ChatMessage).where(ChatMessage.user_id == user_id)
            )
            or 0
        )
        messages = (
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.user_id == user_id)
                    .order_by(ChatMessage.created_at.desc())
                    .offset(offset)
                    .limit(per_page)
                )
            )
            .scalars()
            .all()
        )
        # Reverse to chronological order for readability
        messages = list(reversed(messages))
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render(
        "users/chat.html",
        title=f"Чат #{user_id}",
        active="users",
        user=user,
        messages=messages,
        total=total,
        page=page,
        total_pages=total_pages,
    )


@router.get("/users/{user_id}/chat/export.txt")
async def user_chat_export(user_id: int, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if not user:
            return Response("User not found", status_code=404)
        messages = (
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.user_id == user_id)
                    .order_by(ChatMessage.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    lines = [
        f"# Chat history — user #{user.id} (@{user.username or user.telegram_id})",
        f"# Total messages: {len(messages)}",
        "",
    ]
    for m in messages:
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else ""
        prefix = "👤 User" if m.role == "user" else f"🤖 Bot ({m.model_key or '-'})"
        lines.append(f"[{ts}] {prefix}:")
        lines.append(m.content)
        lines.append("")
    body = "\n".join(lines).encode("utf-8")
    return Response(
        body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="chat-{user_id}.txt"',
        },
    )


@router.post("/users/{user_id}/ban")
async def ban_user(
    user_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user:
            user.is_banned = not user.is_banned
            new_state = user.is_banned
            await session.commit()
            log_action_bg(
                "token",
                "user.ban" if new_state else "user.unban",
                target_user_id=user_id,
                ip=request.client.host if request.client else None,
                banned=new_state,
            )
    return RedirectResponse(f"/admin/users/{user_id}?msg=Статус+изменён", status_code=302)


@router.post("/users/{user_id}/credits")
async def adjust_credits(
    user_id: int,
    request: Request,
    amount: int = Form(...),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user:
            old_credits = user.credits
            user.credits = max(0, user.credits + amount)
            new_credits = user.credits
            await session.commit()
            log_action_bg(
                "token",
                "user.credits",
                target_user_id=user_id,
                ip=request.client.host if request.client else None,
                delta=amount,
                old=old_credits,
                new=new_credits,
            )
    return RedirectResponse(f"/admin/users/{user_id}?msg=Кредиты+обновлены", status_code=302)


# ── BULK USER ACTIONS ─────────────────────────────────────────────────────────


@router.post("/users/bulk")
async def users_bulk(request: Request, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    form = await request.form()
    action = form.get("action", "")
    value = (form.get("value", "") or "").strip()
    user_ids = [int(x) for x in form.getlist("user_ids") if x.isdigit()]

    if not user_ids or not action:
        return Response("No users or action specified", status_code=400)

    ip = request.client.host if request.client else None
    async with AsyncSessionFactory() as session:
        if action == "ban":
            await session.execute(update(User).where(User.id.in_(user_ids)).values(is_banned=True))
            await session.commit()
        elif action == "unban":
            await session.execute(update(User).where(User.id.in_(user_ids)).values(is_banned=False))
            await session.commit()
        elif action == "credits":
            try:
                delta = int(value.lstrip("+"))
            except ValueError:
                return Response("Invalid credit amount", status_code=400)
            users = (
                (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
            )
            for u in users:
                u.credits = max(0, u.credits + delta)
            await session.commit()
        elif action == "broadcast":
            if not value:
                return Response("No broadcast text", status_code=400)
            tg_ids = (
                (
                    await session.execute(
                        select(User.telegram_id).where(User.id.in_(user_ids), not User.is_banned)
                    )
                )
                .scalars()
                .all()
            )
            asyncio.create_task(_do_broadcast(list(tg_ids), value))
        else:
            return Response("Unknown action", status_code=400)

    log_action_bg(
        "token",
        f"user.bulk_{action}",
        ip=ip,
        count=len(user_ids),
        value=value[:200] if isinstance(value, str) else None,
    )
    return Response(status_code=200)


# ── PROMOS ─────────────────────────────────────────────────────────────────────


@router.get("/promos", response_class=HTMLResponse)
async def promos_list(
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
    promo: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()

    async with AsyncSessionFactory() as session:
        promos = await get_promo_stats(session)
        funnel_data = None
        if promo:
            funnel_data = await get_promo_conversion_funnel(session, promo.upper())

    totals = {
        "count": len(promos),
        "active": sum(1 for p in promos if p["is_active"]),
        "redemptions": sum(p["redemptions"] or 0 for p in promos),
        "credits_issued": sum((p["redemptions"] or 0) * (p["credits"] or 0) for p in promos),
    }

    return render(
        "promos.html",
        title="Промокоды и аналитика",
        active="promos",
        msg=msg,
        promos=promos,
        promo=promo.upper() if promo else "",
        funnel_data=funnel_data,
        totals=totals,
        bot_username=settings.BOT_USERNAME or "NeyraChatBot",
    )


@router.post("/promos/create")
async def create_promo(
    request: Request,
    code: str = Form(...),
    credits: int = Form(...),
    max_uses: int = Form(0),
    expires_days: int = Form(0),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    expires_at = datetime.now(UTC) + timedelta(days=expires_days) if expires_days > 0 else None
    norm_code = code.upper().strip()
    async with AsyncSessionFactory() as session:
        promo = PromoCode(code=norm_code, credits=credits, max_uses=max_uses, expires_at=expires_at)
        session.add(promo)
        await session.commit()
    log_action_bg(
        "token",
        "promo.create",
        ip=request.client.host if request.client else None,
        code=norm_code,
        credits=credits,
        max_uses=max_uses,
        expires_days=expires_days,
    )
    return RedirectResponse("/admin/promos?msg=Промокод+создан", status_code=302)


@router.post("/promos/{promo_id}/deactivate")
async def deactivate_promo(
    promo_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(PromoCode).where(PromoCode.id == promo_id).values(is_active=False)
        )
        await session.commit()
    log_action_bg(
        "token",
        "promo.deactivate",
        ip=request.client.host if request.client else None,
        promo_id=promo_id,
    )
    return RedirectResponse("/admin/promos?msg=Промокод+отключён", status_code=302)


# /admin/settings удалён — все его уникальные поля (image_promo_*, test_broadcast_username)
# перенесены в /admin/pricing. Остальное было дублем /pricing.


# ── PRICING (models + packs) ───────────────────────────────────────────────────


@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        await ensure_defaults(session)
        config = await get_all_config(session)

    # После пивота visual-first дорогие LLM (GPT-5 / Claude Sonnet/Opus) и
    # сам чат убраны с этой страницы — ключи model_cost_basic/mid/pro/ultra/
    # vision/free остаются в bot_config для обратной совместимости, но не
    # редактируются здесь (чат вторичен и работает на бесплатной DeepSeek).
    # Реальные деньги — фото-операции (ниже) + пакеты/подписки.
    model_rows = [
        ("model_cost_image", "Картинка с нуля", "🎨", "purple"),
        ("model_cost_voice", "Голос (транскрипция)", "🎤", "success"),
    ]
    # Подсветим РЕАЛЬНЫЕ текущие цены фото-операций (read-only, идут из кода).
    from app.database.repo.credits import ALL_PACKS, MODEL_COSTS

    photo_prices = [
        ("Правка фото · quality (2K)", MODEL_COSTS.get("image_edit", 0)),
        ("Правка фото · fast", MODEL_COSTS.get("image_edit_fast", 0)),
        ("Face-swap", MODEL_COSTS.get("face_swap", 0)),
    ]
    # Строим из реальных ALL_PACKS — таблица не дрейфует при смене витрины.
    packs = [
        (p["id"], f"{p['emoji']} {p['name']}", str(p["credits"]), str(p["rub"])) for p in ALL_PACKS
    ]
    return render(
        "pricing.html",
        title="Цены",
        active="pricing",
        msg=msg,
        config=config,
        model_rows=model_rows,
        packs=packs,
        photo_prices=photo_prices,
    )


@router.post("/pricing")
async def save_pricing(request: Request, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    form = await request.form()
    changed: dict[str, str] = {}
    INT_KEYS = (
        "daily_free_credits",
        "referral_bonus",
        "image_promo_discount",
        "welcome_bonus_credits",
    )
    STR_KEYS = ("image_promo_until", "test_broadcast_username", "yookassa_recurring_enabled")
    async with AsyncSessionFactory() as session:
        for k in form:
            v = str(form[k])
            if k.startswith("model_cost_") or k.startswith("pack_") or k in INT_KEYS:
                try:
                    int(v)
                except (ValueError, TypeError):
                    continue
                await set_config_value(session, k, v)
                changed[k] = v
            elif k in STR_KEYS:
                await set_config_value(session, k, v)
                changed[k] = v
        await session.commit()
    log_action_bg(
        "token",
        "pricing.update",
        ip=request.client.host if request.client else None,
        changes=changed,
    )
    return RedirectResponse("/admin/pricing?msg=Цены+обновлены", status_code=302)


# ── BOT TEXTS ──────────────────────────────────────────────────────────────────


@router.get("/texts", response_class=HTMLResponse)
async def texts_page(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        await ensure_defaults(session)
        config = await get_all_config(session)

    text_rows = [
        (
            "text_social_proof",
            "🌟 Социальное доказательство (над welcome)",
            "Короткая строка над приветствием. Пусто = скрыть.",
            "🌟 <b>Уже пользуются 13 000+ человек</b> — присоединяйся",
        ),
        (
            "text_welcome",
            "👋 Приветствие (/start)",
            "Показывается новым и существующим пользователям при /start. Поддерживается HTML.",
            WELCOME_TEXT,
        ),
        (
            "text_help",
            "ℹ️ Справка (/help)",
            "Показывается при команде /help. Поддерживается HTML.",
            HELP_TEXT,
        ),
    ]
    return render(
        "texts.html",
        title="Тексты бота",
        active="texts",
        msg=msg,
        config=config,
        text_rows=text_rows,
    )


@router.post("/texts")
async def save_texts(request: Request, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    form = await request.form()
    keys_changed: list[str] = []
    async with AsyncSessionFactory() as session:
        for k in form:
            if k.startswith("text_"):
                await set_config_value(session, k, str(form[k]))
                keys_changed.append(k)
        await session.commit()
    log_action_bg(
        "token",
        "texts.update",
        ip=request.client.host if request.client else None,
        keys=keys_changed,
    )
    return RedirectResponse("/admin/texts?msg=Тексты+сохранены", status_code=302)


# ── BROADCAST (sender) ─────────────────────────────────────────────────────────


@router.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.bot_config import get_config_value

    async with AsyncSessionFactory() as session:
        total = await session.scalar(
            select(func.count()).select_from(User).where(not User.is_banned)
        )
        test_user = (await get_config_value(session, "test_broadcast_username") or "grelok").lstrip(
            "@"
        )
    return render(
        "broadcasts/send.html",
        title="Новая рассылка",
        active="broadcasts",
        msg=msg,
        total=total or 0,
        test_user=test_user,
    )


async def _do_broadcast(user_ids: list[int], text: str) -> None:
    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        logger.error("Broadcast: bot not available")
        return
    sent, failed = 0, 0
    for tg_id in user_ids:
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    logger.info("Broadcast complete", sent=sent, failed=failed)


@router.post("/broadcast")
async def do_broadcast(
    request: Request,
    text: str = Form(...),
    audience: str = Form("all"),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()

    async with AsyncSessionFactory() as session:
        if audience == "test_user":
            user_ids = await _resolve_test_user_tg_id(session)
        elif audience == "admin_only":
            user_ids = list(settings.admin_ids_list or [])
        else:
            q = select(User.telegram_id).where(not User.is_banned)
            if audience == "free":
                subq = select(Subscription.user_id).where(Subscription.is_active)
                q = q.where(~User.id.in_(subq))
            elif audience == "paid":
                subq = select(Subscription.user_id).where(Subscription.is_active)
                q = q.where(User.id.in_(subq))
            result = await session.execute(q)
            user_ids = [row[0] for row in result.all()]

    asyncio.create_task(_do_broadcast(user_ids, text))
    log_action_bg(
        "token",
        "broadcast.send",
        ip=request.client.host if request.client else None,
        audience=audience,
        count=len(user_ids),
        text=text[:200],
    )
    return RedirectResponse(
        f"/admin/broadcast?msg=Рассылка+запущена+для+{len(user_ids)}+пользователей", status_code=302
    )


# ── EVENTS ─────────────────────────────────────────────────────────────────────


@router.get("/events", response_class=HTMLResponse)
async def events_page(
    admin_session: str | None = Cookie(default=None),
    event_type: str = "",
    days: int = 7,
    user_tg: str = "",
    limit: int = 200,
):
    if not _check_auth(admin_session):
        return _redirect_login()

    days = max(1, min(days, 365))
    limit = max(50, min(limit, 1000))
    since = datetime.now(UTC) - timedelta(days=days)

    async with AsyncSessionFactory() as session:
        q = (
            select(BotEvent, User.username, User.telegram_id, User.id.label("uid"))
            .outerjoin(User, User.id == BotEvent.user_id)
            .where(BotEvent.created_at >= since)
            .order_by(BotEvent.created_at.desc())
        )
        if event_type:
            q = q.where(BotEvent.event_type == event_type)
        if user_tg.strip():
            try:
                q = q.where(User.telegram_id == int(user_tg.strip()))
            except ValueError:
                pass

        type_counts_rows = await session.execute(
            select(BotEvent.event_type, func.count().label("cnt"))
            .where(BotEvent.created_at >= since)
            .group_by(BotEvent.event_type)
            .order_by(func.count().desc())
        )
        type_counts = [(r.event_type, int(r.cnt)) for r in type_counts_rows]

        items = (await session.execute(q.limit(limit))).all()

    return render(
        "events.html",
        title="События",
        active="events",
        items=items,
        type_counts=type_counts,
        event_type=event_type,
        days=days,
        user_tg=user_tg,
        limit=limit,
    )


# ── REALTIME LOG STREAM (SSE) ──────────────────────────────────────────────────


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    # active='events' — logs и events слиты в один пункт меню «События»
    # (вкладки «Живой поток» / «История»).
    return render("logs.html", title="События · живой поток", pretitle="SSE", active="events")


def _ev_payload(ev: BotEvent, uname: str | None, tg_id: int | None, uid: int | None) -> dict:
    return {
        "id": ev.id,
        "event_type": ev.event_type,
        "model_key": ev.model_key,
        "credits_spent": ev.credits_spent,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
        "user_id": uid,
        "telegram_id": tg_id,
        "username": uname,
    }


@router.get("/api/events/stream")
async def events_stream(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return Response(status_code=403)

    async def event_gen():
        # Send backlog of last 50 events first so the page is never empty
        async with AsyncSessionFactory() as s:
            rows = (
                await s.execute(
                    select(BotEvent, User.username, User.telegram_id, User.id.label("uid"))
                    .outerjoin(User, User.id == BotEvent.user_id)
                    .order_by(BotEvent.id.desc())
                    .limit(50)
                )
            ).all()
            last_id = max((r[0].id for r in rows), default=0)
        # Emit backlog in chronological order (oldest first) so newer appear on top after JS prepend
        for ev, uname, tg_id, uid in reversed(rows):
            yield f"data: {json.dumps(_ev_payload(ev, uname, tg_id, uid), ensure_ascii=False)}\n\n"
        yield ": backlog-done\n\n"

        while True:
            try:
                async with AsyncSessionFactory() as session:
                    rows = (
                        await session.execute(
                            select(BotEvent, User.username, User.telegram_id, User.id.label("uid"))
                            .outerjoin(User, User.id == BotEvent.user_id)
                            .where(BotEvent.id > last_id)
                            .order_by(BotEvent.id.asc())
                            .limit(50)
                        )
                    ).all()
                for ev, uname, tg_id, uid in rows:
                    last_id = max(last_id, ev.id)
                    yield f"data: {json.dumps(_ev_payload(ev, uname, tg_id, uid), ensure_ascii=False)}\n\n"
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("SSE stream error", error=str(exc)[:200])
            await asyncio.sleep(2)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── PHOTO TEMPLATES (viral face-swap broadcasts) ───────────────────────────────

# In-memory cache for re-using TG file_id (already uploaded to TG → no re-upload)
# Keyed by photo_template_id. Stored in DB after first upload.


@router.get("/broadcasts/photo", response_class=HTMLResponse)
async def photo_templates_list(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        templates = (
            (await session.execute(select(PhotoTemplate).order_by(PhotoTemplate.created_at.desc())))
            .scalars()
            .all()
        )
    return render(
        "broadcasts/photo_list.html",
        title="Фото-шаблоны",
        pretitle="Виральные рассылки",
        active="photo_tpl",
        templates=templates,
    )


@router.get("/broadcasts/photo/new", response_class=HTMLResponse)
async def photo_templates_new(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    return render("broadcasts/photo_new.html", title="Новый фото-шаблон", active="photo_tpl")


@router.post("/broadcasts/photo/create")
async def photo_templates_create(
    request: Request,
    photo: UploadFile = File(...),
    caption: str = Form(""),
    prompt_used: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    # Upload photo to TG by sending to admin (silent) → get file_id, save for reuse
    content = await photo.read()
    if not content or len(content) > 10 * 1024 * 1024:
        return RedirectResponse(
            "/admin/broadcasts/photo/new?msg=Файл+слишком+большой+или+пустой", status_code=302
        )
    if len(caption) > 1000:
        caption = caption[:1000]

    from aiogram.types import BufferedInputFile

    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        return Response("Bot unavailable", status_code=500)

    admin_ids = settings.admin_ids_list
    if not admin_ids:
        return Response("No admin IDs configured", status_code=500)

    try:
        sent = await bot.send_photo(
            admin_ids[0],
            BufferedInputFile(content, filename="template.jpg"),
            caption="📋 <i>Этот шаблон сохранён в БД</i>",
            parse_mode="HTML",
        )
        # Pick the highest-res version's file_id
        file_id = sent.photo[-1].file_id
    except Exception as exc:
        logger.error("photo upload to TG failed", error=str(exc)[:300])
        return Response(f"TG upload failed: {exc}", status_code=500)

    async with AsyncSessionFactory() as session:
        tpl = PhotoTemplate(
            file_id=file_id,
            caption=caption,
            prompt_used=prompt_used,
        )
        session.add(tpl)
        await session.commit()
        tpl_id = tpl.id

    log_action_bg(
        "token",
        "photo_template.create",
        ip=request.client.host if request.client else None,
        tpl_id=tpl_id,
        caption_len=len(caption),
    )
    return RedirectResponse(f"/admin/broadcasts/photo/{tpl_id}?msg=Шаблон+создан", status_code=302)


@router.get("/broadcasts/photo/{tpl_id}", response_class=HTMLResponse)
async def photo_templates_detail(
    tpl_id: int,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.bot_config import get_config_value

    async with AsyncSessionFactory() as session:
        tpl = await session.scalar(select(PhotoTemplate).where(PhotoTemplate.id == tpl_id))
        if not tpl:
            return HTMLResponse("Не найдено", status_code=404)
        total_all = (
            await session.scalar(select(func.count()).select_from(User).where(not User.is_banned))
            or 0
        )
        test_user = (await get_config_value(session, "test_broadcast_username") or "grelok").lstrip(
            "@"
        )
    return render(
        "broadcasts/photo_detail.html",
        title=f"Шаблон #{tpl_id}",
        active="photo_tpl",
        msg=msg,
        tpl=tpl,
        total_all=total_all,
        test_user=test_user,
    )


@router.get("/broadcasts/photo/{tpl_id}/thumb")
async def photo_templates_thumb(tpl_id: int, admin_session: str | None = Cookie(default=None)):
    """Proxy preview of TG photo by file_id, so admin can see it in the dashboard."""
    if not _check_auth(admin_session):
        return Response(status_code=403)
    async with AsyncSessionFactory() as session:
        tpl = await session.scalar(select(PhotoTemplate).where(PhotoTemplate.id == tpl_id))
    if not tpl:
        return Response(status_code=404)
    try:
        from app.bot import get_bot

        bot = get_bot()
        if not bot:
            return Response(status_code=503)
        tg_file = await bot.get_file(tpl.file_id)
        body = await bot.download_file(tg_file.file_path)
        return Response(
            content=body.read(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception as exc:
        logger.warning("thumb proxy failed", tpl_id=tpl_id, error=str(exc)[:200])
        return Response(status_code=500)


async def _resolve_test_user_tg_id(session) -> list[int]:
    """Returns [telegram_id] for configured test-broadcast user, or [] if missing."""
    from app.database.repo.bot_config import get_config_value

    username = (
        (await get_config_value(session, "test_broadcast_username") or "").lstrip("@").strip()
    )
    if not username:
        return []
    user = await session.scalar(select(User).where(User.username.ilike(username)))
    return [user.telegram_id] if user else []


async def _do_photo_template_broadcast(tpl_id: int, audience: str) -> None:
    """Sends the photo template to the chosen audience and updates counters."""
    from aiogram.types import InlineKeyboardButton as IB
    from aiogram.types import InlineKeyboardMarkup as IM

    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        logger.error("photo_tpl broadcast: bot unavailable")
        return

    # Build audience query
    async with AsyncSessionFactory() as session:
        tpl = await session.scalar(select(PhotoTemplate).where(PhotoTemplate.id == tpl_id))
        if not tpl:
            return

        if audience == "admin_only":
            tg_ids = list(settings.admin_ids_list or [])
        elif audience == "test_user":
            tg_ids = await _resolve_test_user_tg_id(session)
        else:
            q = select(User.telegram_id).where(not User.is_banned)
            if audience == "free":
                subq = select(Subscription.user_id).where(Subscription.is_active)
                q = q.where(~User.id.in_(subq))
            elif audience == "paid":
                subq = select(Subscription.user_id).where(Subscription.is_active)
                q = q.where(User.id.in_(subq))
            elif audience == "low_credits":
                q = q.where(User.credits <= 100)
            res = await session.execute(q)
            tg_ids = [row[0] for row in res.all()]

    kb = IM(
        inline_keyboard=[
            [
                IB(text="Сделать с моим лицом", callback_data=f"tpl_face_swap:{tpl_id}"),
            ]
        ]
    )
    full_caption = tpl.caption or "🎭 Хочешь так же? Подставь своё лицо в этот кадр."
    if tpl.prompt_used:
        full_caption += f"\n\n<i>Промпт: «{tpl.prompt_used[:200]}»</i>"

    sent = blocked = errors = 0
    for tg_id in tg_ids:
        try:
            await bot.send_photo(
                tg_id, tpl.file_id, caption=full_caption, parse_mode="HTML", reply_markup=kb
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as exc:
            err = str(exc).lower()
            if "forbidden" in err or "blocked" in err or "deactivated" in err:
                blocked += 1
            else:
                errors += 1
                logger.warning("photo_tpl send failed", tg_id=tg_id, error=str(exc)[:200])

    # Update counters
    async with AsyncSessionFactory() as session:
        tpl = await session.scalar(select(PhotoTemplate).where(PhotoTemplate.id == tpl_id))
        if tpl:
            tpl.sent_count += sent
            await session.commit()

    # Записать в broadcasts table для аналитики
    async with AsyncSessionFactory() as session:
        b = Broadcast(
            source="neyra",
            promo_code=f"PHOTO_TPL_{tpl_id}",
            total_recipients=len(tg_ids),
            sent_count=sent,
            blocked_count=blocked,
            error_count=errors,
            completed_at=datetime.now(UTC),
            status="completed",
        )
        session.add(b)
        await session.commit()

    logger.info(
        "photo_tpl broadcast done",
        tpl_id=tpl_id,
        audience=audience,
        sent=sent,
        blocked=blocked,
        errors=errors,
    )


@router.post("/broadcasts/photo/{tpl_id}/send")
async def photo_templates_send(
    tpl_id: int,
    request: Request,
    audience: str = Form("admin_only"),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    asyncio.create_task(_do_photo_template_broadcast(tpl_id, audience))
    log_action_bg(
        "token",
        "photo_template.send",
        ip=request.client.host if request.client else None,
        tpl_id=tpl_id,
        audience=audience,
    )
    return RedirectResponse(
        f"/admin/broadcasts/photo/{tpl_id}?msg=Рассылка+запущена", status_code=302
    )


@router.post("/broadcasts/photo/{tpl_id}/delete")
async def photo_templates_delete(
    tpl_id: int,
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        await session.execute(PhotoTemplate.__table__.delete().where(PhotoTemplate.id == tpl_id))
        await session.commit()
    log_action_bg(
        "token",
        "photo_template.delete",
        ip=request.client.host if request.client else None,
        tpl_id=tpl_id,
    )
    return RedirectResponse("/admin/broadcasts/photo?msg=Удалено", status_code=302)


# ── BULK ACTIONS (массовые операции с сегментами юзеров) ──────────────────────
@router.get("/bulk", response_class=HTMLResponse)
async def bulk_page(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    now = datetime.now(UTC)
    async with AsyncSessionFactory() as session:
        sub_active = select(Subscription.user_id).where(Subscription.is_active)
        counts = {
            "active_7d": await session.scalar(
                select(func.count())
                .select_from(User)
                .where(not User.is_banned, User.last_active_at >= now - timedelta(days=7))
            )
            or 0,
            "active_14d": await session.scalar(
                select(func.count())
                .select_from(User)
                .where(not User.is_banned, User.last_active_at >= now - timedelta(days=14))
            )
            or 0,
            "free": await session.scalar(
                select(func.count())
                .select_from(User)
                .where(not User.is_banned, ~User.id.in_(sub_active))
            )
            or 0,
            "paid": await session.scalar(
                select(func.count())
                .select_from(User)
                .where(not User.is_banned, User.id.in_(sub_active))
            )
            or 0,
            "all": await session.scalar(
                select(func.count()).select_from(User).where(not User.is_banned)
            )
            or 0,
        }
    return render("bulk.html", title="Массовые операции", active="bulk", msg=msg, counts=counts)


async def _do_bulk_credits(segment: str, amount: int, notify_text: str) -> None:
    """Начисляет credit-бонус всем юзерам сегмента + (опционально) шлёт TG-сообщение."""
    from app.bot import get_bot
    from app.database.repo.credits import add_credits

    now = datetime.now(UTC)
    bot = get_bot()
    async with AsyncSessionFactory() as session:
        sub_active = select(Subscription.user_id).where(Subscription.is_active)
        q = select(User).where(not User.is_banned)
        if segment == "active_7d":
            q = q.where(User.last_active_at >= now - timedelta(days=7))
        elif segment == "active_14d":
            q = q.where(User.last_active_at >= now - timedelta(days=14))
        elif segment == "free":
            q = q.where(~User.id.in_(sub_active))
        elif segment == "paid":
            q = q.where(User.id.in_(sub_active))
        users = list((await session.execute(q)).scalars().all())
        for u in users:
            await add_credits(session, u, amount)
        await session.commit()
        user_ids_tg = [u.telegram_id for u in users]
    sent = failed = 0
    if notify_text and bot:
        full_text = f"{notify_text}\n\n🎁 <b>+{amount} кр</b> уже на счету."
        for tg in user_ids_tg:
            try:
                await bot.send_message(tg, full_text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
    logger.info(
        "bulk credits done",
        segment=segment,
        amount=amount,
        users=len(user_ids_tg),
        notify_sent=sent,
        notify_failed=failed,
    )


@router.post("/bulk/credits")
async def bulk_credits(
    request: Request,
    segment: str = Form(...),
    amount: int = Form(...),
    notify_text: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    if amount <= 0 or amount > 10000:
        return RedirectResponse("/admin/bulk?msg=Некорректная+сумма+(1-10000)", status_code=302)
    if segment not in ("active_7d", "active_14d", "free", "paid", "all"):
        return RedirectResponse("/admin/bulk?msg=Неверный+сегмент", status_code=302)
    asyncio.create_task(_do_bulk_credits(segment, amount, notify_text.strip()))
    log_action_bg(
        "token",
        "bulk.credits",
        ip=request.client.host if request.client else None,
        segment=segment,
        amount=amount,
        notify=bool(notify_text.strip()),
    )
    return RedirectResponse("/admin/bulk?msg=Бонус+начислен+(работает+в+фоне)", status_code=302)


# ── PREVIEW GALLERY ───────────────────────────────────────────────────────────
# Все превью одним экраном — визуальная сетка муж+жен по всем пресетам
# (хардкод + кастом), фильтр по полу и категории. Удобно глянуть «что в
# каталоге у юзера» без захода в Telegram.
@router.get("/gallery", response_class=HTMLResponse)
async def gallery_page(
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
    g: str = "all",
    cat: str = "all",
    f: str = "all",
    sort: str = "alpha",
):
    """Галерея всех превью с действиями. Фильтры: пол (g), категория (cat),
    состояние (f: all/missing/custom/featured), сортировка (sort)."""
    if not _check_auth(admin_session):
        return _redirect_login()
    import app.handlers.image_edit as ie
    from app.database.repo.custom_presets import list_custom_presets
    from app.database.repo.preset_launch import get_launch_map
    from app.database.repo.preset_previews import get_preview_file_id
    from app.handlers.image_edit import _FEMALE_ONLY_PRESETS, EDIT_PRESETS, PRESET_PAGES

    # Актуальный состав «🆕 Новинки» (для бейджа и кнопки «Сделать новым»).
    await ie.refresh_fresh_presets()
    badge_keys = ie._BADGE_KEYS

    key_to_cat: dict[str, str] = {}
    for page in PRESET_PAGES:
        for k in page["items"]:
            key_to_cat.setdefault(k, page["title"])
    categories = [p["title"] for p in PRESET_PAGES]
    cutoff_30d = datetime.now(UTC) - timedelta(days=30)

    async with AsyncSessionFactory() as session:
        customs = await list_custom_presets(session)
        custom_map = {c.key: c for c in customs}
        launch_map = await get_launch_map(session)
        from app.database.repo.events import get_excluded_user_ids

        exclude_uids = await get_excluded_user_ids(session)
        # Стат по генерациям за 30д. Тех-юзеры (admin + @grelok) исключены —
        # иначе тестер с сотнями ген. забивает топ.
        preset_key_expr = BotEvent.extra.op("->>")("preset_key")
        stat_rows = await session.execute(
            select(
                preset_key_expr.label("k"),
                func.count().label("cnt"),
                func.max(BotEvent.created_at).label("last"),
            )
            .where(
                BotEvent.event_type == "image_edited",
                BotEvent.created_at >= cutoff_30d,
                preset_key_expr.isnot(None),
                BotEvent.user_id.notin_(exclude_uids),
            )
            .group_by(preset_key_expr)
        )
        stats_map = {r.k: {"cnt": r.cnt, "last": r.last} for r in stat_rows.all()}

        items = []
        for key, (label, _prompt) in EDIT_PRESETS.items():
            category = key_to_cat.get(key, "🆕 Новинки")
            if cat != "all" and category != cat:
                continue
            f0 = await get_preview_file_id(session, key, position=0)
            f1 = await get_preview_file_id(session, key, position=1)
            custom = custom_map.get(key)
            is_female_only = (key in _FEMALE_ONLY_PRESETS) or (
                custom.female_only if custom else False
            )
            is_featured = bool(custom and custom.featured)
            if g == "male" and (is_female_only or not f1):
                continue
            if g == "female" and not f0:
                continue
            # Фильтр по состоянию
            if f == "missing" and (f0 or f1):
                continue
            if f == "custom" and custom is None:
                continue
            if f == "featured" and not is_featured:
                continue
            s = stats_map.get(key, {"cnt": 0, "last": None})
            items.append(
                {
                    "key": key,
                    "label": label,
                    "category": category,
                    "has_f": bool(f0),
                    "has_m": bool(f1),
                    "female_only": is_female_only,
                    "is_custom": custom is not None,
                    "custom_id": custom.id if custom else None,
                    "featured": is_featured,
                    "gen_count_30d": s["cnt"],
                    "last_used": s["last"],
                    "is_new": key in badge_keys,
                    "launched_at": launch_map.get(key),
                }
            )
        # Сортировка
        if sort == "top":
            items.sort(key=lambda i: (-i["gen_count_30d"], i["label"]))
        elif sort == "recent":
            items.sort(
                key=lambda i: (
                    i["last_used"] is None,
                    -(i["last_used"].timestamp() if i["last_used"] else 0),
                )
            )
        else:
            items.sort(key=lambda i: i["label"])
    return render(
        "gallery.html",
        title="Галерея превью",
        active="gallery",
        msg=msg,
        items=items,
        categories=categories,
        current_g=g,
        current_cat=cat,
        current_f=f,
        current_sort=sort,
        total=len(items),
    )


def _gallery_resolve_prompt(key: str) -> tuple[str, str, str | None, bool, int | None]:
    """Возвращает (label, prompt, male_prompt, female_only, custom_id) для любого
    пресета (хардкод или DB). Для хардкод-пресета custom_id=None."""
    from app.handlers.image_edit import (
        _FEMALE_ONLY_PRESETS,
        _MALE_PRESET_OVERRIDES,
        EDIT_PRESETS,
    )

    label, prompt = EDIT_PRESETS.get(key, (key, ""))
    male_prompt = _MALE_PRESET_OVERRIDES.get(key)
    female_only = key in _FEMALE_ONLY_PRESETS
    return label, prompt, male_prompt, female_only, None  # custom_id заполнит вызвавший


@router.post("/gallery/{key}/regen")
async def gallery_regen(
    key: str,
    request: Request,
    pos: str = Form("both"),
    admin_session: str | None = Cookie(default=None),
):
    """Перегенерить превью пресета по ключу. pos: 'both' / 'female' / 'male'.
    Цена ~$0.12 за каждое (fal 2K)."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.handlers.image_edit import EDIT_PRESETS

    if key not in EDIT_PRESETS:
        return RedirectResponse("/admin/gallery?msg=Пресет+не+найден", status_code=302)
    label, prompt, male_prompt, female_only, _ = _gallery_resolve_prompt(key)
    if not prompt:
        return RedirectResponse("/admin/gallery?msg=У+пресета+нет+промпта", status_code=302)
    positions = None
    if pos == "female":
        positions = {0}
    elif pos == "male":
        positions = {1}
    asyncio.create_task(
        _do_generate_preview_by_key(
            key,
            label,
            prompt,
            male_prompt,
            female_only,
            positions=positions,
        )
    )
    log_action_bg(
        "token",
        "gallery.regen",
        ip=request.client.host if request.client else None,
        key=key,
        pos=pos,
    )
    return RedirectResponse(
        "/admin/gallery?msg=Регенерю+превью+(fal+~$0.12+за+каждое)+—+результат+в+Telegram",
        status_code=302,
    )


@router.post("/gallery/{key}/delete-thumb/{position}")
async def gallery_delete_thumb(
    key: str,
    position: int,
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    """Удалить конкретное превью (положение 0/1). Сам пресет не трогает."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.models import PresetPreview

    async with AsyncSessionFactory() as session:
        await session.execute(
            PresetPreview.__table__.delete().where(
                PresetPreview.preset_key == key,
                PresetPreview.position == position,
            )
        )
        await session.commit()
    # Удалили превью, попавшее в welcome-коллаж — сбрасываем кэш.
    try:
        from app.services.welcome_collage import invalidate_welcome_collage, is_collage_preset

        if is_collage_preset(key, position):
            await invalidate_welcome_collage()
    except Exception:
        pass
    log_action_bg(
        "token",
        "gallery.delete_thumb",
        ip=request.client.host if request.client else None,
        key=key,
        pos=position,
    )
    return RedirectResponse("/admin/gallery?msg=Превью+удалено", status_code=302)


@router.get("/gallery/{key}/broadcast", response_class=HTMLResponse)
async def gallery_broadcast_form(
    key: str,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
):
    """Форма рассылки по любому пресету из галереи (хардкод или custom)."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.handlers.image_edit import EDIT_PRESETS, NO_BROADCAST_PRESETS

    if key not in EDIT_PRESETS:
        return HTMLResponse("Пресет не найден", status_code=404)
    if key in NO_BROADCAST_PRESETS:
        return RedirectResponse(
            f"/admin/gallery?msg=Пресет+{key}+в+блок-листе+рассылок+(провокация)", status_code=302
        )
    from app.database.repo.preset_previews import get_preview_file_id

    async with AsyncSessionFactory() as session:
        has_male = bool(await get_preview_file_id(session, key, position=1))
        has_female = bool(await get_preview_file_id(session, key, position=0))
        total_all = (
            await session.scalar(select(func.count()).select_from(User).where(not User.is_banned))
            or 0
        )
    label = EDIT_PRESETS[key][0]
    default_caption = (
        f"🔥 <b>Новая сцена в Нейре: {label}</b>\n\n"
        "Лицо в кадре — твоё. И для парней, и для девушек.\n\n"
        "<b>Как сделать:</b>\n1️⃣ Жми кнопку ниже\n"
        "2️⃣ Укажи, для кого фото\n3️⃣ Пришли селфи и выбери сцену 👇"
    )
    return render(
        "gallery_broadcast.html",
        title=f"Рассылка: {label}",
        active="gallery",
        msg=msg,
        key=key,
        label=label,
        has_male=has_male,
        has_female=has_female,
        default_caption=default_caption,
        total_all=total_all,
    )


@router.post("/gallery/{key}/broadcast")
async def gallery_broadcast(
    key: str,
    request: Request,
    audience: str = Form("all"),
    caption: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.handlers.image_edit import EDIT_PRESETS, NO_BROADCAST_PRESETS

    if key not in EDIT_PRESETS:
        return RedirectResponse("/admin/gallery?msg=Пресет+не+найден", status_code=302)
    if key in NO_BROADCAST_PRESETS:
        return RedirectResponse(
            f"/admin/gallery?msg=Пресет+{key}+в+блок-листе+рассылок+(провокация)", status_code=302
        )
    label = EDIT_PRESETS[key][0]
    asyncio.create_task(_do_broadcast_by_key(key, label, audience, caption))
    log_action_bg(
        "token",
        "gallery.broadcast",
        ip=request.client.host if request.client else None,
        key=key,
        audience=audience,
        caption_len=len(caption),
    )
    return RedirectResponse("/admin/gallery?msg=Рассылка+запущена", status_code=302)


@router.post("/gallery/{key}/toggle-featured")
async def gallery_toggle_featured(
    key: str,
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    """Featured toggle с галереи — работает только для custom-пресетов
    (у хардкодных нет записи в БД, для них featured концептуально невозможен
    без миграции — TODO если попросишь)."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import get_by_key, toggle_featured
    from app.handlers.image_edit import reload_custom_presets

    async with AsyncSessionFactory() as session:
        preset = await get_by_key(session, key)
        if not preset:
            return RedirectResponse(
                "/admin/gallery?msg=Featured+доступен+только+для+custom-пресетов", status_code=302
            )
        new_state = await toggle_featured(session, preset.id)
        await session.commit()
    await reload_custom_presets()
    log_action_bg(
        "token",
        "gallery.toggle_featured",
        ip=request.client.host if request.client else None,
        key=key,
        featured=new_state,
    )
    return RedirectResponse("/admin/gallery?msg=Featured+обновлён", status_code=302)


@router.post("/gallery/{key}/mark-new")
async def gallery_mark_new(
    key: str,
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    """Снова сделать пресет «новым»: launched_at=now → попадёт в «🆕 Новинки»
    на окно свежести. Работает для ЛЮБОГО пресета (код или DB) — реестр
    preset_launch keyed by preset_key, миграция не нужна (в отличие от featured)."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from datetime import datetime

    from app.database.repo.preset_launch import set_launch
    from app.handlers.image_edit import EDIT_PRESETS, reload_custom_presets

    if key not in EDIT_PRESETS:
        return RedirectResponse("/admin/gallery?msg=Пресет+не+найден", status_code=302)
    async with AsyncSessionFactory() as session:
        await set_launch(session, key, datetime.now(UTC))
    await reload_custom_presets()  # пересчитает _NEW_SECTION/_BADGE_KEYS
    log_action_bg(
        "token", "gallery.mark_new", ip=request.client.host if request.client else None, key=key
    )
    return RedirectResponse("/admin/gallery?msg=Пресет+снова+в+Новинках", status_code=302)


@router.post("/gallery/{key}/create-override")
async def gallery_create_override(
    key: str,
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    """Создаёт custom-пресет с тем же key, что и хардкод — он перетирает
    хардкод-промпт через reload (см. snapshot-логику в image_edit.py).
    Все поля предзаполняются текущими значениями хардкода. Редиректит на
    форму edit, где можно сразу поправить и сохранить. Удалить override =
    хардкод чисто вернётся обратно."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.models import CustomPreset
    from app.database.repo.custom_presets import get_by_key
    from app.handlers.image_edit import (
        _FEMALE_ONLY_PRESETS,
        _MALE_PRESET_OVERRIDES,
        EDIT_PRESETS,
        PRESET_PAGES,
        PRESET_QUALITY_MAP,
        reload_custom_presets,
    )

    if key not in EDIT_PRESETS:
        return RedirectResponse("/admin/gallery?msg=Пресет+не+найден", status_code=302)
    async with AsyncSessionFactory() as session:
        if await get_by_key(session, key):
            existing = await get_by_key(session, key)
            return RedirectResponse(
                f"/admin/presets/{existing.id}/edit?msg=Override+уже+есть+—+редактируй+тут",
                status_code=302,
            )
        label, prompt = EDIT_PRESETS[key]
        male_prompt = _MALE_PRESET_OVERRIDES.get(key)
        category = next((p["title"] for p in PRESET_PAGES if key in p["items"]), None)
        preset = CustomPreset(
            key=key,
            label=label,
            prompt=prompt,
            male_prompt=male_prompt,
            quality_mode=PRESET_QUALITY_MAP.get(key, "quality"),
            category=category,
            female_only=(key in _FEMALE_ONLY_PRESETS),
            featured=False,
            enabled=True,
        )
        session.add(preset)
        await session.commit()
        new_id = preset.id
    await reload_custom_presets()
    log_action_bg(
        "token",
        "preset.create_override",
        ip=request.client.host if request.client else None,
        key=key,
    )
    return RedirectResponse(
        f"/admin/presets/{new_id}/edit?msg=Override+создан+—+правь+и+сохраняй", status_code=302
    )


@router.get("/gallery/{key}/inspect", response_class=HTMLResponse)
async def gallery_inspect(
    key: str,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
):
    """Read-only детали пресета — РЕАЛЬНЫЙ актуальный промпт (основной + мужской
    override, который сейчас идёт в fal). Для custom можно сразу перейти в edit;
    для хардкод — промпт правится только в коде (но видно что именно)."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import get_by_key
    from app.database.repo.preset_previews import get_preview_file_id
    from app.handlers.image_edit import (
        _FEMALE_ONLY_PRESETS,
        _MALE_PRESET_OVERRIDES,
        _MALE_STYLE_SUFFIX,
        EDIT_PRESETS,
        PRESET_PAGES,
        PRESET_QUALITY_MAP,
    )

    if key not in EDIT_PRESETS:
        return HTMLResponse("Пресет не найден", status_code=404)
    label, prompt = EDIT_PRESETS[key]
    quality_mode = PRESET_QUALITY_MAP.get(key, "quality")
    female_only = key in _FEMALE_ONLY_PRESETS
    # Мужской промпт: либо явный override, либо основной + male-suffix.
    male_override = _MALE_PRESET_OVERRIDES.get(key)
    if female_only:
        male_prompt = None  # для жен-only пресетов мужского варианта нет
    elif male_override:
        male_prompt = male_override
    else:
        male_prompt = prompt + _MALE_STYLE_SUFFIX
    category = next((p["title"] for p in PRESET_PAGES if key in p["items"]), "🆕 Новинки")

    async with AsyncSessionFactory() as session:
        custom = await get_by_key(session, key)
        has_male = bool(await get_preview_file_id(session, key, position=1))
        has_female = bool(await get_preview_file_id(session, key, position=0))

    return render(
        "gallery_inspect.html",
        title=f"Пресет: {label}",
        active="gallery",
        msg=msg,
        key=key,
        label=label,
        prompt=prompt,
        male_prompt=male_prompt,
        male_override=male_override,
        quality_mode=quality_mode,
        female_only=female_only,
        category=category,
        custom=custom,
        has_male=has_male,
        has_female=has_female,
        male_suffix=_MALE_STYLE_SUFFIX,
    )


@router.get("/gallery/thumb/{position}/{key}")
async def gallery_thumb(position: int, key: str, admin_session: str | None = Cookie(default=None)):
    """Прокси превью по KEY+position (для галереи — там работаем с key, а не id)."""
    if not _check_auth(admin_session):
        return Response(status_code=403)
    from app.database.repo.preset_previews import get_preview_file_id

    async with AsyncSessionFactory() as session:
        file_id = await get_preview_file_id(session, key, position=position)
    if not file_id:
        return Response(status_code=404)
    try:
        from app.bot import get_bot

        bot = get_bot()
        if not bot:
            return Response(status_code=503)
        tg_file = await bot.get_file(file_id)
        body = await bot.download_file(tg_file.file_path)
        return Response(
            content=body.read(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=1800"},
        )
    except Exception:
        return Response(status_code=500)


# ── PROMPT SANDBOX ────────────────────────────────────────────────────────────
# Прогон произвольного промпта через fal на base-лице — для итерации промптов
# ДО сохранения как пресет. Стоит $0.12, требует подтверждения в браузере.
@router.get("/sandbox", response_class=HTMLResponse)
async def sandbox_page(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    return render("sandbox.html", title="Песочница промптов", active="sandbox", msg=msg)


async def _do_sandbox_run(prompt: str, gender: str) -> None:

    from aiogram.types import BufferedInputFile

    from app.bot import get_bot
    from app.handlers.image_edit import _MALE_STYLE_SUFFIX
    from app.services import fal_image_service as F

    bot = get_bot()
    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    if not bot or not admin:
        return
    base = "/app/data/base_face_male.jpg" if gender == "male" else "/app/data/base_face.jpg"
    if not os.path.exists(base):
        await bot.send_message(admin, f"❌ Песочница: нет base-лица ({base})")
        return
    p = prompt + _MALE_STYLE_SUFFIX if gender == "male" else prompt
    try:
        with open(base, "rb") as fh:
            src = fh.read()
        res = await F.edit_image(src, p)
        if not res:
            await bot.send_message(admin, "⚠ Песочница: fal вернул пусто.")
            return
        await bot.send_photo(
            admin,
            BufferedInputFile(res, "sandbox.png"),
            caption=f"🧪 <b>Песочница</b> · {'муж' if gender == 'male' else 'жен'}\n\n"
            f"<i>{prompt[:200]}{'…' if len(prompt) > 200 else ''}</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await bot.send_message(admin, f"⚠ Песочница: ошибка — {str(exc)[:150]}")


@router.post("/sandbox/run")
async def sandbox_run(
    request: Request,
    prompt: str = Form(...),
    gender: str = Form("female"),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    if not prompt.strip():
        return RedirectResponse("/admin/sandbox?msg=Пустой+промпт", status_code=302)
    if gender not in ("male", "female"):
        gender = "female"
    asyncio.create_task(_do_sandbox_run(prompt.strip(), gender))
    log_action_bg(
        "token",
        "sandbox.run",
        ip=request.client.host if request.client else None,
        gender=gender,
        prompt_len=len(prompt),
    )
    return RedirectResponse(
        "/admin/sandbox?msg=Запустил+(fal+~$0.12)+—+результат+придёт+в+Telegram", status_code=302
    )


# ── PRESETS (DB-backed, мёржатся в рантайм-каталог без правки кода) ──────────────
_BASE_FACE_FEMALE = "/app/data/base_face.jpg"
_BASE_FACE_MALE = "/app/data/base_face_male.jpg"


def _preset_categories() -> list[str]:
    from app.handlers.image_edit import PRESET_PAGES

    return [p["title"] for p in PRESET_PAGES]


@router.get("/presets", response_class=HTMLResponse)
async def presets_list(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import list_custom_presets
    from app.database.repo.preset_previews import get_preview_file_id

    cutoff_30d = datetime.now(UTC) - timedelta(days=30)
    async with AsyncSessionFactory() as session:
        presets = await list_custom_presets(session)
        from app.database.repo.events import get_excluded_user_ids

        exclude_uids = await get_excluded_user_ids(session)
        # Один запрос: счётчик image_edited за 30д + последнее использование,
        # сгруппировано по preset_key (preset_key пишется в BotEvent.extra c 22.05).
        # Исключаем тех-юзеров (админ + @grelok), иначе тестер забивает топ.
        preset_key_expr = BotEvent.extra.op("->>")("preset_key")
        stat_rows = await session.execute(
            select(
                preset_key_expr.label("k"),
                func.count().label("cnt"),
                func.max(BotEvent.created_at).label("last"),
            )
            .where(
                BotEvent.event_type == "image_edited",
                BotEvent.created_at >= cutoff_30d,
                preset_key_expr.isnot(None),
                BotEvent.user_id.notin_(exclude_uids),
            )
            .group_by(preset_key_expr)
        )
        stats_map = {r.k: {"cnt": r.cnt, "last": r.last} for r in stat_rows.all()}
        rows = []
        for p in presets:
            f0 = await get_preview_file_id(session, p.key, position=0)
            f1 = await get_preview_file_id(session, p.key, position=1)
            s = stats_map.get(p.key, {"cnt": 0, "last": None})
            rows.append(
                {
                    "p": p,
                    "has_female": bool(f0),
                    "has_male": bool(f1),
                    "gen_count_30d": s["cnt"],
                    "last_used": s["last"],
                }
            )
        total_users = (
            await session.scalar(select(func.count()).select_from(User).where(not User.is_banned))
            or 0
        )
    return render(
        "presets/list.html",
        title="Пресеты",
        active="presets",
        msg=msg,
        rows=rows,
        total_users=total_users,
    )


@router.get("/presets/new", response_class=HTMLResponse)
async def presets_new(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    return render(
        "presets/form.html",
        title="Новый пресет",
        active="presets",
        msg=msg,
        preset=None,
        categories=_preset_categories(),
    )


@router.get("/presets/{preset_id}/edit", response_class=HTMLResponse)
async def presets_edit(
    preset_id: int, admin_session: str | None = Cookie(default=None), msg: str = ""
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import get_custom_preset
    from app.handlers.image_edit import _HARDCODED_SNAPSHOT, _snapshot_hardcoded_once

    async with AsyncSessionFactory() as session:
        preset = await get_custom_preset(session, preset_id)
    if not preset:
        return HTMLResponse("Не найдено", status_code=404)
    _snapshot_hardcoded_once()
    is_override = bool(_HARDCODED_SNAPSHOT and preset.key in _HARDCODED_SNAPSHOT["edit_presets"])
    return render(
        "presets/form.html",
        title=f"Пресет: {preset.label}",
        active="presets",
        msg=msg,
        preset=preset,
        categories=_preset_categories(),
        is_override=is_override,
    )


@router.post("/presets/create")
async def presets_create(
    request: Request,
    key: str = Form(...),
    label: str = Form(...),
    prompt: str = Form(...),
    male_prompt: str = Form(""),
    quality_mode: str = Form("quality"),
    category: str = Form(""),
    female_only: str = Form(""),
    featured: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import get_by_key
    from app.handlers.image_edit import EDIT_PRESETS, reload_custom_presets

    norm_key = key.strip().lower().replace(" ", "_")
    if not norm_key or not label.strip() or not prompt.strip():
        return RedirectResponse(
            "/admin/presets/new?msg=Заполни+ключ,+название+и+промпт", status_code=302
        )
    async with AsyncSessionFactory() as session:
        if norm_key in EDIT_PRESETS or await get_by_key(session, norm_key):
            return RedirectResponse("/admin/presets/new?msg=Ключ+уже+занят", status_code=302)
        from app.database.models import CustomPreset

        preset = CustomPreset(
            key=norm_key,
            label=label.strip(),
            prompt=prompt.strip(),
            male_prompt=male_prompt.strip() or None,
            quality_mode=quality_mode,
            category=category.strip() or None,
            female_only=bool(female_only),
            featured=bool(featured),
            enabled=True,
        )
        session.add(preset)
        await session.commit()
    await reload_custom_presets()
    log_action_bg(
        "token", "preset.create", ip=request.client.host if request.client else None, key=norm_key
    )
    return RedirectResponse("/admin/presets?msg=Пресет+создан+—+сгенерь+превью", status_code=302)


@router.post("/presets/{preset_id}/toggle-featured")
async def presets_toggle_featured(
    preset_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import toggle_featured
    from app.handlers.image_edit import reload_custom_presets

    async with AsyncSessionFactory() as session:
        new_state = await toggle_featured(session, preset_id)
        await session.commit()
    await reload_custom_presets()
    log_action_bg(
        "token",
        "preset.toggle_featured",
        ip=request.client.host if request.client else None,
        preset_id=preset_id,
        featured=new_state,
    )
    return RedirectResponse("/admin/presets?msg=Featured+обновлён", status_code=302)


@router.post("/presets/import")
async def presets_import(
    request: Request,
    payload: str = Form(...),
    admin_session: str | None = Cookie(default=None),
):
    """Массовый импорт пресетов из JSON. Формат — список объектов:
    [{"key":"x","label":"...","prompt":"...","male_prompt":"...","category":"...","quality_mode":"quality","female_only":false,"featured":false}, ...]"""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.models import CustomPreset
    from app.database.repo.custom_presets import get_by_key
    from app.handlers.image_edit import EDIT_PRESETS, reload_custom_presets

    try:
        items = json.loads(payload)
        if not isinstance(items, list):
            raise ValueError("должен быть массив")
    except Exception as exc:
        return RedirectResponse(
            f"/admin/presets/new?msg=Некорректный+JSON:+{str(exc)[:80]}", status_code=302
        )
    created = skipped = 0
    seen_in_payload: set[str] = set()  # отлов дубликатов ВНУТРИ массива (раньше падал unique)
    async with AsyncSessionFactory() as session:
        for it in items:
            if not isinstance(it, dict):
                skipped += 1
                continue
            key = (it.get("key") or "").strip().lower().replace(" ", "_")
            label = (it.get("label") or "").strip()
            prompt = (it.get("prompt") or "").strip()
            if not key or not label or not prompt:
                skipped += 1
                continue
            if key in seen_in_payload:
                skipped += 1
                continue
            if key in EDIT_PRESETS or await get_by_key(session, key):
                skipped += 1
                continue
            session.add(
                CustomPreset(
                    key=key,
                    label=label,
                    prompt=prompt,
                    male_prompt=(it.get("male_prompt") or "").strip() or None,
                    quality_mode=it.get("quality_mode", "quality"),
                    category=(it.get("category") or "").strip() or None,
                    female_only=bool(it.get("female_only")),
                    featured=bool(it.get("featured")),
                    enabled=True,
                )
            )
            seen_in_payload.add(key)
            created += 1
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.warning("preset.import commit failed", error=str(exc)[:200])
            from urllib.parse import quote

            return RedirectResponse(
                f"/admin/presets/new?msg={quote(f'Ошибка commit: {str(exc)[:80]}')}",
                status_code=302,
            )
    await reload_custom_presets()
    log_action_bg(
        "token",
        "preset.import",
        ip=request.client.host if request.client else None,
        created=created,
        skipped=skipped,
    )
    from urllib.parse import quote

    return RedirectResponse(
        f"/admin/presets?msg={quote(f'Импорт: создано {created}, пропущено {skipped}')}",
        status_code=302,
    )


@router.post("/presets/{preset_id}/update")
async def presets_update(
    preset_id: int,
    request: Request,
    label: str = Form(...),
    prompt: str = Form(...),
    male_prompt: str = Form(""),
    quality_mode: str = Form("quality"),
    category: str = Form(""),
    female_only: str = Form(""),
    featured: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import update_custom_preset
    from app.handlers.image_edit import reload_custom_presets

    async with AsyncSessionFactory() as session:
        ok = await update_custom_preset(
            session,
            preset_id,
            label=label.strip(),
            prompt=prompt.strip(),
            male_prompt=male_prompt.strip() or None,
            quality_mode=quality_mode,
            category=category.strip() or None,
            female_only=bool(female_only),
            featured=bool(featured),
        )
        await session.commit()
    if not ok:
        return HTMLResponse("Не найдено", status_code=404)
    await reload_custom_presets()
    log_action_bg(
        "token",
        "preset.update",
        ip=request.client.host if request.client else None,
        preset_id=preset_id,
    )
    return RedirectResponse("/admin/presets?msg=Сохранено", status_code=302)


@router.post("/presets/{preset_id}/toggle")
async def presets_toggle(
    preset_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import toggle_custom_preset
    from app.handlers.image_edit import reload_custom_presets

    async with AsyncSessionFactory() as session:
        new_state = await toggle_custom_preset(session, preset_id)
        await session.commit()
    await reload_custom_presets()
    log_action_bg(
        "token",
        "preset.toggle",
        ip=request.client.host if request.client else None,
        preset_id=preset_id,
        enabled=new_state,
    )
    return RedirectResponse("/admin/presets?msg=Статус+обновлён", status_code=302)


@router.post("/presets/{preset_id}/delete")
async def presets_delete(
    preset_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import delete_custom_preset
    from app.handlers.image_edit import reload_custom_presets

    async with AsyncSessionFactory() as session:
        key = await delete_custom_preset(session, preset_id)
        await session.commit()
    await reload_custom_presets()
    log_action_bg(
        "token",
        "preset.delete",
        ip=request.client.host if request.client else None,
        preset_id=preset_id,
        key=key,
    )
    return RedirectResponse("/admin/presets?msg=Удалено", status_code=302)


@router.get("/presets/{preset_id}/thumb/{position}")
async def presets_thumb(
    preset_id: int, position: int, admin_session: str | None = Cookie(default=None)
):
    """Прокси превью пресета (по key+position) — чтобы видеть картинку в дашборде."""
    if not _check_auth(admin_session):
        return Response(status_code=403)
    from app.database.repo.custom_presets import get_custom_preset
    from app.database.repo.preset_previews import get_preview_file_id

    async with AsyncSessionFactory() as session:
        preset = await get_custom_preset(session, preset_id)
        if not preset:
            return Response(status_code=404)
        file_id = await get_preview_file_id(session, preset.key, position=position)
    if not file_id:
        return Response(status_code=404)
    try:
        from app.bot import get_bot

        bot = get_bot()
        if not bot:
            return Response(status_code=503)
        tg_file = await bot.get_file(file_id)
        body = await bot.download_file(tg_file.file_path)
        return Response(
            content=body.read(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=600"},
        )
    except Exception as exc:
        logger.warning("preset thumb failed", preset_id=preset_id, error=str(exc)[:200])
        return Response(status_code=500)


async def _do_generate_preview_by_key(
    key: str,
    label: str,
    prompt: str,
    male_prompt: str | None,
    female_only: bool,
    positions: set[int] | None = None,
) -> None:
    """Генерит 2K-превью по ключу (работает и для хардкод, и для custom-пресетов).
    positions ограничивает, что генерить: {0} жен / {1} муж / None обе.
    ~$0.12 за каждое (fal 2K). Шлёт админу отчёт."""

    from aiogram.types import BufferedInputFile

    from app.bot import get_bot
    from app.database.repo.preset_previews import save_preview
    from app.handlers.image_edit import _MALE_STYLE_SUFFIX
    from app.services import fal_image_service as F

    bot = get_bot()
    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    if not bot or not admin:
        return
    want = positions or ({0} if female_only else {0, 1})
    jobs = []
    if 0 in want and os.path.exists(_BASE_FACE_FEMALE):
        jobs.append((0, _BASE_FACE_FEMALE, prompt))
    if 1 in want and not female_only and os.path.exists(_BASE_FACE_MALE):
        jobs.append((1, _BASE_FACE_MALE, male_prompt or (prompt + _MALE_STYLE_SUFFIX)))
    if not jobs:
        await bot.send_message(admin, f"❌ Нет base-лиц для генерации превью «{label}».")
        return

    done = []
    for pos, base, p in jobs:
        try:
            with open(base, "rb") as fh:
                src = fh.read()
            res = await F.edit_image(src, p)
            if not res:
                await bot.send_message(admin, f"⚠ Превью «{label}» (pos={pos}) не сгенерилось.")
                continue
            sent = await bot.send_photo(
                admin,
                BufferedInputFile(res, f"{key}_{pos}.png"),
                caption=f"🎨 Превью «{label}» · {'муж' if pos == 1 else 'жен'}",
            )
            fid = sent.photo[-1].file_id
            async with AsyncSessionFactory() as session:
                await save_preview(session, key, fid, position=pos, source_file=f"dash:{key}")
                await session.commit()
            # Если регенерим один из 3 превью, попавших в welcome-коллаж,
            # сбрасываем кэш — иначе /start будет показывать старую версию.
            try:
                from app.services.welcome_collage import (
                    invalidate_welcome_collage,
                    is_collage_preset,
                )

                if is_collage_preset(key, pos):
                    await invalidate_welcome_collage()
            except Exception:
                pass
            done.append("муж" if pos == 1 else "жен")
        except Exception as exc:
            logger.warning("preview gen failed", key=key, pos=pos, error=str(exc)[:200])
            await bot.send_message(
                admin, f"⚠ Ошибка генерации «{label}» (pos={pos}): {str(exc)[:120]}"
            )
    if done:
        await bot.send_message(
            admin,
            f"✅ Превью «{label}» готовы: {', '.join(done)}. "
            f"Открой галерею или раздел «Пресеты» — можно рассылать.",
        )


async def _do_generate_preview(preset_id: int) -> None:
    """Обёртка над _do_generate_preview_by_key — фетчит CustomPreset → вызывает общую."""
    from app.database.repo.custom_presets import get_custom_preset

    async with AsyncSessionFactory() as session:
        preset = await get_custom_preset(session, preset_id)
        if not preset:
            return
        args = (preset.key, preset.label, preset.prompt, preset.male_prompt, preset.female_only)
    await _do_generate_preview_by_key(*args)


@router.post("/presets/{preset_id}/generate")
async def presets_generate(
    preset_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    asyncio.create_task(_do_generate_preview(preset_id))
    log_action_bg(
        "token",
        "preset.generate",
        ip=request.client.host if request.client else None,
        preset_id=preset_id,
    )
    return RedirectResponse(
        "/admin/presets?msg=Генерю+превью+(fal+~$0.12+за+каждое)+—+пришлю+в+Telegram",
        status_code=302,
    )


async def _do_broadcast_by_key(
    key: str,
    label: str,
    audience: str,
    caption_override: str,
) -> None:
    """Рассылает по preset_key (работает и для хардкод, и для custom). Коллаж
    муж+жен (или одиночное фото) + подпись + tracked-кнопка bc_open:<id>.
    Broadcast создаётся СРАЗУ для получения id."""
    from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

    from app.bot import get_bot
    from app.database.repo.preset_previews import get_preview_file_id
    from app.handlers.admin import PE_SPARKLES, _build_showcase_collage
    from app.handlers.image_edit import NO_BROADCAST_PRESETS

    # Defense-in-depth: даже если кто-то напрямую дёрнул _do_broadcast_by_key
    # (минуя админ-форму) — провокационные пресеты не уходят в рассылку.
    if key in NO_BROADCAST_PRESETS:
        logger.warning("broadcast blocked: preset in NO_BROADCAST_PRESETS", key=key)
        return

    bot = get_bot()
    if not bot:
        return
    async with AsyncSessionFactory() as session:
        male_fid = await get_preview_file_id(session, key, position=1)
        female_fid = await get_preview_file_id(session, key, position=0)

    caption = caption_override.strip() or (
        f"🔥 <b>Новая сцена в Нейре: {label}</b>\n\n"
        "Лицо в кадре — твоё. И для парней, и для девушек.\n\n"
        "<b>Как сделать:</b>\n1️⃣ Жми кнопку ниже\n"
        "2️⃣ Укажи, для кого фото\n3️⃣ Пришли селфи и выбери сцену 👇"
    )

    # 1. Создаём Broadcast СРАЗУ — нужен id для tracked callback `bc_open:<id>`.
    async with AsyncSessionFactory() as session:
        bc = Broadcast(
            source="neyra",
            promo_code=f"PRESET_{key}",
            total_recipients=0,
            sent_count=0,
            blocked_count=0,
            error_count=0,
            click_count=0,
            status="running",
        )
        session.add(bc)
        await session.commit()
        bc_id = bc.id

    tracked_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text="Сделать фото с моим лицом",
                    callback_data=f"bc_open:{bc_id}",
                    icon_custom_emoji_id=PE_SPARKLES,
                    style="success",
                ),
            ]
        ]
    )

    admin = settings.admin_ids_list[0] if settings.admin_ids_list else None
    try:
        if male_fid and female_fid:
            collage = await _build_showcase_collage(bot, male_fid, female_fid)
            holder = await bot.send_photo(
                admin,
                BufferedInputFile(collage, "preset.jpg"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=tracked_kb,
            )
            photo_ref = holder.photo[-1].file_id
        else:
            photo_ref = male_fid or female_fid
            if not photo_ref:
                if admin:
                    await bot.send_message(admin, f"❌ У «{label}» нет превью — сначала сгенерь.")
                async with AsyncSessionFactory() as session:
                    bc = await session.get(Broadcast, bc_id)
                    if bc:
                        bc.status = "failed"
                        await session.commit()
                return
    except Exception as exc:
        logger.warning("preset broadcast prep failed", key=key, error=str(exc)[:200])
        # 24.05: помечаем Broadcast как failed, иначе он висит в 'running'
        # навсегда и портит метрики/dashboards.
        try:
            async with AsyncSessionFactory() as session:
                bc = await session.get(Broadcast, bc_id)
                if bc and bc.status == "running":
                    bc.status = "failed"
                    await session.commit()
        except Exception:
            pass
        return

    async with AsyncSessionFactory() as session:
        if audience == "admin_only":
            tg_ids = list(settings.admin_ids_list or [])
        elif audience == "test_user":
            tg_ids = await _resolve_test_user_tg_id(session)
        else:
            q = select(User.telegram_id).where(not User.is_banned)
            if audience == "free":
                subq = select(Subscription.user_id).where(Subscription.is_active)
                q = q.where(~User.id.in_(subq))
            elif audience == "paid":
                subq = select(Subscription.user_id).where(Subscription.is_active)
                q = q.where(User.id.in_(subq))
            elif audience == "zero_gen":
                # «Тёплая база с балансом, но БЕЗ единой генерации» — у нас 80%
                # такой когорты. Цель: re-engagement push для активации первой
                # сцены. Исключаем тех-юзеров и забаненных.
                from app.database.repo.events import get_excluded_user_ids

                exclude_uids = await get_excluded_user_ids(session)
                has_edit_subq = (
                    select(BotEvent.user_id).where(BotEvent.event_type == "image_edited").distinct()
                )
                q = q.where(~User.id.in_(has_edit_subq))
                if exclude_uids:
                    q = q.where(User.id.notin_(exclude_uids))
            res = await session.execute(q)
            tg_ids = [row[0] for row in res.all()]

    sent = blocked = errors = 0
    for tg_id in tg_ids:
        if admin and tg_id == admin and audience not in ("admin_only", "test_user"):
            sent += 1
            continue
        try:
            await bot.send_photo(
                tg_id, photo_ref, caption=caption, parse_mode="HTML", reply_markup=tracked_kb
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as exc:
            err = str(exc).lower()
            if "forbidden" in err or "blocked" in err or "deactivated" in err:
                blocked += 1
            else:
                errors += 1
    async with AsyncSessionFactory() as session:
        bc = await session.get(Broadcast, bc_id)
        if bc:
            bc.total_recipients = len(tg_ids)
            bc.sent_count = sent
            bc.blocked_count = blocked
            bc.error_count = errors
            bc.status = "completed"
            bc.completed_at = datetime.now(UTC)
            await session.commit()
    logger.info(
        "preset broadcast done",
        key=key,
        audience=audience,
        bc_id=bc_id,
        sent=sent,
        blocked=blocked,
        errors=errors,
    )


async def _do_preset_broadcast(preset_id: int, audience: str, caption_override: str) -> None:
    """Обёртка: фетчит CustomPreset по id → вызывает _do_broadcast_by_key.
    Сохранена для совместимости с очередью scheduled_broadcasts и существующих POST."""
    from app.database.repo.custom_presets import get_custom_preset

    async with AsyncSessionFactory() as session:
        preset = await get_custom_preset(session, preset_id)
        if not preset:
            return
        key, label = preset.key, preset.label
    await _do_broadcast_by_key(key, label, audience, caption_override)


@router.get("/presets/{preset_id}/broadcast", response_class=HTMLResponse)
async def presets_broadcast_form(
    preset_id: int,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
):
    """Страница рассылки пресета — позволяет отредактировать подпись и выбрать
    аудиторию ДО запуска. Превью муж+жен показываем сразу."""
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.custom_presets import get_custom_preset
    from app.database.repo.preset_previews import get_preview_file_id

    async with AsyncSessionFactory() as session:
        preset = await get_custom_preset(session, preset_id)
        if not preset:
            return HTMLResponse("Не найдено", status_code=404)
        has_male = bool(await get_preview_file_id(session, preset.key, position=1))
        has_female = bool(await get_preview_file_id(session, preset.key, position=0))
        total_all = (
            await session.scalar(select(func.count()).select_from(User).where(not User.is_banned))
            or 0
        )
    # Дефолтный caption — тот же, что отдаёт _do_preset_broadcast при пустом.
    default_caption = (
        f"🔥 <b>Новая сцена в Нейре: {preset.label}</b>\n\n"
        "Лицо в кадре — твоё. И для парней, и для девушек.\n\n"
        "<b>Как сделать:</b>\n1️⃣ Жми кнопку ниже\n"
        "2️⃣ Укажи, для кого фото\n3️⃣ Пришли селфи и выбери сцену 👇"
    )
    return render(
        "presets/broadcast.html",
        title=f"Рассылка: {preset.label}",
        active="presets",
        msg=msg,
        preset=preset,
        has_male=has_male,
        has_female=has_female,
        default_caption=default_caption,
        total_all=total_all,
    )


@router.post("/presets/{preset_id}/broadcast")
async def presets_broadcast(
    preset_id: int,
    request: Request,
    audience: str = Form("all"),
    caption: str = Form(""),
    send_at: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    # send_at от <input type=datetime-local> в МСК (часовой пояс админа).
    # Пустой → шлём сразу. Иначе — кладём в scheduled_broadcasts (диспатчер
    # каждую минуту в handlers/admin.py подберёт и запустит).
    if send_at.strip():
        try:
            local_naive = datetime.fromisoformat(send_at.strip())
            msk = timezone(timedelta(hours=3))
            send_at_utc = local_naive.replace(tzinfo=msk).astimezone(UTC)
        except Exception:
            return RedirectResponse(
                f"/admin/presets/{preset_id}/broadcast?msg=Некорректное+время", status_code=302
            )
        if send_at_utc <= datetime.now(UTC):
            return RedirectResponse(
                f"/admin/presets/{preset_id}/broadcast?msg=Время+должно+быть+в+будущем",
                status_code=302,
            )
        from app.database.models import ScheduledBroadcast

        async with AsyncSessionFactory() as session:
            sb = ScheduledBroadcast(
                preset_id=preset_id,
                audience=audience,
                caption=caption,
                send_at=send_at_utc,
                status="pending",
            )
            session.add(sb)
            await session.commit()
        log_action_bg(
            "token",
            "preset.broadcast.scheduled",
            ip=request.client.host if request.client else None,
            preset_id=preset_id,
            audience=audience,
            send_at=send_at,
        )
        return RedirectResponse("/admin/scheduled?msg=Рассылка+запланирована", status_code=302)
    asyncio.create_task(_do_preset_broadcast(preset_id, audience, caption))
    log_action_bg(
        "token",
        "preset.broadcast",
        ip=request.client.host if request.client else None,
        preset_id=preset_id,
        audience=audience,
        caption_len=len(caption),
    )
    return RedirectResponse("/admin/presets?msg=Рассылка+пресета+запущена", status_code=302)


@router.get("/scheduled", response_class=HTMLResponse)
async def scheduled_list(admin_session: str | None = Cookie(default=None), msg: str = ""):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.models import CustomPreset, ScheduledBroadcast

    async with AsyncSessionFactory() as session:
        rows = (
            await session.execute(
                select(ScheduledBroadcast, CustomPreset.label, CustomPreset.key)
                .join(CustomPreset, CustomPreset.id == ScheduledBroadcast.preset_id)
                .order_by(ScheduledBroadcast.send_at.desc())
                .limit(100)
            )
        ).all()
        items = [{"sb": r[0], "label": r[1], "key": r[2]} for r in rows]
    return render(
        "scheduled.html", title="Отложенные рассылки", active="scheduled", msg=msg, items=items
    )


@router.post("/scheduled/{sb_id}/cancel")
async def scheduled_cancel(
    sb_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.models import ScheduledBroadcast

    async with AsyncSessionFactory() as session:
        sb = await session.get(ScheduledBroadcast, sb_id)
        if sb and sb.status == "pending":
            sb.status = "cancelled"
            await session.commit()
    log_action_bg(
        "token",
        "preset.broadcast.cancel",
        ip=request.client.host if request.client else None,
        sb_id=sb_id,
    )
    return RedirectResponse("/admin/scheduled?msg=Отменено", status_code=302)


# ── BROADCASTS HISTORY ──────────────────────────────────────────────────────────


@router.get("/broadcasts", response_class=HTMLResponse)
async def broadcasts_list(
    admin_session: str | None = Cookie(default=None),
    source: str = "",
    page: int = 1,
):
    if not _check_auth(admin_session):
        return _redirect_login()

    per_page = 15
    offset = (page - 1) * per_page

    async with AsyncSessionFactory() as session:
        q = select(Broadcast).order_by(Broadcast.started_at.desc())
        if source:
            q = q.where(Broadcast.source == source)
        total = await session.scalar(select(func.count()).select_from(q.subquery())) or 0
        broadcasts = (await session.execute(q.offset(offset).limit(per_page))).scalars().all()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render(
        "broadcasts/list.html",
        title="Рассылки",
        active="broadcasts",
        broadcasts=broadcasts,
        total=total,
        page=page,
        total_pages=total_pages,
        source=source,
    )


@router.get("/broadcasts/{broadcast_id}", response_class=HTMLResponse)
async def broadcast_detail(
    broadcast_id: int,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        broadcast = await get_broadcast_by_id(session, broadcast_id)
        if not broadcast:
            return HTMLResponse("Рассылка не найдена", status_code=404)
    return render(
        "broadcasts/detail.html",
        title=f"Рассылка #{broadcast.id}",
        active="broadcasts",
        msg=msg,
        broadcast=broadcast,
    )


# ── ANALYTICS (legacy redirect → merged into /promos) ───────────────────────────


@router.get("/analytics")
async def analytics_legacy_redirect(promo: str = ""):
    target = f"/admin/promos?promo={promo}" if promo else "/admin/promos"
    return RedirectResponse(target, status_code=301)


@router.get("/api/promos/{promo_code}/conversion")
async def promo_conversion_api(promo_code: str, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return Response(status_code=403)
    async with AsyncSessionFactory() as session:
        return await get_promo_conversion_funnel(session, promo_code.upper())


# ── FINANCE ─────────────────────────────────────────────────────────────────────


@router.get("/finance", response_class=HTMLResponse)
async def finance_page(admin_session: str | None = Cookie(default=None), days: int = 30):
    if not _check_auth(admin_session):
        return _redirect_login()
    days = max(7, min(days, 365))

    async with AsyncSessionFactory() as session:
        summary = await get_revenue_summary(session)
        daily = await get_daily_revenue(session, days=days)
        by_method = await get_revenue_by_method(session, days=days)
        spenders = await get_top_spenders(session, limit=20)
        status_breakdown = await get_payment_status_breakdown(session, days=days)
        funnel = await get_conversion_metrics(session)
        pay_funnel = await get_payment_funnel(session, days=days)

    daily_labels = [d["day"] for d in daily]
    daily_amounts = [d["amount"] for d in daily]
    total_methods = sum(d["amount"] for d in by_method) or 1

    return render(
        "finance.html",
        title="Финансы",
        active="finance",
        summary=summary,
        days=days,
        by_method=by_method,
        total_methods=total_methods,
        spenders=spenders,
        status_breakdown=status_breakdown,
        funnel=funnel,
        pay_funnel=pay_funnel,
        daily_labels=daily_labels,
        daily_amounts=daily_amounts,
    )


# ── PAYMENTS ─────────────────────────────────────────────────────────────────────


@router.get("/payments", response_class=HTMLResponse)
async def payments_page(
    admin_session: str | None = Cookie(default=None),
    status: str = "",
    method: str = "",
    user_id: str = "",
    page: int = 1,
    msg: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()

    per_page = 30
    offset = (page - 1) * per_page

    async with AsyncSessionFactory() as session:
        q = select(Payment).order_by(Payment.created_at.desc())
        filters = []
        if status:
            try:
                filters.append(Payment.status == PaymentStatus(status))
            except ValueError:
                pass
        if method:
            filters.append(Payment.payment_method == method)
        if user_id.strip():
            try:
                tg_id = int(user_id.strip())
                u = await session.scalar(select(User).where(User.telegram_id == tg_id))
                if u:
                    filters.append(Payment.user_id == u.id)
                else:
                    filters.append(Payment.id == -1)
            except ValueError:
                pass
        if filters:
            q = q.where(and_(*filters))

        total = await session.scalar(select(func.count()).select_from(q.subquery())) or 0
        payments = (await session.execute(q.offset(offset).limit(per_page))).scalars().all()

        user_ids = list({p.user_id for p in payments})
        users_map: dict[int, User] = {}
        if user_ids:
            urows = await session.execute(select(User).where(User.id.in_(user_ids)))
            users_map = {u.id: u for u in urows.scalars().all()}

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render(
        "payments.html",
        title="Платежи",
        active="payments",
        msg=msg,
        payments=payments,
        users_map=users_map,
        total=total,
        page=page,
        total_pages=total_pages,
        status=status,
        method=method,
        user_id=user_id,
    )


@router.post("/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        p = await session.scalar(select(Payment).where(Payment.id == payment_id))
        if not p:
            return RedirectResponse("/admin/payments?msg=Платёж+не+найден", status_code=302)
        if p.status != PaymentStatus.SUCCEEDED:
            return RedirectResponse(
                "/admin/payments?msg=Только+succeeded+можно+вернуть", status_code=302
            )
        credits = (p.extra or {}).get("credits", 0)
        user = await session.scalar(select(User).where(User.id == p.user_id))
        if user and credits:
            user.credits = max(0, user.credits - credits)
        p.status = PaymentStatus.CANCELED
        p.updated_at = datetime.now(UTC)
        target_user_id = p.user_id
        amount = float(p.amount)
        await session.commit()
    log_action_bg(
        "token",
        "payment.refund",
        target_user_id=target_user_id,
        ip=request.client.host if request.client else None,
        payment_id=payment_id,
        amount=amount,
        credits_deducted=credits,
    )
    return RedirectResponse(
        f"/admin/payments?msg=Возврат+оформлен+(списано+{credits}+кр)", status_code=302
    )


@router.post("/payments/{payment_id}/cancel")
async def cancel_payment(
    payment_id: int, request: Request, admin_session: str | None = Cookie(default=None)
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        p = await session.scalar(select(Payment).where(Payment.id == payment_id))
        if p and p.status == PaymentStatus.PENDING:
            p.status = PaymentStatus.FAILED
            p.updated_at = datetime.now(UTC)
            target_user_id = p.user_id
            await session.commit()
            log_action_bg(
                "token",
                "payment.cancel",
                target_user_id=target_user_id,
                ip=request.client.host if request.client else None,
                payment_id=payment_id,
            )
    return RedirectResponse("/admin/payments?msg=Платёж+отменён", status_code=302)


# ── AURUM CROSS-BOT TRAFFIC ─────────────────────────────────────────────────────


@router.get("/channel-traffic", response_class=HTMLResponse)
async def channel_traffic_page(admin_session: str | None = Cookie(default=None)):
    """Дашборд трафика @conturD: разбивка джойнов по invite-ссылкам + retention.
    Источник: таблица channel_traffic, заполняется хендлером
    app/handlers/channel_traffic.py (chat_member updates)."""
    if not _check_auth(admin_session):
        return _redirect_login()

    # Атрибуция «по последнему join». Юзер зашёл по A, вышел, зашёл по B →
    # засчитываем его только B (старая агрегация по (link,user) давала double-
    # count: юзер вылезал и в строке A, и в строке B, retention раздувался).
    # still_here = из тех, чей последний join был через эту ссылку, кто сейчас
    # в канале (последнее ЛЮБОЕ событие = 'join').
    async with AsyncSessionFactory() as session:
        sql = sa_text("""
            WITH last_join AS (
                SELECT DISTINCT ON (user_tg_id)
                    user_tg_id, invite_link_url, invite_link_name, ts AS join_ts
                FROM channel_traffic
                WHERE event = 'join'
                ORDER BY user_tg_id, ts DESC
            ),
            latest_event AS (
                SELECT DISTINCT ON (user_tg_id)
                    user_tg_id, event
                FROM channel_traffic
                ORDER BY user_tg_id, ts DESC
            )
            SELECT
                COALESCE(lj.invite_link_url, 'organic') AS link_url,
                COALESCE(MAX(lj.invite_link_name), '') AS link_name,
                COUNT(*) AS joined,
                SUM(CASE WHEN le.event = 'join' THEN 1 ELSE 0 END) AS still_here,
                SUM(CASE WHEN lj.join_ts > NOW() - interval '24 hour' THEN 1 ELSE 0 END) AS joined_24h,
                SUM(CASE WHEN lj.join_ts > NOW() - interval '7 days' THEN 1 ELSE 0 END) AS joined_7d
            FROM last_join lj
            LEFT JOIN latest_event le ON le.user_tg_id = lj.user_tg_id
            GROUP BY lj.invite_link_url
            ORDER BY joined DESC
        """)
        rows = (await session.execute(sql)).mappings().all()

        totals_sql = sa_text("""
            SELECT
                COUNT(*) FILTER (WHERE event='join')  AS total_joins,
                COUNT(*) FILTER (WHERE event='leave') AS total_leaves,
                COUNT(*) FILTER (WHERE event='join'  AND ts > NOW() - interval '24 hour') AS joins_24h,
                COUNT(*) FILTER (WHERE event='leave' AND ts > NOW() - interval '24 hour') AS leaves_24h,
                MIN(ts) AS first_event_ts
            FROM channel_traffic
        """)
        totals = (await session.execute(totals_sql)).mappings().first()

        recent_sql = sa_text("""
            SELECT ts, event, user_tg_id, invite_link_url, invite_link_name, via_chat_folder
            FROM channel_traffic
            ORDER BY ts DESC
            LIMIT 30
        """)
        recent = (await session.execute(recent_sql)).mappings().all()

    # Реальное число подписчиков в TG (по getChatMemberCount). Нужно как
    # baseline — без него цифра 'tracked since deploy' выглядит как «всё
    # сломано, где остальные 169». Если бот не отвечает — отдаём None,
    # шаблон покажет прочерк.
    from app.bot import get_bot

    bot = get_bot()
    tg_total_subs = None
    if bot:
        try:
            tg_total_subs = await bot.get_chat_member_count(chat_id="@conturD")
        except Exception:
            tg_total_subs = None

    return render(
        "channel_traffic.html",
        title="Трафик канала",
        pretitle="Джойны/левы @conturD по invite-ссылкам",
        active="channel_traffic",
        sources=[dict(r) for r in rows],
        totals=dict(totals) if totals else {},
        recent=[dict(r) for r in recent],
        tg_total_subs=tg_total_subs,
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    admin_session: str | None = Cookie(default=None),
    page: int = 1,
    action: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()
    per_page = 50
    offset = (page - 1) * per_page
    async with AsyncSessionFactory() as session:
        q = (
            select(AdminAction, User.username, User.telegram_id, User.id.label("uid"))
            .outerjoin(User, User.id == AdminAction.target_user_id)
            .order_by(AdminAction.created_at.desc())
        )
        if action:
            q = q.where(AdminAction.action == action)
        count_q = select(func.count()).select_from(AdminAction)
        if action:
            count_q = count_q.where(AdminAction.action == action)
        total = await session.scalar(count_q) or 0
        items = (await session.execute(q.offset(offset).limit(per_page))).all()
        # distinct actions for filter dropdown
        actions = [
            r[0]
            for r in (
                await session.execute(
                    select(AdminAction.action).distinct().order_by(AdminAction.action)
                )
            ).all()
        ]
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render(
        "audit.html",
        title="Журнал админа",
        active="audit",
        items=items,
        total=total,
        page=page,
        total_pages=total_pages,
        actions=actions,
        action_filter=action,
    )


# ── HEALTH DASHBOARD ────────────────────────────────────────────────────────────


@router.get("/health", response_class=HTMLResponse)
async def health_page(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()

    import time as _time

    health = {
        "db_ok": False,
        "db_ms": 0,
        "db_error": None,
        "redis_ok": False,
        "redis_ms": 0,
        "redis_error": None,
        "last_msg_min": None,
        "last_msg_tier": None,
        "errors_hour": 0,
        "msgs_today": 0,
        "trial_today": 0,
        "images_today": 0,
        "edits_today": 0,
    }
    recent_errors: list[BotEvent] = []

    # DB ping
    t0 = _time.time()
    try:
        async with AsyncSessionFactory() as session:
            await session.execute(select(func.now()))
            health["db_ms"] = int((_time.time() - t0) * 1000)
            health["db_ok"] = True
            # Stats from DB
            now = datetime.now(UTC)
            hour_ago = now - timedelta(hours=1)
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            health["errors_hour"] = (
                await session.scalar(
                    select(func.count())
                    .select_from(BotEvent)
                    .where(
                        BotEvent.event_type == "error",
                        BotEvent.created_at >= hour_ago,
                    )
                )
                or 0
            )
            health["msgs_today"] = (
                await session.scalar(
                    select(func.count())
                    .select_from(BotEvent)
                    .where(
                        BotEvent.event_type == "message_sent",
                        BotEvent.created_at >= today,
                    )
                )
                or 0
            )
            health["trial_today"] = (
                await session.scalar(
                    select(func.count())
                    .select_from(BotEvent)
                    .where(
                        BotEvent.event_type == "message_sent",
                        BotEvent.model_key == "pro",
                        BotEvent.credits_spent == 0,
                        BotEvent.created_at >= today,
                    )
                )
                or 0
            )
            health["images_today"] = (
                await session.scalar(
                    select(func.count())
                    .select_from(BotEvent)
                    .where(
                        BotEvent.event_type == "image_generated",
                        BotEvent.created_at >= today,
                    )
                )
                or 0
            )
            health["edits_today"] = (
                await session.scalar(
                    select(func.count())
                    .select_from(BotEvent)
                    .where(
                        BotEvent.event_type == "image_edited",
                        BotEvent.created_at >= today,
                    )
                )
                or 0
            )
            # Последняя успешная AI-активность ЛЮБОГО типа: чат, картинки, swap, vision.
            # Раньше смотрели только message_sent — давало ложное «43ч назад», когда
            # юзеры весь день жали кнопки фото-фич, а чат был тихим.
            _AI_EVENT_TYPES = (
                "message_sent",
                "image_generated",
                "image_edited",
                "face_swapped",
                "vision_analyzed",
            )
            _AI_TYPE_RU = {
                "message_sent": "чат",
                "image_generated": "генерация",
                "image_edited": "фото-эдит",
                "face_swapped": "face-swap",
                "vision_analyzed": "анализ фото",
            }
            last_evt = await session.scalar(
                select(BotEvent)
                .where(BotEvent.event_type.in_(_AI_EVENT_TYPES))
                .order_by(BotEvent.created_at.desc())
                .limit(1)
            )
            if last_evt and last_evt.created_at:
                lm_at = last_evt.created_at
                if lm_at.tzinfo is None:
                    lm_at = lm_at.replace(tzinfo=UTC)
                delta = (now - lm_at).total_seconds() / 60
                health["last_msg_min"] = int(delta)
                health["last_msg_tier"] = _AI_TYPE_RU.get(last_evt.event_type, last_evt.event_type)
            # Recent errors
            err_rows = (
                (
                    await session.execute(
                        select(BotEvent)
                        .where(BotEvent.event_type == "error")
                        .order_by(BotEvent.created_at.desc())
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            recent_errors = err_rows
    except Exception as exc:
        health["db_error"] = str(exc)[:200]

    # Redis ping
    t0 = _time.time()
    try:
        redis = _get_redis()
        await redis.ping()
        health["redis_ms"] = int((_time.time() - t0) * 1000)
        health["redis_ok"] = True
    except Exception as exc:
        health["redis_error"] = str(exc)[:200]

    # AI model stats — только фото-модели (visual-first продукт). Текстовый
    # чат-чейн (TIER_MODELS: DeepSeek/GPT/Gemini) убран: чат вторичен и работает
    # на бесплатной DeepSeek — в health он только зашумлял список.
    try:
        from app.services.image_edit_service import (
            EDIT_MODELS_FAST,
            EDIT_MODELS_QUALITY,
            FACE_SWAP_MODELS,
        )

        all_models: list[str] = []
        seen = set()
        for src in (EDIT_MODELS_QUALITY, EDIT_MODELS_FAST, FACE_SWAP_MODELS):
            for m, _ in src:
                if m not in seen:
                    seen.add(m)
                    all_models.append(m)
        redis = _get_redis()
        model_stats = await model_health.get_stats(redis, all_models) if redis else []
        # Sort: unhealthy first, then by failures desc
        model_stats.sort(key=lambda x: (not x["unhealthy"], -x["failure"]))
    except Exception as exc:
        logger.warning("health model stats failed", error=str(exc)[:200])
        model_stats = []

    return render(
        "health.html",
        title="Health-чек",
        pretitle="Системный статус",
        active="health",
        health=health,
        model_stats=model_stats,
        recent_errors=recent_errors,
    )


# ── COHORTS & HEATMAP ───────────────────────────────────────────────────────────


@router.get("/cohorts", response_class=HTMLResponse)
async def cohorts_page(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        data = await get_cohort_retention(session, weeks_back=12)
    return render(
        "cohorts.html", title="Cohort retention", pretitle="Аналитика", active="cohorts", data=data
    )


@router.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page(admin_session: str | None = Cookie(default=None), days: int = 30):
    if not _check_auth(admin_session):
        return _redirect_login()
    days = max(1, min(days, 365))
    async with AsyncSessionFactory() as session:
        data = await get_activity_heatmap(session, days=days)
    return render(
        "heatmap.html",
        title="Heatmap активности",
        pretitle="Аналитика",
        active="heatmap",
        data=data,
    )


# ── A/B EXPERIMENTS ─────────────────────────────────────────────────────────────


@router.get("/experiments", response_class=HTMLResponse)
async def experiments_list(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        experiments = (
            (await session.execute(select(Experiment).order_by(Experiment.created_at.desc())))
            .scalars()
            .all()
        )
        # Count assignments per experiment
        count_rows = (
            await session.execute(
                select(ExperimentAssignment.experiment_id, func.count()).group_by(
                    ExperimentAssignment.experiment_id
                )
            )
        ).all()
        counts = {eid: c for eid, c in count_rows}
    return render(
        "experiments/list.html",
        title="A/B Эксперименты",
        active="experiments",
        experiments=experiments,
        counts=counts,
    )


@router.get("/experiments/new", response_class=HTMLResponse)
async def experiments_new(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    return render("experiments/new.html", title="Новый эксперимент", active="experiments")


@router.post("/experiments/create")
async def experiments_create(
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    form = await request.form()
    name = (form.get("name") or "").strip()
    target = (form.get("target") or "welcome").strip()
    description = (form.get("description") or "").strip()
    variants: dict[str, str] = {}
    for key in ("A", "B", "C", "D"):
        v = (form.get(f"variant_{key}") or "").strip()
        if v:
            variants[key] = v
    if not name or len(variants) < 2:
        return RedirectResponse("/admin/experiments/new", status_code=302)

    async with AsyncSessionFactory() as session:
        exp = Experiment(
            name=name, target=target, description=description, variants=variants, is_active=True
        )
        session.add(exp)
        await session.commit()
    return RedirectResponse("/admin/experiments?msg=Эксперимент+запущен", status_code=302)


@router.get("/experiments/{exp_id}", response_class=HTMLResponse)
async def experiments_detail(exp_id: int, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        exp = await session.scalar(select(Experiment).where(Experiment.id == exp_id))
        if not exp:
            return HTMLResponse("Not found", status_code=404)
        results = await get_experiment_results(session, exp_id)
    best_rate = max((r["rate"] for r in results), default=0.0)
    return render(
        "experiments/detail.html",
        title=f"Эксперимент: {exp.name}",
        active="experiments",
        exp=exp,
        results=results,
        best_rate=best_rate,
    )


@router.post("/experiments/{exp_id}/toggle")
async def experiments_toggle(exp_id: int, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        exp = await session.scalar(select(Experiment).where(Experiment.id == exp_id))
        if exp:
            exp.is_active = not exp.is_active
            await session.commit()
    return RedirectResponse(f"/admin/experiments/{exp_id}", status_code=302)


@router.post("/experiments/{exp_id}/delete")
async def experiments_delete(exp_id: int, admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        await session.execute(
            ExperimentAssignment.__table__.delete().where(
                ExperimentAssignment.experiment_id == exp_id
            )
        )
        await session.execute(Experiment.__table__.delete().where(Experiment.id == exp_id))
        await session.commit()
    return RedirectResponse("/admin/experiments?msg=Удалён", status_code=302)


# ── CSV EXPORTS ─────────────────────────────────────────────────────────────────


def _csv_response(rows: list[list], header: list[str], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/export/users.csv")
async def export_users_csv(admin_session: str | None = Cookie(default=None)):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        users = (
            (await session.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
        )
    rows = [
        [
            u.id,
            u.telegram_id,
            u.username or "",
            u.full_name or "",
            u.credits,
            u.preferred_model,
            u.streak_days,
            int(u.is_banned),
            u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
            u.last_active_at.strftime("%Y-%m-%d %H:%M") if u.last_active_at else "",
        ]
        for u in users
    ]
    header = [
        "id",
        "telegram_id",
        "username",
        "full_name",
        "credits",
        "preferred_model",
        "streak_days",
        "banned",
        "created_at",
        "last_active_at",
    ]
    return _csv_response(rows, header, "users.csv")


@router.get("/api/export/payments.csv")
async def export_payments_csv(
    admin_session: str | None = Cookie(default=None),
    status: str = "",
    method: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()
    async with AsyncSessionFactory() as session:
        q = (
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .order_by(Payment.created_at.desc())
        )
        if status:
            try:
                q = q.where(Payment.status == PaymentStatus(status))
            except ValueError:
                pass
        if method:
            q = q.where(Payment.payment_method == method)
        items = (await session.execute(q)).all()
    rows = [
        [
            p.id,
            u.telegram_id,
            u.username or "",
            float(p.amount),
            p.currency,
            p.payment_method,
            p.status.value if hasattr(p.status, "value") else p.status,
            p.external_id or "",
            (p.extra or {}).get("credits", ""),
            p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
        ]
        for p, u in items
    ]
    header = [
        "id",
        "telegram_id",
        "username",
        "amount",
        "currency",
        "method",
        "status",
        "external_id",
        "credits",
        "created_at",
    ]
    return _csv_response(rows, header, "payments.csv")


@router.get("/api/export/events.csv")
async def export_events_csv(
    admin_session: str | None = Cookie(default=None),
    event_type: str = "",
    days: int = 30,
):
    if not _check_auth(admin_session):
        return _redirect_login()
    days = max(1, min(days, 365))
    since = datetime.now(UTC) - timedelta(days=days)

    async with AsyncSessionFactory() as session:
        q = (
            select(BotEvent, User.username, User.telegram_id)
            .outerjoin(User, User.id == BotEvent.user_id)
            .where(BotEvent.created_at >= since)
            .order_by(BotEvent.created_at.desc())
        )
        if event_type:
            q = q.where(BotEvent.event_type == event_type)
        items = (await session.execute(q)).all()
    rows = [
        [
            e.id,
            tg_id or "",
            uname or "",
            e.event_type,
            e.model_key or "",
            e.credits_spent,
            e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
        ]
        for e, uname, tg_id in items
    ]
    header = [
        "id",
        "telegram_id",
        "username",
        "event_type",
        "model_key",
        "credits_spent",
        "created_at",
    ]
    return _csv_response(rows, header, "events.csv")


# ── SUPPORT TICKETS ─────────────────────────────────────────────────────────────


@router.get("/support", response_class=HTMLResponse)
async def support_list(
    admin_session: str | None = Cookie(default=None),
    status: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.support import list_tickets

    async with AsyncSessionFactory() as session:
        tickets = await list_tickets(session, status=(status or None))
    return render(
        "support/list.html", title="Поддержка", active="support", tickets=tickets, status=status
    )


@router.get("/support/{ticket_id}", response_class=HTMLResponse)
async def support_thread(
    ticket_id: int,
    admin_session: str | None = Cookie(default=None),
    msg: str = "",
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.support import get_messages, get_ticket

    async with AsyncSessionFactory() as session:
        ticket = await get_ticket(session, ticket_id)
        if not ticket:
            return HTMLResponse("Не найдено", status_code=404)
        messages = await get_messages(session, ticket_id)
        user = await session.scalar(select(User).where(User.id == ticket.user_id))
    return render(
        "support/thread.html",
        title=f"Обращение #{ticket_id}",
        active="support",
        msg=msg,
        ticket=ticket,
        messages=messages,
        user=user,
    )


@router.post("/support/{ticket_id}/reply")
async def support_reply(
    ticket_id: int,
    request: Request,
    body: str = Form(...),
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    import html as _html

    from app.database.repo.support import add_admin_reply, add_system_note, get_ticket

    body = (body or "").strip()
    if not body:
        return RedirectResponse(f"/admin/support/{ticket_id}?msg=Пустой+ответ", status_code=302)

    async with AsyncSessionFactory() as session:
        ticket = await get_ticket(session, ticket_id)
        if not ticket:
            return HTMLResponse("Не найдено", status_code=404)
        user = await session.scalar(select(User).where(User.id == ticket.user_id))
        await add_admin_reply(session, ticket_id, body)
        await session.commit()
        telegram_id = user.telegram_id if user else None

    delivered = False  # ставим True только после фактической отправки
    if telegram_id:
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.types import InlineKeyboardButton as IB
        from aiogram.types import InlineKeyboardMarkup as IM

        from app.bot import get_bot

        bot = get_bot()
        if bot:
            kb = IM(
                inline_keyboard=[
                    [IB(text="Дописать к обращению", callback_data=f"support_reply:{ticket_id}")],
                    [IB(text="Вопрос решён", callback_data=f"support_close:{ticket_id}")],
                ]
            )
            text = f"💬 <b>Ответ поддержки по обращению №{ticket_id}:</b>\n\n{_html.escape(body)}"
            try:
                await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=kb)
                delivered = True
            except TelegramForbiddenError:
                logger.warning(
                    "support reply not delivered: user blocked bot",
                    ticket_id=ticket_id,
                    telegram_id=telegram_id,
                )
                async with AsyncSessionFactory() as session:
                    await add_system_note(
                        session,
                        ticket_id,
                        "⚠️ Ответ не доставлен: пользователь заблокировал бота.",
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning(
                    "support reply send failed", ticket_id=ticket_id, error=str(exc)[:200]
                )

    log_action_bg(
        "token",
        "support.reply",
        ip=request.client.host if request.client else None,
        ticket_id=ticket_id,
        delivered=delivered,
    )
    note = "Ответ+отправлен" if delivered else "Ответ+сохранён,+но+не+доставлен"
    return RedirectResponse(f"/admin/support/{ticket_id}?msg={note}", status_code=302)


@router.post("/support/{ticket_id}/close")
async def support_close(
    ticket_id: int,
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    if not _check_auth(admin_session):
        return _redirect_login()
    from app.database.repo.support import close_ticket as _close

    async with AsyncSessionFactory() as session:
        await _close(session, ticket_id)
        await session.commit()
    log_action_bg(
        "token",
        "support.close",
        ip=request.client.host if request.client else None,
        ticket_id=ticket_id,
    )
    return RedirectResponse(f"/admin/support/{ticket_id}?msg=Обращение+закрыто", status_code=302)


@router.get("/support/{ticket_id}/attachment/{message_id}")
async def support_attachment(
    ticket_id: int,
    message_id: int,
    admin_session: str | None = Cookie(default=None),
):
    """Proxy фото вложения тикета по attachment_file_id (как photo_templates_thumb)."""
    if not _check_auth(admin_session):
        return Response(status_code=403)
    from app.database.models import SupportMessage

    async with AsyncSessionFactory() as session:
        msg = await session.scalar(
            select(SupportMessage).where(
                SupportMessage.id == message_id,
                SupportMessage.ticket_id == ticket_id,
            )
        )
    if not msg or not msg.attachment_file_id:
        return Response(status_code=404)
    try:
        from app.bot import get_bot

        bot = get_bot()
        if not bot:
            return Response(status_code=503)
        tg_file = await bot.get_file(msg.attachment_file_id)
        data = await bot.download_file(tg_file.file_path)
        return Response(
            content=data.read(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception as exc:
        logger.warning(
            "support attachment proxy failed", message_id=message_id, error=str(exc)[:200]
        )
        return Response(status_code=500)


# ---------------------------------------------------------------------------

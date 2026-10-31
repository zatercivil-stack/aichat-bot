from __future__ import annotations

from ipaddress import ip_address, ip_network

import structlog
from fastapi import FastAPI, Request, Response

logger = structlog.get_logger(__name__)

app = FastAPI(docs_url=None, redoc_url=None)


@app.middleware('http')
async def csrf_and_session_middleware(request: Request, call_next):
    """CSRF-защита админки + установка сессии в contextvar для render().

    На POST к /admin/* (кроме /login) проверяем form-поле csrf_token против
    HMAC(secret, admin_session). Невалидно → 403. Остальные методы/пути —
    пропускаем. Сессию кладём в contextvar, чтобы render() вставил токен
    в формы.
    """
    from app.webserver.csrf import csrf_valid, current_session
    session = request.cookies.get('admin_session')
    token = current_session.set(session)
    try:
        path = request.url.path
        if (request.method == 'POST'
                and path.startswith('/admin')
                and path not in ('/admin/login', '/admin/login/')):
            try:
                # КРИТИЧНО: сперва кэшируем СЫРОЕ тело через request.body().
                # Starlette BaseHTTPMiddleware (_CachedRequest) реплеит тело
                # вниз по стеку, ТОЛЬКО если был вызван .body() — он ставит
                # self._body. А .form() сам по себе читает self.stream() и
                # _body НЕ ставит → downstream-хендлер получает ПУСТУЮ форму
                # (все Form()/File() пустые). Без body() здесь ломаются ВСЕ
                # админ-POST'ы (ban, кредиты, bulk, промокоды, цены, загрузка
                # фото-шаблона и т.д.) — проверено репро на starlette 0.52.
                await request.body()
                form = await request.form()
                submitted = form.get('csrf_token')
            except Exception:
                submitted = None
            if not csrf_valid(session, submitted):
                logger.warning('CSRF validation failed', path=path)
                return Response('CSRF token invalid or missing', status_code=403)
        return await call_next(request)
    finally:
        current_session.reset(token)

from app.webserver.admin import router as admin_router

app.include_router(admin_router)

YOOKASSA_IP_NETWORKS = [
    ip_network('185.71.76.0/27'),
    ip_network('185.71.77.0/27'),
    ip_network('77.75.153.0/25'),
    ip_network('77.75.154.128/25'),
    ip_network('77.75.156.11/32'),
    ip_network('77.75.156.35/32'),
]


def _is_yookassa_ip(request: Request) -> bool:
    """Проверка IP для YooKassa webhook.

    24.05 ФИКС уязвимости: раньше брался ПЕРВЫЙ X-Forwarded-For (контролируется
    атакующим) + слепо доверялись private/loopback → любой мог послать
    `X-Forwarded-For: 127.0.0.1` и обойти проверку. Теперь:
      • берём ПОСЛЕДНИЙ X-Forwarded-For — его добавляет НАШ Caddy (реальный
        удалённый IP, его атакующий подделать не может);
      • НИКАКОГО слепого allow для private/loopback;
      • проверяем только против официальных сетей YooKassa.
    Это первый слой; главная защита — verify_yookassa_payment (API)."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        raw = xff.split(',')[-1].strip()  # последний = добавленный Caddy
    else:
        raw = (request.client.host if request.client else '') or ''

    logger.info('Webhook IP check', ip=raw)
    try:
        ip = ip_address(raw)
        is_yookassa = any(ip in net for net in YOOKASSA_IP_NETWORKS)
        if not is_yookassa:
            logger.warning('Webhook: blocked non-YooKassa IP', ip=str(ip))
        return is_yookassa
    except ValueError as e:
        logger.warning('Webhook: invalid IP format', raw=raw, error=str(e))
        return False


@app.post('/yookassa-webhook')
async def yookassa_webhook(request: Request) -> Response:
    if not _is_yookassa_ip(request):
        logger.warning('YooKassa webhook: forbidden IP', ip=request.client.host if request.client else 'unknown')
        return Response(status_code=403)

    logger.info('YooKassa webhook received')

    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    event = data.get('event', '')
    logger.info('YooKassa webhook received')

    if event != 'payment.succeeded':
        # ВАЖНО: ключ не 'event' — structlog уже занимает его под текст лога
        # (иначе TypeError → 500 → YooKassa ретраит не-succeeded вебхуки).
        logger.info('YooKassa webhook: event not payment.succeeded', yk_event=event)
        return Response(status_code=200)

    obj = data.get('object', {})
    yk_payment_id = obj.get('id', '')
    metadata = obj.get('metadata', {})
    payment_db_id_str = metadata.get('payment_db_id', '')
    metadata.get('kind', 'pack')

    if not payment_db_id_str:
        logger.warning('YooKassa webhook: no payment_db_id in metadata', yk_id=yk_payment_id)
        return Response(status_code=200)

    try:
        payment_db_id = int(payment_db_id_str)
    except ValueError:
        logger.warning('YooKassa webhook: invalid payment_db_id', raw=payment_db_id_str)
        return Response(status_code=200)

    # ГЛАВНАЯ защита: верифицируем платёж напрямую через YooKassa API. Даже
    # если IP-чек обойдён (спуфинг) — поддельный webhook не начислит кредиты,
    # т.к. статус и payment_db_id берём из ответа YooKassa, а не из тела POST.
    from app.services.payment_verify import verify_yookassa_payment
    if not await verify_yookassa_payment(yk_payment_id, payment_db_id):
        logger.warning('YooKassa webhook: API verification FAILED — rejecting',
                       yk_id=yk_payment_id, payment_db_id=payment_db_id)
        return Response(status_code=200)  # 200 чтобы YooKassa не ретраила бесконечно

    # Передаём payment_method-объект — это нужно для сохранения recurring card
    # (auto_renew). Без него подписка активируется, но без автопродления.
    pm_payload = obj.get('payment_method') or {}

    from app.services.payment_processing import process_succeeded_payment
    ok = await process_succeeded_payment(
        payment_db_id, external_id=yk_payment_id, pm_payload=pm_payload,
    )
    logger.info('YooKassa webhook: processed', payment_db_id=payment_db_id, success=ok)
    return Response(status_code=200)



@app.get('/yookassa-webhook')
async def yookassa_webhook_check() -> dict:
    return {'status': 'ok', 'endpoint': 'yookassa-webhook'}


@app.get('/health')
async def health() -> dict:
    return {'status': 'ok', 'bot': 'neyra'}


# Tracked-redirect (/r/{source}) удалён: Aurum теперь сам пишет traffic_source
# при /start chan_*, ссылки в канале идут напрямую на t.me/Shop_Aurum_Bot?start=...
# Если в будущем понадобится трекать клики на внешние домены — восстановить из git.

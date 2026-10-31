"""Серверная верификация платежа напрямую через YooKassa API.

Главная защита вебхука: даже если злоумышленник подделает POST
/yookassa-webhook (обойдя IP-чек спуфингом X-Forwarded-For), кредиты НЕ
начислятся — статус и сумму платежа берём не из тела запроса, а из ответа
YooKassa по payment_id. Подделать ответ YooKassa нельзя (нет доступа к их API
от нашего shop_id/secret).
"""
from __future__ import annotations

import asyncio

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)


async def verify_yookassa_payment(yk_payment_id: str, payment_db_id: int) -> bool:
    """True только если YooKassa подтверждает: платёж yk_payment_id РЕАЛЬНО
    succeeded И его metadata.payment_db_id совпадает с переданным (защита от
    подстановки чужого payment_db_id). Любая ошибка/несовпадение → False."""
    if not settings.yookassa_enabled:
        # Без ключей верифицировать нечем — безопаснее отказать.
        logger.warning('verify_yookassa: yookassa disabled, rejecting')
        return False
    if not yk_payment_id:
        return False

    def _find():
        from yookassa import Configuration
        from yookassa import Payment as YKPayment
        Configuration.account_id = settings.YOOKASSA_SHOP_ID
        Configuration.secret_key = settings.YOOKASSA_SECRET_KEY
        return YKPayment.find_one(yk_payment_id)

    try:
        # SDK синхронный — уводим в тред, чтобы не блокировать event loop.
        payment = await asyncio.to_thread(_find)
    except Exception as exc:
        logger.warning('verify_yookassa: API lookup failed',
                       yk_id=yk_payment_id, error=str(exc)[:200])
        return False

    status = getattr(payment, 'status', None)
    if status != 'succeeded':
        logger.warning('verify_yookassa: status not succeeded',
                       yk_id=yk_payment_id, status=status)
        return False

    # metadata на стороне YooKassa (мы её записали при создании платежа) —
    # сверяем payment_db_id, чтобы webhook нельзя было перенаправить на чужой.
    meta = getattr(payment, 'metadata', None) or {}
    meta_db_id = str(meta.get('payment_db_id', ''))
    if meta_db_id != str(payment_db_id):
        logger.warning('verify_yookassa: payment_db_id mismatch',
                       yk_id=yk_payment_id, expected=payment_db_id, got=meta_db_id)
        return False

    # Локальная запись платежа — сверяем сумму и валюту.
    from decimal import Decimal

    from app.database.base import AsyncSessionFactory
    from app.database.repo.payments import get_payment
    async with AsyncSessionFactory() as session:
        local = await get_payment(session, payment_db_id)
    if local is None:
        logger.warning('verify_yookassa: local payment not found', payment_db_id=payment_db_id)
        return False

    yk_amount = getattr(payment, 'amount', None)
    yk_value = getattr(yk_amount, 'value', None)
    yk_currency = getattr(yk_amount, 'currency', None)
    try:
        if yk_value is None or Decimal(str(yk_value)) != Decimal(str(local.amount)):
            logger.warning('verify_yookassa: amount mismatch',
                           yk_id=yk_payment_id, expected=str(local.amount), got=str(yk_value))
            return False
    except Exception:
        logger.warning('verify_yookassa: invalid amount', yk_id=yk_payment_id, got=str(yk_value))
        return False
    if yk_currency and str(yk_currency).upper() != str(local.currency).upper():
        logger.warning('verify_yookassa: currency mismatch',
                       yk_id=yk_payment_id, expected=local.currency, got=yk_currency)
        return False

    logger.info('verify_yookassa: confirmed', yk_id=yk_payment_id,
                payment_db_id=payment_db_id)
    return True

"""Альтернативные способы оплаты: Telegram Stars (⭐) и CryptoBot (крипта).

Оба создают Payment-запись с ТЕМ ЖЕ `extra`, что и YooKassa, и активируются
через единый `process_succeeded_payment` (кредиты + подписка + уведомление,
идемпотентно). Так не дублируем логику начисления.

Зачем: YooKassa (карта) у части аудитории не открывается под VPN / отсекается
гео; плюс шла фрод-проверка карт. Stars (баланс Telegram) и крипта обходят это.

Подтверждение:
  • Stars — нативный апдейт `successful_payment` (см. handlers/payments.py).
  • CryptoBot — поллинг `getInvoices(status=paid)` (как YooKassa-checker).
"""
from __future__ import annotations

from decimal import Decimal

import aiohttp
import structlog

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Plan
from app.database.repo.credits import get_pack_by_id, get_subscription_plan
from app.database.repo.payments import create_payment

logger = structlog.get_logger(__name__)

# Сколько Stars за 1₽. 0.8: Telegram выкупает звёзды обратно ~$0.013/шт, при
# 0.6 на выводе теряли ~28% от рублёвой цены — 0.8 покрывает комиссию платформы
# (99₽ → 79⭐ ≈ 95₽ net). Stars-цены показываются юзеру.
STARS_PER_RUB = 0.8

_CRYPTO_API = 'https://pay.crypt.bot/api'


def _rub_to_stars(rub: int) -> int:
    return max(1, round(rub * STARS_PER_RUB))


def product_info(product_id: str) -> dict | None:
    """Унифицированное описание продукта (pack ИЛИ subscription) для оплаты.
    Возвращает: kind, credits, rub, stars, expires_days, sub_plan, plan_name,
    plan_enum (Plan), title."""
    pack = get_pack_by_id(product_id)
    if pack:
        return {
            'kind': 'pack', 'credits': pack['credits'], 'rub': int(pack['rub']),
            'stars': _rub_to_stars(int(pack['rub'])),
            'expires_days': pack.get('expires_days'),
            'sub_plan': None, 'plan_name': None, 'plan_enum': Plan.FREE,
            'title': pack['name'],
        }
    sub = get_subscription_plan(product_id)
    if sub:
        return {
            'kind': 'subscription', 'credits': sub['credits_monthly'],
            'rub': int(sub['rub']), 'stars': _rub_to_stars(int(sub['rub'])),
            'expires_days': None, 'sub_plan': sub['plan_enum'],
            'plan_name': sub['name'], 'plan_enum': Plan(sub['plan_enum']),
            'title': f"Подписка {sub['name']}",
        }
    return None


def _invoice_copy(info: dict) -> tuple[str, str]:
    """Премиальные title+description для инвойса (Stars/крипта).
    Без сухих «500 кр (вечный)» — бренд + понятная польза (фото, перки)."""
    credits = info['credits']
    cr = f"{credits:,}".replace(',', ' ')  # 6 000 — пробел только в числе
    photos = max(1, credits // 200)
    if info['kind'] == 'subscription':
        title = f"Подписка {info['plan_name']} · Нейра"[:32]
        desc = (f"{cr} кр/мес + бонус кредитов каждый день. "
                f"Без водяного знака всё время подписки. Дешевле пакетов")
    else:
        title = "Нейра — AI Фотостудия"
        desc = (f"{cr} кредитов ≈ {photos} фото с твоим лицом в любой "
                f"сцене. Зачисление за минуту, без водяного знака")
    return title, desc


async def _create_pending(user_id: int, user_tg_id: int, product_id: str,
                          info: dict, method: str) -> int:
    """Создаёт PENDING Payment с extra, который понимает process_succeeded_payment.
    amount = цена в рублях (для единообразия выручки). Возвращает payment.id."""
    extra = {
        'product_id': product_id,
        'kind': info['kind'],
        'credits': info['credits'],
        'user_tg_id': user_tg_id,
        'expires_days': info.get('expires_days'),
        'sub_plan': info.get('sub_plan'),
        'plan_name': info.get('plan_name'),
        'pay_method': method,
    }
    async with AsyncSessionFactory() as s:
        p = await create_payment(
            s, user_id=user_id, amount=Decimal(info['rub']), currency='RUB',
            payment_method=method, plan=info['plan_enum'], extra=extra,
        )
        await s.commit()
        return p.id


# ─────────────────────────── Telegram Stars ────────────────────────────────
async def send_stars_invoice(bot, chat_id: int, user_id: int, user_tg_id: int,
                             product_id: str) -> bool:
    """Шлёт Stars-инвойс (XTR). payload = altpay:<payment_id> → ловится в
    handlers/payments.py.successful_payment."""
    from aiogram.types import LabeledPrice
    info = product_info(product_id)
    if not info:
        return False
    pid = await _create_pending(user_id, user_tg_id, product_id, info, 'stars')
    title, desc = _invoice_copy(info)
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=desc,
            payload=f'altpay:{pid}',
            currency='XTR',
            prices=[LabeledPrice(label='Нейра · доступ', amount=info['stars'])],
        )
        return True
    except Exception as exc:
        logger.error('stars invoice failed', error=str(exc)[:200], product=product_id)
        return False


# ─────────────────────────── CryptoBot (крипта) ────────────────────────────
async def _crypto_request(method: str, payload: dict | None = None) -> dict | None:
    if not settings.CRYPTOBOT_TOKEN:
        logger.warning('CRYPTOBOT_TOKEN не задан')
        return None
    headers = {'Crypto-Pay-API-Token': settings.CRYPTOBOT_TOKEN}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f'{_CRYPTO_API}/{method}', json=payload or {},
                              headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json()
        if not data.get('ok'):
            logger.error('cryptobot api error', method=method, resp=str(data)[:200])
            return None
        return data.get('result')
    except Exception as exc:
        logger.error('cryptobot request failed', method=method, error=str(exc)[:200])
        return None


async def create_crypto_invoice(user_id: int, user_tg_id: int, product_id: str,
                                bot_username: str) -> str | None:
    """Создаёт фиатный (RUB) инвойс в CryptoBot — юзер платит криптой по курсу.
    Возвращает pay_url или None. invoice_id сохраняем в Payment.extra для поллинга."""
    info = product_info(product_id)
    if not info:
        return None
    pid = await _create_pending(user_id, user_tg_id, product_id, info, 'cryptobot')
    _, _desc = _invoice_copy(info)
    res = await _crypto_request('createInvoice', {
        'currency_type': 'fiat',
        'fiat': 'RUB',
        'amount': str(info['rub']),
        'description': _desc[:200],
        'payload': f'altpay:{pid}',
        'paid_btn_name': 'openBot',
        'paid_btn_url': f'https://t.me/{bot_username}',
        'expires_in': 1800,
    })
    if not res:
        return None
    invoice_id = res.get('invoice_id')
    pay_url = res.get('bot_invoice_url') or res.get('pay_url') or res.get('mini_app_invoice_url')
    # сохраняем invoice_id в extra для поллинга
    try:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from app.database.models import Payment
        async with AsyncSessionFactory() as s:
            p = await s.scalar(select(Payment).where(Payment.id == pid))
            if p:
                extra = dict(p.extra or {})
                extra['crypto_invoice_id'] = invoice_id
                p.extra = extra
                flag_modified(p, 'extra')
                await s.commit()
    except Exception as exc:
        logger.warning('save crypto invoice_id failed', error=str(exc)[:150])
    return pay_url


async def poll_crypto_payments(bot) -> None:
    """Поллер: берёт оплаченные инвойсы CryptoBot и активирует соответствующие
    PENDING-платежи через process_succeeded_payment. Идемпотентно."""
    from sqlalchemy import select

    from app.database.models import Payment, PaymentStatus
    from app.services.payment_processing import process_succeeded_payment

    # наши pending cryptobot-платежи
    async with AsyncSessionFactory() as s:
        pend = (await s.execute(
            select(Payment).where(Payment.payment_method == 'cryptobot',
                                  Payment.status == PaymentStatus.PENDING)
        )).scalars().all()
    if not pend:
        return
    by_invoice = {}
    for p in pend:
        inv = (p.extra or {}).get('crypto_invoice_id')
        if inv:
            by_invoice[int(inv)] = p.id

    if not by_invoice:
        return
    res = await _crypto_request('getInvoices', {
        'invoice_ids': ','.join(str(i) for i in by_invoice),
        'status': 'paid',
    })
    items = (res or {}).get('items', []) if isinstance(res, dict) else (res or [])
    for inv in items:
        if inv.get('status') != 'paid':
            continue
        pid = by_invoice.get(int(inv.get('invoice_id', 0)))
        if pid:
            try:
                await process_succeeded_payment(pid, external_id=str(inv.get('invoice_id')), bot=bot)
                logger.info('cryptobot payment activated', payment_id=pid)
            except Exception as exc:
                logger.error('cryptobot activate failed', payment_id=pid, error=str(exc)[:200])

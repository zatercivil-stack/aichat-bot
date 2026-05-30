from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import PaymentStatus
from app.database.repo.credits import add_credits
from app.database.repo.payments import get_pending_payments, update_payment
from app.database.repo.users import get_all_telegram_ids, get_total_users_count, get_user
from app.config import settings
from app.keyboards.main import (
    PE_AU_GOLD, PE_BUBBLE, PE_CARD, PE_LIGHTNING, PE_ROCKET, PE_SPARKLES,
)

logger = structlog.get_logger(__name__)
router = Router()

_payment_checker_task = None


@router.message(
    F.photo,
    F.caption.startswith('setpreview:'),
    F.from_user.id.in_(set(settings.admin_ids_list)),
)
async def cmd_set_preview(message: Message) -> None:
    """Админ-инструмент: сохранить присланное фото как превью пресета.
    Подпись: setpreview:<key>:<pos>  (pos 0=жен, 1=муж).
    admin.router подключён первым → перехватывает фото до vision-обработчика.
    Фильтр in_(admin_ids) в декораторе — чтобы НЕ перехватывать фото обычных
    юзеров (даже если они случайно/намеренно поставят такую подпись)."""
    from app.database.repo.preset_previews import save_preview
    from app.handlers.image_edit import reload_custom_presets, EDIT_PRESETS
    parts = (message.caption or '').split(':')
    if len(parts) < 3:
        await message.answer('Формат: <code>setpreview:&lt;key&gt;:&lt;0|1&gt;</code>', parse_mode='HTML')
        return
    key, pos_raw = parts[1].strip(), parts[2].strip()
    try:
        pos = int(pos_raw)
    except ValueError:
        await message.answer('pos должен быть 0 (жен) или 1 (муж).')
        return
    if key not in EDIT_PRESETS:
        await message.answer(f'Пресет «{key}» не найден.')
        return
    fid = message.photo[-1].file_id
    async with AsyncSessionFactory() as session:
        await save_preview(session, key, fid, position=pos, source_file='manual')
        await session.commit()
    await reload_custom_presets()
    gender = 'муж' if pos == 1 else 'жен'
    await message.answer(f'✅ Превью «{key}» ({gender}) сохранено из присланного фото.')


async def ensure_default_promos() -> None:
    """Создать промокоды по умолчанию при старте, если их ещё нет."""
    from app.database.repo.promos import create_promo, get_promo

    defaults = [
        # код, кредиты, максимум использований (0 = безлимит), дней до истечения (0 = бессрочно)
        ('СТАРТ149', 500, 0, 30),
    ]
    async with AsyncSessionFactory() as session:
        for code, credits, max_uses, expire_days in defaults:
            existing = await get_promo(session, code)
            if existing:
                continue
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=expire_days)
                if expire_days > 0
                else None
            )
            try:
                await create_promo(session, code, credits, max_uses, expires_at)
                logger.info('Default promo created', code=code, credits=credits)
            except Exception as e:
                logger.error('Failed to create default promo', code=code, error=e)
        await session.commit()


# YooKassa использует американское написание 'canceled' (одна l). Британское
# 'cancelled' держим для совместимости — мало ли отдадут оба варианта.
_YOOKASSA_FAILED_STATUSES = {'canceled', 'cancelled', 'expired', 'refunded'}
_PAYMENT_EXPIRE_HOURS = 24


async def check_yookassa_payments(bot=None) -> None:
    """Проверять статус платежей YooKassa каждые 60 секунд."""
    try:
        from yookassa import Configuration, Payment as YKPayment

        Configuration.account_id = settings.YOOKASSA_SHOP_ID
        Configuration.secret_key = settings.YOOKASSA_SECRET_KEY

        now_utc = datetime.now(timezone.utc)

        async with AsyncSessionFactory() as session:
            pending = await get_pending_payments(session)

            # Список платежей которые нужно обработать через общий helper.
            # Собираем тут (с активной сессией), вызываем helper ПОСЛЕ закрытия
            # сессии — helper открывает свою.
            to_activate: list[tuple[int, str]] = []  # (payment_db_id, yk_id)

            for payment in pending:
                # Автоматически отменяем платежи без external_id старше N часов
                if not payment.external_id:
                    created = payment.created_at
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if (now_utc - created).total_seconds() > _PAYMENT_EXPIRE_HOURS * 3600:
                        await update_payment(session, payment.id, PaymentStatus.FAILED)
                        logger.info('Payment auto-expired (no external_id)',
                                    payment_id=payment.id)
                    continue

                # Обрабатываем И обычные паки (yookassa) И подписки (yookassa_sub).
                # До 2026-05-17 фильтр игнорировал _sub — подписки не активировались
                # через checker, юзер платил и ничего не получал.
                if payment.payment_method not in ('yookassa', 'yookassa_sub'):
                    continue

                try:
                    yk_payment = YKPayment.find_one(payment.external_id)
                except Exception as e:
                    logger.error('Failed to check YooKassa payment status',
                                 payment_id=payment.id, external_id=payment.external_id, error=e)
                    continue

                if yk_payment.status == 'succeeded':
                    to_activate.append((payment.id, payment.external_id))
                elif yk_payment.status in _YOOKASSA_FAILED_STATUSES:
                    await update_payment(session, payment.id, PaymentStatus.FAILED)
                    logger.info('YooKassa payment marked failed',
                                payment_id=payment.id, yk_status=yk_payment.status)

            await session.commit()

        # Активация через общий helper. Делает то же что webhook
        # (кредиты + subscription + уведомление). Без pm_payload, поэтому
        # auto_renew не включится — но это OK для fallback-пути.
        if to_activate:
            from app.services.payment_processing import process_succeeded_payment
            for payment_id, yk_id in to_activate:
                try:
                    await process_succeeded_payment(
                        payment_id, external_id=yk_id, bot=bot,
                    )
                    logger.info('Payment activated via checker', payment_id=payment_id)
                except Exception as e:
                    logger.error('Failed to activate payment via checker',
                                 payment_id=payment_id, error=str(e)[:200])

    except Exception as e:
        logger.error('Failed to check YooKassa payments', error=e)


def start_payment_checker(bot_instance) -> None:
    """Запустить периодическую проверку платежей"""
    global _payment_checker_task
    logger.info('Creating payment checker task...')

    async def payment_checker_loop():
        while True:
            try:
                await check_yookassa_payments(bot_instance)
                await asyncio.sleep(60)  # Проверять каждую минуту для быстрого зачисления
            except Exception as e:
                logger.error('Payment checker error', error=e)
                await asyncio.sleep(60)

    _payment_checker_task = asyncio.create_task(payment_checker_loop())
    logger.info('Payment checker task created')


def stop_payment_checker() -> None:
    """Остановить проверку платежей"""
    global _payment_checker_task
    if _payment_checker_task:
        _payment_checker_task.cancel()
        logger.info('Payment checker stopped')


# ─── Daily push: умная сегментация + ротация ──────────────────────────────────

_BOT_URL = 'https://t.me/NeyraChatBot'

# Кнопка push'ей — ведёт СРАЗУ в каталог сцен (а не открывает чат бота вхолостую).
# callback gallery_open работает в личке: открывает карусель пресетов с превью.
_OPEN_BOT_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton.model_construct(
        text='Сделать фото с моим лицом',
        callback_data='gallery_open',
        icon_custom_emoji_id=PE_SPARKLES, style='success',
    )],
])

_BUY_PUSH_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton.model_construct(
        text='Пополнить — от 99₽',
        callback_data='shop',
        icon_custom_emoji_id=PE_LIGHTNING, style='success',
    )],
    [InlineKeyboardButton.model_construct(
        text='Подписка выгоднее',
        callback_data='subscription',
        icon_custom_emoji_id=PE_AU_GOLD, style='primary',
    )],
])

_SUB_PUSH_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton.model_construct(
        text='СТАРТ — 149₽/мес',
        callback_data='choose_plan:starter',
        icon_custom_emoji_id=PE_LIGHTNING, style='primary',
    )],
    [InlineKeyboardButton.model_construct(
        text='ПРО — 399₽/мес',
        callback_data='choose_plan:pro',
        icon_custom_emoji_id=PE_ROCKET, style='success',
    )],
    [InlineKeyboardButton.model_construct(
        text='Купить разово',
        callback_data='shop',
        icon_custom_emoji_id=PE_CARD,
    )],
])

# Ротация по дням недели для активных пользователей (0=Пн … 6=Вс).
# 24.05 v2: monetization-push переехал на ПЯТНИЦУ (получка, расходное
# настроение), воскресенье стало «итог недели + Mystery». Убраны хардкод
# названий сцен (marble_punk запрещён в broadcasts — синхронизируем). Топ
# сцены подтягиваем динамически через build_day_push_text().
_DAY_PUSH_HARDCODED: list[tuple[str, InlineKeyboardMarkup]] = [
    # Понедельник — старт недели, аватарка
    (
        "✨ <b>Понедельник = новая аватарка.</b>\n\n"
        "Пока все только просыпаются — у тебя уже свежий профиль.\n"
        "Одно селфи + сцена из каталога = готово за минуту.\n\n"
        "Жми → выбери настроение → пришли селфи.",
        _OPEN_BOT_KB,
    ),
    # Вторник — wow-сцена «под водой»
    (
        "🌊 <b>Ты под водой, сверху красный свет.</b>\n\n"
        "Кадр как с обложки: толща воды, твоё лицо, кинематографичный свет.\n\n"
        "Одно селфи — и ты там.",
        _OPEN_BOT_KB,
    ),
    # Среда — соц-доказательство (Mystery дня)
    (
        "🎲 <b>Что выбирают сегодня</b> — листай каталог, "
        "там есть Сюрприз дня (один пресет бесплатно).\n\n"
        "Большая коллекция готовых сцен. Лицо в кадре узнаётся как твоё, "
        "а не чужая модель.\n\n"
        "Открой каталог 👇",
        _OPEN_BOT_KB,
    ),
    # Четверг — анонс новых
    (
        "🔥 <b>В каталоге — свежие сцены.</b>\n\n"
        "Резкие, стильные, ни на что не похожие. Селфи → сцена → готово "
        "за минуту.",
        _OPEN_BOT_KB,
    ),
    # ПЯТНИЦА — MONETIZATION (получка, аппетит на покупки)
    (
        "💎 <b>Пятница — самое время оформить подписку.</b>\n\n"
        "<b>СТАРТ · 149₽/мес</b>\n"
        "• <b>1 000 кр</b> сразу = ~5 топ-сцен\n"
        "• <b>+30 кр каждый день</b> 30 дней\n"
        "• Вся коллекция, новые сцены, без водяного знака\n\n"
        "За выходные сделаешь себе целый фотосет.",
        _SUB_PUSH_KB,
    ),
    # Суббота — выходной, экспериментируй
    (
        "🌅 <b>Выходной — примерь новый образ.</b>\n\n"
        "Открой каталог, тыкай примеры свободно — селфи попрошу только "
        "когда выберешь сцену.\n\n"
        "Одно фото открывает всю коллекцию 👇",
        _OPEN_BOT_KB,
    ),
    # Воскресенье — итог недели + Mystery
    (
        "🎁 <b>Воскресенье — день сюрпризов.</b>\n\n"
        "В главном меню есть <b>Подарок дня</b> — один пресет бесплатно.\n"
        "Плюс <b>Колесо удачи</b> (раз в сутки бесплатные кредиты).\n\n"
        "Забери и до начала недели сделай себе обновлённый профиль.",
        _OPEN_BOT_KB,
    ),
]


async def build_day_push() -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает (текст, kb) пуша на сегодня. Берёт хардкод-шаблон по дню
    недели, БЕЗ упоминания конкретных названий сцен — они меняются и пуш
    не должен врать. Если есть активный Mystery дня — добавляем его строкой
    в конец как «бонус-крючок»."""
    weekday = datetime.now().weekday()
    text, kb = _DAY_PUSH_HARDCODED[weekday]
    try:
        from app.services.mystery_preset import get_today_mystery
        m = await get_today_mystery()
        if m:
            # Strip emoji from label for cleanliness
            import re
            clean = re.sub(r'^\W+', '', m['label']).strip()
            text = (
                text + f"\n\n🎲 <i>Сегодня бесплатно — «{clean}»</i>"
            )
    except Exception:
        pass
    return text, kb

# Сообщения для тех, кто молчал 2-7 дней.
# Без «соскучился» — нейтральный, уважительный тон. Каждое сообщение
# напоминает об одной из главных фишек.
_REENGAGE_MSGS: list[str] = [
    (
        "🌊 <b>Пока тебя не было — добавили новые сцены.</b>\n\n"
        "Ты под водой в красном свете, в неоне будущего, на обложке глянца. "
        "Лицо в кадре — твоё, не похожее.\n\n"
        "Пришли селфи — покажу каталог."
    ),
    (
        "🎁 <b>Каждый день в боте — Подарок дня.</b>\n\n"
        "Один пресет бесплатно (без списания кредитов). "
        "Сегодняшняя сцена ждёт в главном меню — забери до полуночи.\n\n"
        "Одно селфи — и поехали."
    ),
    (
        "🎯 <b>На счету копятся бесплатные кредиты.</b>\n\n"
        "Загляни — хватит на новую сцену. А если ты в канале "
        "@conturD, бесплатных кредитов <b>40 в день вместо 15</b>.\n\n"
        "Плюс там секретные промокоды."
    ),
]

# Сообщения для тех, кто молчал 7-14 дней.
# Тон ещё более спокойный — не давим, напоминаем о ценности + что нового.
_WINBACK_MSGS: list[str] = [
    (
        "👋 <b>Давно не виделись — в Нейре стало интереснее.</b>\n\n"
        "Новые сцены: неон будущего, обложки глянца, под водой с золотыми "
        "рыбками. Плюс Подарок дня — один пресет бесплатно каждый день.\n\n"
        "Вернуться — одно селфи."
    ),
    (
        "🎁 <b>Бесплатные кредиты тебя дождались.</b>\n\n"
        "Хватит на новую сцену из каталога. И каждый день — Подарок дня "
        "(один пресет совсем без списания).\n\n"
        "Подпишись на @conturD — будет 40 кр в день вместо 15."
    ),
]

_daily_push_scheduler = None


async def dispatch_scheduled_broadcasts() -> None:
    """Минутный диспатчер: берёт ScheduledBroadcast.status='pending' с send_at<=now,
    помечает 'running' и зовёт _do_preset_broadcast. Импорт ленивый — модуль
    webserver/admin.py содержит саму отправку и связанные хелперы."""
    from sqlalchemy import select as _sel
    from app.database.base import AsyncSessionFactory
    from app.database.models import ScheduledBroadcast
    from app.webserver.admin import _do_preset_broadcast

    now = datetime.now(timezone.utc)
    async with AsyncSessionFactory() as session:
        due = (await session.execute(
            _sel(ScheduledBroadcast).where(
                ScheduledBroadcast.status == 'pending',
                ScheduledBroadcast.send_at <= now,
            ).order_by(ScheduledBroadcast.send_at)
        )).scalars().all()
        if not due:
            return
        # помечаем running ДО запуска (чтобы повторный тик не схватил)
        for sb in due:
            sb.status = 'running'
        await session.commit()
        jobs = [(sb.id, sb.preset_id, sb.audience, sb.caption or '') for sb in due]

    for sb_id, preset_id, audience, caption in jobs:
        try:
            await _do_preset_broadcast(preset_id, audience, caption)
            new_status, err = 'sent', None
        except Exception as exc:
            new_status, err = 'failed', str(exc)[:300]
            logger.warning('scheduled broadcast failed', sb_id=sb_id, error=err)
        async with AsyncSessionFactory() as session:
            sb = await session.get(ScheduledBroadcast, sb_id)
            if sb:
                sb.status = new_status
                sb.error = err
                await session.commit()


# 0-gen онбординг. Отправляется С КАРТИНКОЙ (welcome-коллаж результатов) —
# «👆» в текстах указывает на неё. Для фото-продукта показать вау важнее, чем
# рассказать. Барьеры, которые снимаем: (1) не видел результат → показываем
# коллаж; (2) страшно слать лицо боту → строка про приватность; (3) паралич
# выбора → «листай свободно, селфи попрошу потом».
_ZERO_GEN_MSGS: list[str] = [
    "🪄 <b>Вот что Нейра делает из обычного селфи 👆</b>\n\n"
    "У тебя уже <b>500 кр</b> — хватит на 2-3 такие сцены.\n"
    "Нужно одно селфи. Минута до первого результата.",

    "📸 <b>Одно селфи — и ты в сцене с обложки 👆</b>\n\n"
    "Фото идёт только в генерацию, нигде не публикуется и не хранится — "
    "стереть всё можно командой /reset.\n"
    "Твои <b>500 кр</b> ждут — попробуй первую сцену.",

    "🤔 <b>Не уверен с чего начать? Глянь примеры 👆</b>\n\n"
    "Жми кнопку — листай свободно. Селфи попрошу только когда выберешь "
    "конкретную сцену. Без обязательств, первая — за счёт подарка.",

    "🎁 <b>Подарок дня: один пресет бесплатно</b> (без списания) 👆\n\n"
    "Плюс <b>500 кр</b> в запасе. Одно селфи — и заберёшь сегодняшнюю сцену, "
    "потом распробуешь остальные.",
]


async def send_daily_push(bot_instance) -> None:
    """Отправить push пользователям с умной сегментацией:
    - 0 lifetime генераций → onboard-push (фокус «попробуй за минуту»)
    - 7-14 дней молчал → win-back
    - 2-7 дней молчал → re-engagement
    - 6-48ч молчал → день-недели ротация ИЛИ low-credit-monetization"""
    from sqlalchemy import select, and_
    from app.database.models import User, BotEvent

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_recent = now - timedelta(hours=6)
    cutoff_warm = now - timedelta(hours=48)
    cutoff_cold = now - timedelta(days=7)
    cutoff_max = now - timedelta(days=14)

    async with AsyncSessionFactory() as session:
        from app.database.models import Subscription
        stmt = select(User).where(and_(
            User.last_active_at >= cutoff_max,
            User.last_active_at <= cutoff_recent,
            User.is_banned == False,
        )).limit(5000)
        users = (await session.execute(stmt)).scalars().all()
        # Pre-compute set юзеров с хотя бы одной генерацией — для O(1) проверки
        # в цикле (иначе N queries для каждого).
        edited_uids = set((await session.execute(
            select(BotEvent.user_id).where(
                BotEvent.event_type == 'image_edited'
            ).distinct()
        )).scalars().all())
        # Кол-во сгенерированных фото за 7 дней — для smart upsell
        # (юзер активно тратит → предлагаем подписку как «выгоднее»).
        from sqlalchemy import func as _func
        edits_last7 = dict((await session.execute(
            select(BotEvent.user_id, _func.count())
            .where(BotEvent.event_type == 'image_edited',
                   BotEvent.created_at >= now - timedelta(days=7))
            .group_by(BotEvent.user_id)
        )).all())
        # Активные подписчики — этих не дёргаем upsell'ом
        subscribed_uids = set((await session.execute(
            select(Subscription.user_id).where(Subscription.is_active == True)
        )).scalars().all())

    day_text, day_kb = await build_day_push()

    # Коллаж результатов для 0-gen онбординга — тянем ОДИН раз (file_id reuse).
    # Показать вау для фото-продукта важнее текста. Если не достали — fallback
    # на текстовый пуш (photo=None ниже).
    try:
        from app.services.welcome_collage import get_or_build_welcome_collage_fid
        onboard_collage_fid = await get_or_build_welcome_collage_fid(bot_instance)
    except Exception as exc:
        logger.warning('daily_push: collage fetch failed', error=str(exc)[:120])
        onboard_collage_fid = None

    # Пул превью сцен для монетизационных пушей (подписка/покупка). Каждому
    # юзеру шлём СЛУЧАЙНУЮ сцену — свежая картинка напоминает «вот что можно
    # сделать» и повышает шанс зацепить (а не один и тот же кадр всем). Тянем
    # один раз, дальше random.choice в цикле (file_id reuse, без затрат).
    try:
        from app.database.repo.preset_previews import get_preview_pool
        async with AsyncSessionFactory() as session:
            sub_image_pool = await get_preview_pool(session, limit=60)
    except Exception as exc:
        logger.warning('daily_push: preview pool fetch failed', error=str(exc)[:120])
        sub_image_pool = []

    sent = failed = 0
    for user in users:
        last = user.last_active_at
        if last and last.tzinfo is not None:
            last = last.replace(tzinfo=None)

        photo = None  # если задан — шлём send_photo с text как caption

        # ПРИОРИТЕТ: 0-gen onboard перекрывает все остальные сегменты для тех,
        # кто пришёл, активировал промо, но ещё не сделал ни одной генерации.
        # Это самая болевая когорта — 80% базы (см. аналитику 23.05).
        if user.id not in edited_uids:
            text = random.choice(_ZERO_GEN_MSGS)
            kb = _OPEN_BOT_KB
            photo = onboard_collage_fid  # показываем коллаж результатов
        elif last and last <= cutoff_cold:
            # Win-back: молчал 7-14 дней. С коллажем — показать что нового.
            text = random.choice(_WINBACK_MSGS)
            kb = _OPEN_BOT_KB
            photo = onboard_collage_fid
        elif last and last <= cutoff_warm:
            # Re-engagement: молчал 2-7 дней. Тоже визуал.
            text = random.choice(_REENGAGE_MSGS)
            kb = _OPEN_BOT_KB
            photo = onboard_collage_fid
        else:
            # Warming: молчал 6ч-48ч.
            # Smart upsell приоритеты:
            # 1. heavy_user без подписки (3+ генераций/7д) → «купи подписку,
            #    у тебя ARPU и так выше 149₽» — самый высокий конверт-сегмент
            # 2. кредитов <5 → «купи пак или подписку» (urgent)
            # 3. иначе — день-недели пуш
            n_edits = edits_last7.get(user.id, 0)
            is_sub = user.id in subscribed_uids
            if n_edits >= 3 and not is_sub:
                # 3+ генераций за неделю = ARPU >= ~600 кр > подписка СТАРТ
                text = (
                    f"💎 <b>Ты сделал {n_edits} фото за неделю.</b>\n\n"
                    "На обычных кредитах это дороже подписки.\n\n"
                    "<b>СТАРТ · 149₽/мес</b>\n"
                    "• <b>1 000 кр</b> сразу = ~5 топ-сцен\n"
                    "• <b>+30 кр каждый день</b> = ещё ~5 за месяц\n"
                    "• Вся коллекция · без водяного знака\n\n"
                    "Окупится после 3-4 сцен."
                )
                kb = _SUB_PUSH_KB
            elif user.credits < 5:
                # Кредиты почти кончились — монетизационный пуш
                text = (
                    "⚡ <b>Кредиты на нуле — а сцена ждёт.</b>\n\n"
                    f"Осталось: <b>{user.credits} кр</b>\n\n"
                    "🔥 <b>1 500 кредитов — 99₽</b> (×3 к обычному паку, "
                    "хватит на 7 топ-сцен).\n"
                    "Или подписка от 149₽ — выгоднее вдолгую."
                )
                kb = _BUY_PUSH_KB
            else:
                # Обычный день-недели пуш
                text = day_text
                kb = day_kb

            # Монетизационные пуши (подписка/покупка) шлём с РАЗНОЙ сценой
            # каждый раз — свежий кадр-напоминание «вот что можно сделать»
            # цепляет лучше голого текста. Покрывает heavy-upsell, low-credit
            # и пятничный sub-day-push (он тоже на _SUB_PUSH_KB).
            if (kb is _SUB_PUSH_KB or kb is _BUY_PUSH_KB) and sub_image_pool:
                photo = random.choice(sub_image_pool)

        try:
            if photo:
                await bot_instance.send_photo(
                    user.telegram_id, photo=photo, caption=text,
                    parse_mode='HTML', reply_markup=kb,
                )
            else:
                await bot_instance.send_message(
                    user.telegram_id, text, parse_mode='HTML', reply_markup=kb,
                )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    logger.info('Daily push complete', sent=sent, failed=failed, total=len(users))


async def send_mystery_push(bot_instance) -> None:
    """Дневной push (12:00 МСК) о Подарке дня — главный retention-крючок.

    Шлём только тем кто:
      • Активен последние 14 дней (не cold)
      • Сделал хотя бы 1 image_edited (понимает что бот делает — иначе пуш
        бессмыслен)
      • Не забирал сегодняшний Mystery (Redis-проверка)
      • Не заблокирован
      • Не подписчик с активной подпиской (им халява не нужна как retention)

    Этот push НЕ дублирует daily_push (10:00) — у них разная цель и аудитория:
    daily_push сегментирует по тишине, mystery_push зовёт активных за подарком.
    """
    from sqlalchemy import select, and_
    from app.database.models import User, BotEvent
    from app.services.mystery_preset import get_today_mystery, is_used_today
    import re

    mystery = await get_today_mystery()
    if not mystery:
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=14)

    async with AsyncSessionFactory() as session:
        # Активные за 14д с хотя бы одной генерацией
        edited_uids = set((await session.execute(
            select(BotEvent.user_id).where(
                BotEvent.event_type == 'image_edited'
            ).distinct()
        )).scalars().all())
        stmt = select(User).where(and_(
            User.last_active_at >= cutoff,
            User.is_banned == False,
            User.id.in_(edited_uids) if edited_uids else False,
        )).limit(5000)
        users = (await session.execute(stmt)).scalars().all()

    clean_label = re.sub(r'^\W+', '', mystery['label']).strip()
    text = (
        f'🎁 <b>Подарок дня — бесплатно, без списания кредитов</b>\n\n'
        f'Сегодня дарю сцену «{clean_label}» 👆 Пришли селфи — и ты в ней за '
        f'минуту, лицо <b>в точности твоё</b>.\n\n'
        f'⏳ Только сегодня, до полуночи МСК. Завтра будет другая.'
    )
    # Превью самой сцены — показываем ЧТО дарим (визуал > текста для фото-бота).
    from app.database.repo.preset_previews import get_preview_file_id
    async with AsyncSessionFactory() as session:
        gift_fid = await get_preview_file_id(session, mystery['preset_key'], position=0)

    sent = skipped = failed = 0
    for user in users:
        # Skip если уже забрал сегодня
        if await is_used_today(user.telegram_id):
            skipped += 1
            continue
        try:
            if gift_fid:
                await bot_instance.send_photo(
                    user.telegram_id, photo=gift_fid, caption=text,
                    parse_mode='HTML', reply_markup=_OPEN_BOT_KB,
                )
            else:
                await bot_instance.send_message(
                    user.telegram_id, text, parse_mode='HTML', reply_markup=_OPEN_BOT_KB,
                )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    logger.info('Mystery push complete', preset=mystery['preset_key'],
                sent=sent, skipped=skipped, failed=failed, total=len(users))


async def send_subscription_expiring_notifications(bot_instance) -> None:
    """Уведомить юзеров чьи подписки истекают через 1-3 дня."""
    from sqlalchemy import select, and_
    from app.database.models import Plan, Subscription, User
    from app.database.repo.credits import SUBSCRIPTION_PLANS

    # 24.05: было utcnow() (naive) — при сравнении с aware Subscription.expires_at
    # PostgreSQL мог отдавать криво или Python кидать TypeError. Все остальные
    # cron-задачи здесь используют aware datetime, выравниваем.
    now = datetime.now(timezone.utc)
    in_3_days = now + timedelta(days=3)

    async with AsyncSessionFactory() as session:
        stmt = select(Subscription, User).join(User, Subscription.user_id == User.id).where(and_(
            Subscription.is_active == True,
            Subscription.expires_at > now,
            Subscription.expires_at <= in_3_days,
            Subscription.plan != Plan.FREE,
        ))
        result = await session.execute(stmt)
        rows = result.all()

    sent = 0
    for sub, user in rows:
        # Нормализуем expires_at для расчёта (могут быть aware/naive).
        # С 24.05 now всегда aware → если expires_at naive, делаем его aware.
        sub_expires = sub.expires_at
        if sub_expires.tzinfo is None:
            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
        delta_days = (sub_expires - now).days + 1
        days_left = max(1, delta_days)

        plan_val = sub.plan.value if hasattr(sub.plan, 'value') else str(sub.plan)
        plan_dict = next((p for p in SUBSCRIPTION_PLANS if p['plan_enum'] == plan_val), None)
        if not plan_dict:
            continue

        if days_left == 1:
            day_word = 'день'
        elif days_left in (2, 3, 4):
            day_word = 'дня'
        else:
            day_word = 'дней'

        try:
            await bot_instance.send_message(
                user.telegram_id,
                f"⏰ <b>Подписка {plan_dict['name']} истекает через {days_left} {day_word}</b>\n\n"
                f"Продли сейчас за <b>{plan_dict['rub']}₽</b>, чтобы не потерять доступ.",
                parse_mode='HTML',
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    logger.info('Sub expiry notifications sent', count=sent, total=len(rows))


async def send_streak_reminders(bot_instance) -> None:
    """Вечером (20:00 МСК): напоминаем тем, у кого серия ≥2 дней и кто СЕГОДНЯ
    ещё не заходил — иначе завтра серия обнулится. Только текст (fal не тратит).
    Loss aversion: «не потеряй накопленное». Один заход — серия спасена."""
    from sqlalchemy import select, and_
    from app.database.models import User
    from app.database.repo.credits import next_streak_milestone
    from app.utils import premium_text, yesterday_msk

    yesterday = yesterday_msk()  # МСК — иначе ночью считает не тех
    sent = 0
    async with AsyncSessionFactory() as session:
        stmt = select(User).where(and_(
            User.streak_last_date == yesterday,   # последний заход — вчера (не сегодня)
            User.streak_days >= 2,                # есть что терять
            User.is_banned == False,              # noqa: E712
            User.id.notin_([1, 2]),               # без админов
        ))
        users = (await session.execute(stmt)).scalars().all()

    for user in users:
        days = user.streak_days
        nm = next_streak_milestone(days)
        tail = ''
        if nm:
            d, amt = nm
            left = d - days
            if left <= 2:
                tail = f'\n\n🎁 До награды <b>+{amt} кр</b> осталось всего {left} дн — не останавливайся!'
        try:
            await bot_instance.send_message(
                user.telegram_id,
                premium_text(
                    f'🔥 <b>Твоя серия — {days} дней подряд.</b>\n\n'
                    f'Загляни сегодня, чтобы не обнулить её! Достаточно открыть '
                    f'Нейру — и серия в безопасности.{tail}'
                ),
                parse_mode='HTML',
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    logger.info('Streak reminders sent', count=sent, candidates=len(users))


async def grant_time_bonuses(bot_instance) -> None:
    """Каждый час: выдать +200 кредитов тем, кто отправил хотя бы 1 сообщение в первые 24ч после активации промокода."""
    from sqlalchemy import select, and_
    from app.database.models import User, BotEvent

    TIME_BONUS = 200
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=24)

    async with AsyncSessionFactory() as session:
        # Найти кандидатов: promo_activated_at установлен, бонус ещё не выдан, 24ч прошло
        stmt = select(User).where(and_(
            User.promo_activated_at.is_not(None),
            User.time_bonus_granted == False,
            User.promo_activated_at <= cutoff,
        ))
        result = await session.execute(stmt)
        candidates = result.scalars().all()

        for user in candidates:
            activated_at = user.promo_activated_at
            if activated_at and activated_at.tzinfo is not None:
                activated_at = activated_at.replace(tzinfo=None)

            # Проверяем, было ли хотя бы 1 сообщение после активации
            msg_stmt = select(BotEvent).where(and_(
                BotEvent.user_id == user.id,
                BotEvent.event_type == 'message_sent',
                BotEvent.created_at > activated_at,
            )).limit(1)
            msg_result = await session.execute(msg_stmt)
            has_message = msg_result.scalars().first() is not None

            if not has_message:
                # Не активничал — просто помечаем флаг чтобы не проверять снова
                user.time_bonus_granted = True
                continue

            # Через add_credits — корректно очистит burn_pool, согласован путь.
            await add_credits(session, user, TIME_BONUS)
            user.time_bonus_granted = True

            try:
                from app.utils import premium_text
                await bot_instance.send_message(
                    user.telegram_id,
                    premium_text(
                        '🎉 <b>+200 кредитов — бонус за активность!</b>\n\n'
                        'Ты использовал бот в первые сутки после получения подарка. '
                        'Держи ещё 200 кредитов сверху — на будущие вопросы.'
                    ),
                    parse_mode='HTML',
                )
            except Exception as exc:
                logger.warning('time_bonus notify failed', user_id=user.telegram_id, error=str(exc)[:200])

        await session.commit()

    logger.info('Time bonus check complete', candidates=len(candidates))


async def check_error_spike() -> None:
    """Send admin alert if >5 errors in last 15 minutes. Throttled to once per hour."""
    from sqlalchemy import func, select
    from app.database.models import BotEvent
    from app.services.admin_notify import alert_error_spike

    window_min = 15
    threshold = 5
    since = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    try:
        async with AsyncSessionFactory() as session:
            count = await session.scalar(
                select(func.count()).select_from(BotEvent)
                .where(BotEvent.event_type == 'error', BotEvent.created_at >= since)
            )
        if (count or 0) >= threshold:
            # Throttle via a global config flag (key: 'last_error_alert_at')
            from app.database.repo.bot_config import get_config_value, set_config_value
            async with AsyncSessionFactory() as session:
                last_str = await get_config_value(session, 'last_error_alert_at') or '0'
                last_ts = int(last_str) if last_str.isdigit() else 0
                now_ts = int(datetime.now(timezone.utc).timestamp())
                if now_ts - last_ts >= 3600:  # 1h between alerts
                    await alert_error_spike('error', int(count), window_min)
                    await set_config_value(session, 'last_error_alert_at', str(now_ts))
                    await session.commit()
    except Exception as exc:
        logger.warning('error spike check failed', error=str(exc)[:200])


def start_daily_push_scheduler(bot_instance):
    """Запускает APScheduler джоб для daily push в 10:00 МСК."""
    global _daily_push_scheduler
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from app.services.channel_publisher import run_scheduled_post, run_scheduled_feature_post, publish_pending_channel_post

    _daily_push_scheduler = AsyncIOScheduler(timezone=settings.tz)
    _daily_push_scheduler.add_job(
        send_daily_push,
        trigger=CronTrigger(hour=10, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='daily_push',
        replace_existing=True,
    )
    # Mystery Push (12:00 МСК) — главный retention-крючок. Зовёт активных
    # юзеров за «Подарок дня» — бесплатный пресет в главном меню.
    # 30.05: ежедневный пуш убран → 3×/нед (Вт/Чт/Сб). Данные: аудитория 21 чел,
    # claim'ов за всё время 1 → ежедневный пуш = усталость/блоки ради ~нуля.
    # Реже = «подарок» снова событие, меньше риск блока. Сам Подарок дня остаётся
    # доступным каждый день в меню — просто не пушим его ежедневно.
    _daily_push_scheduler.add_job(
        send_mystery_push,
        trigger=CronTrigger(day_of_week='tue,thu,sat', hour=12, minute=0,
                            timezone=settings.tz),
        args=[bot_instance],
        id='mystery_push',
        replace_existing=True,
    )
    _daily_push_scheduler.add_job(
        send_subscription_expiring_notifications,
        trigger=CronTrigger(hour=11, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='sub_expiring',
        replace_existing=True,
    )
    _daily_push_scheduler.add_job(
        grant_time_bonuses,
        trigger=CronTrigger(minute=0),  # каждый час
        args=[bot_instance],
        id='time_bonus',
        replace_existing=True,
    )
    # Channel auto-publisher: вечерний daily-пост Пн/Вт/Чт/Пт/Сб в 19:03–19:17 МСК.
    # Среда и Воскресенье ИСКЛЮЧЕНЫ — там обеденный feature-пост (см. ниже).
    # Получаем 1 пост в день, никаких двойных публикаций.
    # Явно фиксируем timezone — иначе scheduler возьмёт UTC (=22 МСК).
    _channel_publish_minute = random.randint(3, 17)
    _daily_push_scheduler.add_job(
        run_scheduled_post,
        trigger=CronTrigger(
            day_of_week='mon,tue,thu,fri,sat',  # без ср/вс — там feature
            hour=19,
            minute=_channel_publish_minute,
            timezone=settings.tz,
        ),
        args=[bot_instance],
        id='channel_auto_publish',
        replace_existing=True,
    )
    # Feature posts (Нейра-focused): Ср + Вс в 12:25–12:35 МСК (lunch slot).
    # Trackable CTA с deep-link на /start promo_CHAN_FEAT.
    _feature_minute = random.randint(25, 35)
    _daily_push_scheduler.add_job(
        run_scheduled_feature_post,
        trigger=CronTrigger(
            day_of_week='wed,sun',
            hour=12,
            minute=_feature_minute,
            timezone=settings.tz,
        ),
        args=[bot_instance],
        id='channel_feature_publish',
        replace_existing=True,
    )
    _daily_push_scheduler.add_job(
        check_error_spike,
        trigger=CronTrigger(minute='*/15'),  # every 15 min
        id='error_spike_check',
        replace_existing=True,
    )
    # Диспатчер отложенных пресет-рассылок (см. ScheduledBroadcast). Каждую
    # минуту проверяет pending due → зовёт _do_preset_broadcast.
    _daily_push_scheduler.add_job(
        dispatch_scheduled_broadcasts,
        trigger=CronTrigger(minute='*'),
        id='scheduled_broadcasts_dispatcher',
        replace_existing=True,
    )
    # Отложенный ручной пост в канал (showcase и т.п.): каждую минуту проверяем
    # bot_config['pending_channel_post'] и публикуем когда наступит publish_at.
    _daily_push_scheduler.add_job(
        publish_pending_channel_post,
        trigger=CronTrigger(minute='*'),
        args=[bot_instance],
        id='pending_channel_post',
        replace_existing=True,
    )
    # Колесо удачи: каждые 30 мин шлём push «колесо готово» юзерам с
    # истёкшим cooldown'ом (ZSET wheel:ready_at). Ночью (23-09 МСК) тихо.
    from app.handlers.wheel import notify_wheel_ready
    _daily_push_scheduler.add_job(
        notify_wheel_ready,
        trigger=CronTrigger(minute='*/30'),
        args=[bot_instance],
        id='wheel_ready_notifier',
        replace_existing=True,
    )
    # Phase 2: payment recovery every 30 min
    from app.services.payment_recovery import run_payment_recovery
    _daily_push_scheduler.add_job(
        run_payment_recovery,
        trigger=CronTrigger(minute='*/30'),
        args=[bot_instance],
        id='payment_recovery',
        replace_existing=True,
    )
    # Fal.ai отчёт админу каждый день в 9:00 МСК — утренняя сводка
    # перед началом дня (за прошлые сутки).
    _daily_push_scheduler.add_job(
        _fal_daily_report,
        trigger=CronTrigger(hour=9, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='fal_daily_report',
        replace_existing=True,
    )
    # Fal.ai low-balance check каждый час — анти-спам через should_alert_admin()
    _daily_push_scheduler.add_job(
        _fal_low_balance_check,
        trigger=CronTrigger(minute=5),  # каждый час в hh:05
        args=[bot_instance],
        id='fal_low_balance_check',
        replace_existing=True,
    )
    # Phase 2: trial-end conversion push, hourly at minute 30
    from app.services.trial_conversion import run_trial_end_push
    _daily_push_scheduler.add_job(
        run_trial_end_push,
        trigger=CronTrigger(minute=30),
        args=[bot_instance],
        id='trial_end_push',
        replace_existing=True,
    )
    # Phase 3: YooKassa subscription auto-renewal — daily at 03:00 MSK
    # (тихое время, чтобы не наспамить юзера ночью pre-charge уведомлениями).
    from app.services.subscription_renewal import run_subscription_renewals
    _daily_push_scheduler.add_job(
        run_subscription_renewals,
        trigger=CronTrigger(hour=3, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='subscription_renewals',
        replace_existing=True,
    )
    # Phase 4: burn-credit expiry — ежедневно в 04:00 МСК. Обнуляет сгоревшие
    # burn-pool и отправляет напоминание за 2 дня до сгорания.
    from app.services.burn_expiry import run_burn_expiry
    _daily_push_scheduler.add_job(
        run_burn_expiry,
        trigger=CronTrigger(hour=4, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='burn_expiry',
        replace_existing=True,
    )
    # Тихий утренний грант ежедневного бонуса платным подписчикам в 09:00 МСК —
    # выполняет промис «+N кр ежедневно 30 дней» даже в дни простоя. Без сообщений.
    _daily_push_scheduler.add_job(
        grant_daily_to_subscribers,
        trigger=CronTrigger(hour=9, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='daily_subscriber_grant',
        replace_existing=True,
    )
    # Напоминание админу про gamification-план — каждый день в 18:00 МСК.
    _daily_push_scheduler.add_job(
        send_gamification_reminder,
        trigger=CronTrigger(hour=18, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='gamification_reminder',
        replace_existing=True,
    )
    # Вечернее напоминание «не потеряй серию» — 20:00 МСК (вечер, не ночь).
    _daily_push_scheduler.add_job(
        send_streak_reminders,
        trigger=CronTrigger(hour=20, minute=0, timezone=settings.tz),
        args=[bot_instance],
        id='streak_reminders',
        replace_existing=True,
    )
    # Автосинк реального fal.ai баланса с API → baseline. Каждый час в hh:50.
    # Источник истины — официальный billing endpoint, не локальный трекер
    # (который не видит админ-скриптовых генераций).
    from app.services.fal_balance import sync_baseline_from_real
    _daily_push_scheduler.add_job(
        sync_baseline_from_real,
        trigger=CronTrigger(minute=50),  # каждый час в hh:50
        id='fal_balance_autosync',
        replace_existing=True,
    )
    _daily_push_scheduler.start()
    logger.info(
        'Daily push scheduler started',
        schedule='10:00 MSK daily / 11:00 MSK sub-expiry / hourly time-bonus',
        channel_publish_minute=_channel_publish_minute,
    )
    return _daily_push_scheduler


def stop_daily_push_scheduler() -> None:
    global _daily_push_scheduler
    if _daily_push_scheduler:
        _daily_push_scheduler.shutdown(wait=False)
        logger.info('Daily push scheduler stopped')


def is_admin(message: Message) -> bool:
    return message.from_user.id in settings.admin_ids_list


@router.message(Command('analytics'))
async def cmd_analytics(message: Message) -> None:
    if not is_admin(message):
        return

    from sqlalchemy import select, func, and_, distinct
    from app.database.models import User, Payment, PaymentStatus, Subscription

    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async with AsyncSessionFactory() as session:
        total_users = await session.scalar(select(func.count(User.id))) or 0
        new_today = await session.scalar(
            select(func.count(User.id)).where(User.created_at >= day_ago)
        ) or 0
        new_week = await session.scalar(
            select(func.count(User.id)).where(User.created_at >= week_ago)
        ) or 0
        dau = await session.scalar(
            select(func.count(User.id)).where(User.last_active_at >= day_ago)
        ) or 0
        wau = await session.scalar(
            select(func.count(User.id)).where(User.last_active_at >= week_ago)
        ) or 0
        mau = await session.scalar(
            select(func.count(User.id)).where(User.last_active_at >= month_ago)
        ) or 0

        revenue_today = await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(and_(
                Payment.status == PaymentStatus.SUCCEEDED,
                Payment.created_at >= day_ago,
            ))
        ) or 0
        revenue_week = await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(and_(
                Payment.status == PaymentStatus.SUCCEEDED,
                Payment.created_at >= week_ago,
            ))
        ) or 0
        revenue_month = await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(and_(
                Payment.status == PaymentStatus.SUCCEEDED,
                Payment.created_at >= month_ago,
            ))
        ) or 0
        revenue_total = await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.SUCCEEDED,
            )
        ) or 0

        paying_users = await session.scalar(
            select(func.count(distinct(Payment.user_id))).where(
                Payment.status == PaymentStatus.SUCCEEDED,
            )
        ) or 0

        active_subs = await session.scalar(
            select(func.count(Subscription.id)).where(and_(
                Subscription.is_active == True,
                Subscription.expires_at > now,
            ))
        ) or 0

    conv_pct = (paying_users / total_users * 100) if total_users else 0
    avg_revenue = (float(revenue_total) / paying_users) if paying_users else 0

    text = (
        f"📊 <b>Аналитика Нейры</b>\n\n"
        f"<b>Юзеры:</b>\n"
        f"• Всего: {total_users}\n"
        f"• Новых сегодня: {new_today}\n"
        f"• Новых за 7д: {new_week}\n\n"
        f"<b>Активность:</b>\n"
        f"• DAU (день): {dau}\n"
        f"• WAU (неделя): {wau}\n"
        f"• MAU (месяц): {mau}\n\n"
        f"<b>Выручка:</b>\n"
        f"• Сегодня: {float(revenue_today):,.0f} ₽\n"
        f"• За 7д: {float(revenue_week):,.0f} ₽\n"
        f"• За 30д: {float(revenue_month):,.0f} ₽\n"
        f"• Всего: {float(revenue_total):,.0f} ₽\n\n"
        f"<b>Конверсия:</b>\n"
        f"• Платящих: {paying_users} ({conv_pct:.1f}%)\n"
        f"• Средний чек: {avg_revenue:,.0f} ₽\n"
        f"• Активных подписок: {active_subs}"
    )

    await message.answer(text, parse_mode='HTML')


def _format_fal_report(stats: dict) -> str:
    """Общий форматтер отчёта — используется и в /falreport, и в daily-push."""
    bal = stats['balance']
    bal_str = f'${bal:.2f}' if bal is not None else 'не задан (/falbalance)'
    spent_today = stats['spent_today']
    spent_yest = stats['spent_yesterday']
    counts = stats['counts']
    days_left = stats['days_left']
    days_left_str = (f'~{days_left:.1f} дн.' if days_left and days_left < 999 else '∞')
    trend = ''
    if spent_yest > 0:
        diff = spent_today - spent_yest
        sign = '+' if diff >= 0 else ''
        trend = f' ({sign}${diff:.3f} vs вчера)'
    warning = ''
    if bal is not None and bal < 0.30:
        warning = '\n⚠️ <b>Низкий баланс — пополни fal.ai</b>\n'
    return (
        f'📊 <b>fal.ai отчёт</b>{warning}\n'
        f'💰 Баланс: <b>{bal_str}</b>\n'
        f'📅 За сутки: <b>${spent_today:.3f}</b>{trend}\n'
        f'⏱ При текущем темпе хватит: <b>{days_left_str}</b>\n\n'
        f'<b>Операции за сутки:</b>\n'
        f'· quality: {counts["quality"]}\n'
        f'· precise: {counts["precise"]}\n'
        f'· face_swap: {counts["face_swap"]}\n'
        f'· gen (channel): {counts["gen"]}\n\n'
        f'<i>Пороги: precise блок ниже $0.20, всё fal ниже $0.05</i>'
    )


async def _fal_daily_report(bot_instance) -> None:
    """Scheduled-job: каждый день в 23:00 МСК шлёт админам сводку."""
    from app.services import fal_balance
    stats = await fal_balance.get_daily_stats()
    text = _format_fal_report(stats)
    for admin_id in settings.admin_ids_list:
        try:
            await bot_instance.send_message(admin_id, text, parse_mode='HTML',
                                            disable_notification=True)
        except Exception as exc:
            logger.warning('fal daily report send failed',
                          admin_id=admin_id, error=str(exc)[:200])


async def _fal_low_balance_check(bot_instance) -> None:
    """Scheduled-job: каждый час проверяет — не упал ли баланс ниже alert-порога.
    Если упал и за 24ч не алертил — шлёт админу. Защита от спама внутри
    fal_balance.should_alert_admin().
    """
    from app.services import fal_balance
    if not await fal_balance.should_alert_admin():
        return
    bal = await fal_balance.get_balance()
    if bal is None:
        return
    text = (
        f'⚠️ <b>fal.ai баланс низкий: ${bal:.2f}</b>\n\n'
        f'Скоро отключатся precise (порог $0.20) и весь fal (порог $0.05).\n'
        f'Пополни кабинет: https://fal.ai/dashboard\n\n'
        f'После пополнения скажи мне: <code>/falbalance N</code> (где N — новая сумма).'
    )
    for admin_id in settings.admin_ids_list:
        try:
            await bot_instance.send_message(admin_id, text, parse_mode='HTML')
        except Exception as exc:
            logger.warning('fal low balance alert failed',
                          admin_id=admin_id, error=str(exc)[:200])
    await fal_balance.mark_alert_sent()


async def grant_daily_to_subscribers(bot_instance=None) -> None:
    """Утренний (09:00 МСК) тихий грант ежедневного бонуса всем активным платным
    подписчикам — чтобы промис «+N кр ежедневно 30 дней» выполнялся даже в дни,
    когда юзер не заходит в бота. БЕЗ сообщений (просто пополняет баланс).

    Free/channel-юзеры сюда НЕ входят — у них login-грант при заходе в /start
    (не раздуваем балансы мёртвых аккаунтов). Идемпотентно: grant_daily_if_needed
    пропустит тех, кому уже начислено сегодня (например, активным — через фото).
    """
    from sqlalchemy import select, and_
    from app.database.models import User, Subscription, Plan
    from app.database.repo.credits import grant_daily_if_needed

    now = datetime.now(timezone.utc)
    granted_users = 0
    granted_credits = 0
    async with AsyncSessionFactory() as session:
        stmt = (
            select(User)
            .join(Subscription, Subscription.user_id == User.id)
            .where(and_(
                Subscription.is_active.is_(True),
                Subscription.expires_at > now,
                Subscription.plan.in_([Plan.BASIC, Plan.PRO, Plan.MAX]),
            ))
        )
        users = (await session.execute(stmt)).scalars().all()
        for user in users:
            amt = await grant_daily_if_needed(session, user)
            if amt:
                granted_users += 1
                granted_credits += amt
        await session.commit()
    logger.info('daily subscriber grant complete',
                active_subs=len(users), granted_users=granted_users,
                granted_credits=granted_credits)


async def send_gamification_reminder(bot_instance) -> None:
    """18:00 МСК: напоминание админу про gamification-план Нейры + ЖИВОЙ баланс
    fal.ai (преимущество локального джоба над remote-routine — есть доступ к prod).
    Раньше это был remote-routine, чей ответ уходил в облачную сессию и до юзера
    не доходил. Теперь — прямое сообщение в Telegram."""
    from app.services import fal_balance
    try:
        bal = await fal_balance.get_balance()
    except Exception:
        bal = None

    if bal is None:
        bal_line = "💵 fal.ai: baseline не задан (<code>/falbalance N</code>)"
        rec = "Сначала задай baseline, чтобы видеть, когда можно стартовать."
    elif bal >= 30:
        bal_line = f"💵 fal.ai: <b>${bal:.2f}</b> ✅"
        rec = ("Порог достигнут — можно запускать <b>Этап 1: Streak Escalation</b> "
               "(поля БД готовы, безопаснее всего).")
    else:
        bal_line = f"💵 fal.ai: <b>${bal:.2f}</b>"
        rec = f"Для старта Этапа 1 нужно ≥ $30. Не хватает <b>${30 - bal:.2f}</b> — пока ждём."

    text = (
        "🎰 <b>Напоминание: gamification-план Нейры</b>\n\n"
        f"{bal_line}\n\n"
        "Этапы (от дешёвого к дорогому):\n"
        "1. 🔥 <b>Streak Escalation</b> — растущий бонус за серию дней (~1 день работы)\n"
        "2. 🎡 <b>Daily Wheel</b> — колесо призов раз в день (~3 дня, главный retention-hook)\n"
        "3. 🎲 <b>Mystery Preset</b> — пресет дня −50% (~2 дня)\n\n"
        f"👉 {rec}"
    )
    for admin_id in settings.admin_ids_list:
        try:
            await bot_instance.send_message(admin_id, text, parse_mode='HTML',
                                            disable_web_page_preview=True)
        except Exception as exc:
            logger.warning('gamification reminder send failed',
                          admin_id=admin_id, error=str(exc)[:200])


@router.message(Command('falbalance'))
async def cmd_fal_set_balance(message: Message) -> None:
    """Админ устанавливает текущий баланс fal.ai после пополнения.
    Использование: /falbalance 30   (после пополнения на $30)
    Это baseline для расчёта — bot будет считать расход с этого момента.
    """
    if not is_admin(message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2:
        await message.answer(
            '<b>Использование:</b>\n'
            '<code>/falbalance 30</code> — указать что сейчас на fal.ai $30\n\n'
            'Бот будет считать расход с этого момента и блокировать precise/face_swap '
            'когда оценочный баланс упадёт ниже $0.20.',
            parse_mode='HTML',
        )
        return
    try:
        amount = float(parts[1])
        if amount < 0 or amount > 10000:
            raise ValueError('out of range')
    except ValueError:
        await message.answer('❌ Сумма должна быть числом, например <code>/falbalance 30</code>',
                             parse_mode='HTML')
        return
    from app.services import fal_balance
    await fal_balance.set_baseline(amount)
    await message.answer(
        f'✓ Baseline установлен: <b>${amount:.2f}</b>\n\n'
        f'С этого момента бот будет считать расход.\n'
        f'Команда <code>/falreport</code> покажет статистику.',
        parse_mode='HTML',
    )


@router.message(Command('neyra_status', 'status_neyra'))
async def cmd_neyra_status(message: Message) -> None:
    """Полный снимок проекта в личке админа: юзеры + платежи + fal + канал.
    Удобно с мобилы посмотреть «как дела» без захода на сервер.
    """
    if not is_admin(message):
        return
    from sqlalchemy import select, func, text
    from app.services import fal_balance
    from app.database.models import User, Payment, PaymentStatus, BotEvent, ChannelPost

    async with AsyncSessionFactory() as session:
        # Юзеры
        users_total = await session.scalar(
            select(func.count()).select_from(User).where(User.id.notin_([1, 2]))
        )
        users_7d = await session.scalar(
            select(func.count()).select_from(User).where(
                User.id.notin_([1, 2]),
                User.last_active_at > func.now() - text("INTERVAL '7 days'"),
            )
        )
        users_24h = await session.scalar(
            select(func.count()).select_from(User).where(
                User.id.notin_([1, 2]),
                User.last_active_at > func.now() - text("INTERVAL '24 hours'"),
            )
        )
        # Платежи (succeeded only)
        pay_stats = await session.execute(
            select(func.count(), func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.status == PaymentStatus.SUCCEEDED,
                   Payment.user_id.notin_([1, 2]))
        )
        pay_count, pay_total = pay_stats.one()
        # AI ops 24ч
        ai_24h = await session.scalar(
            select(func.count()).select_from(BotEvent).where(
                BotEvent.event_type == 'image_edited',
                BotEvent.user_id.notin_([1, 2]),
                BotEvent.created_at > func.now() - text("INTERVAL '24 hours'"),
            )
        )
        # Последний пост канала
        last_post = await session.execute(
            select(ChannelPost.topic, ChannelPost.cta_type, ChannelPost.published_at)
            .where(ChannelPost.status == 'published')
            .order_by(ChannelPost.published_at.desc()).limit(1)
        )
        last_post_row = last_post.first()

    # fal balance
    bal = await fal_balance.get_balance()
    fal_stats = await fal_balance.get_daily_stats()
    bal_str = (f'${bal:.2f}' if bal is not None else 'не задан (/falbalance)')
    days_left = fal_stats['days_left']
    days_str = (f'~{days_left:.0f} дн' if days_left and days_left < 999 else '∞')

    last_post_str = '—'
    if last_post_row:
        topic, cta, dt = last_post_row
        from datetime import timezone, timedelta
        msk = timezone(timedelta(hours=3))
        dt_str = dt.astimezone(msk).strftime('%d.%m %H:%M') if dt else '?'
        last_post_str = f'{dt_str} · {cta} · <i>{topic[:50]}</i>'

    text = (
        f'📊 <b>Нейра · снимок</b>\n\n'
        f'👥 <b>Юзеры:</b> {users_total} всего · {users_7d} за 7д · {users_24h} за 24ч\n'
        f'💰 <b>Платящих:</b> {pay_count} · собрано {pay_total}₽\n'
        f'🎨 <b>AI за 24ч:</b> {ai_24h} операций\n\n'
        f'💵 <b>fal.ai:</b> {bal_str} · хватит {days_str}\n'
        f'   за сутки: ${fal_stats["spent_today"]:.3f}\n\n'
        f'📢 <b>Последний пост канала:</b>\n{last_post_str}\n\n'
        f'<i>Команды: /falreport /falbalance /stats /admin</i>'
    )
    await message.answer(text, parse_mode='HTML')


@router.message(Command('falreport'))
async def cmd_fal_report(message: Message) -> None:
    """Текущий баланс fal.ai + расход за сутки."""
    if not is_admin(message):
        return
    from app.services import fal_balance
    stats = await fal_balance.get_daily_stats()
    await message.answer(_format_fal_report(stats), parse_mode='HTML')


@router.message(Command('stats'))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message):
        return

    from app.database.repo.promos import list_promos
    async with AsyncSessionFactory() as session:
        total_users = await get_total_users_count(session)
        promos = await list_promos(session)

    active_promos = sum(1 for p in promos if p.is_active)
    total_redemptions = sum(p.used_count for p in promos)

    text = (
        '📊 <b>Статистика</b>\n\n'
        f'👥 Всего пользователей: <b>{total_users}</b>\n\n'
        f'🎟 Активных промокодов: <b>{active_promos}</b>\n'
        f'✅ Всего активаций: <b>{total_redemptions}</b>'
    )
    await message.answer(text, parse_mode='HTML')


@router.message(Command('fixpayments'))
async def cmd_fix_payments(message: Message) -> None:
    """Завершить все висящие платежи YooKassa"""
    if not is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        pending = await get_pending_payments(session)
        yookassa_pending = [p for p in pending if p.payment_method == 'yookassa']

        if not yookassa_pending:
            await message.answer('✅ Нет висящих платежей YooKassa.')
            return

        completed = 0
        for p in yookassa_pending:
            credits = p.extra.get('credits', 0)
            user_tg_id = p.extra.get('user_tg_id')
            if credits and user_tg_id:
                user = await get_user(session, int(user_tg_id))
                if user:
                    await add_credits(session, user, credits)
                    await update_payment(session, p.id, PaymentStatus.SUCCEEDED, external_id='admin_fix')
                    completed += 1

                    # Отправить уведомление пользователю
                    try:
                        await message.bot.send_message(
                            chat_id=int(user_tg_id),
                            text=(
                                f'✅ <b>{credits:,} кредитов зачислено!</b>\n\n'
                                f'💳 Твой баланс: <b>{user.credits + credits:,} кредитов</b>\n\n'
                                f'Просто напиши мне — я готов помочь 🚀'
                            ),
                            parse_mode='HTML',
                        )
                    except Exception as e:
                        logger.error('Failed to send payment notification', user_id=user_tg_id, error=e)

        await session.commit()

    await message.answer(f'✅ Завершено {completed} платежей YooKassa, кредиты зачислены и уведомления отправлены.')


@router.message(Command('checkpayments'))
async def cmd_check_payments(message: Message) -> None:
    """Проверить статус платежей YooKassa через API"""
    if not is_admin(message):
        return

    await message.answer('🔄 Проверяю статус платежей YooKassa...')

    try:
        await check_yookassa_payments(message.bot)
        await message.answer('✅ Проверка платежей завершена.')
    except Exception as e:
        logger.error('Manual payment check failed', error=e)
        await message.answer(f'❌ Ошибка проверки платежей: {e}')


@router.message(Command('addpromo'))
async def cmd_addpromo(message: Message) -> None:
    if not is_admin(message):
        return

    parts = (message.text or '').removeprefix('/addpromo').strip().split()
    if len(parts) < 2:
        await message.answer(
            '❌ Формат:\n'
            '<code>/addpromo КОД КРЕДИТЫ [МАХ] [ДНЕЙ_ДО_ИСТЕЧЕНИЯ]</code>\n\n'
            'Примеры:\n'
            '<code>/addpromo WELCOME 200</code> — безлимит, бессрочно\n'
            '<code>/addpromo PROMO100 100 50</code> — 50 активаций\n'
            '<code>/addpromo LAUNCH 500 0 7</code> — истекает через 7 дней',
            parse_mode='HTML',
        )
        return

    code = parts[0].upper()
    try:
        credits = int(parts[1])
        max_uses = int(parts[2]) if len(parts) > 2 else 0
        expire_days = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        await message.answer('❌ Кредиты, макс. использований и дни должны быть числами.')
        return

    if credits <= 0:
        await message.answer('❌ Количество кредитов должно быть больше 0.')
        return

    expires_at = datetime.now(timezone.utc) + timedelta(days=expire_days) if expire_days > 0 else None

    from app.database.repo.promos import create_promo
    async with AsyncSessionFactory() as session:
        try:
            promo = await create_promo(session, code, credits, max_uses, expires_at)
            await session.commit()
        except Exception:
            await message.answer(f'❌ Промокод <code>{code}</code> уже существует.', parse_mode='HTML')
            return

    uses_label = f'{max_uses}' if max_uses > 0 else '∞'
    exp_label = expires_at.strftime('%d.%m.%Y') if expires_at else 'бессрочно'

    await message.answer(
        f'✅ <b>Промокод создан</b>\n\n'
        f'Код: <code>{code}</code>\n'
        f'Кредиты: <b>{credits}</b>\n'
        f'Активаций: <b>{uses_label}</b>\n'
        f'Истекает: <b>{exp_label}</b>',
        parse_mode='HTML',
    )


@router.message(Command('listpromos'))
async def cmd_listpromos(message: Message) -> None:
    if not is_admin(message):
        return

    from app.database.repo.promos import list_promos
    async with AsyncSessionFactory() as session:
        promos = await list_promos(session)

    if not promos:
        await message.answer('Промокодов нет.')
        return

    lines = ['🎟 <b>Промокоды (последние 20)</b>\n']
    for p in promos:
        status = '✅' if p.is_active else '❌'
        uses = f'{p.used_count}/{p.max_uses}' if p.max_uses > 0 else f'{p.used_count}/∞'
        exp = p.expires_at.strftime('%d.%m') if p.expires_at else '—'
        lines.append(f'{status} <code>{p.code}</code> · {p.credits}кр · {uses} · до {exp}')

    await message.answer('\n'.join(lines), parse_mode='HTML')


@router.message(Command('delpromo'))
async def cmd_delpromo(message: Message) -> None:
    if not is_admin(message):
        return

    code = (message.text or '').removeprefix('/delpromo').strip().upper()
    if not code:
        await message.answer('❌ Укажи код: <code>/delpromo КОД</code>', parse_mode='HTML')
        return

    from app.database.repo.promos import deactivate_promo
    async with AsyncSessionFactory() as session:
        ok = await deactivate_promo(session, code)
        await session.commit()

    if ok:
        await message.answer(f'✅ Промокод <code>{code}</code> деактивирован.', parse_mode='HTML')
    else:
        await message.answer(f'❌ Промокод <code>{code}</code> не найден.', parse_mode='HTML')


@router.message(Command('broadcast'))
async def cmd_broadcast(message: Message) -> None:
    if not is_admin(message):
        return

    text = message.text.removeprefix('/broadcast').strip()
    if not text:
        await message.answer(
            '❌ Укажи текст рассылки:\n<code>/broadcast Ваш текст здесь</code>',
            parse_mode='HTML',
        )
        return

    async with AsyncSessionFactory() as session:
        ids = await get_all_telegram_ids(session)

    total = len(ids)
    sent = 0
    failed = 0

    status_msg = await message.answer(f'📤 Начинаю рассылку на {total} пользователей...')

    for tg_id in ids:
        try:
            await message.bot.send_message(tg_id, text, parse_mode='HTML')
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f'✅ <b>Рассылка завершена</b>\n\n'
        f'📨 Отправлено: {sent}\n'
        f'❌ Не доставлено: {failed}',
        parse_mode='HTML',
    )
    logger.info('Broadcast done', sent=sent, failed=failed, total=total)


# Промо-рассылка новых пресетов: АЛЬБОМ из 2 фото (мужское + женское превью,
# чтобы зацепить оба пола и показать, что бот универсальный) + отдельное
# сообщение с 3-шаговой инструкцией и CTA-кнопкой в каталог. У альбомов TG
# нельзя прикрепить inline-кнопку → шлём кнопку вторым сообщением.
# Двухступенчато: /showcase test шлёт ТОЛЬКО админу (предпросмотр),
# /showcase send — всем активным ≤30д. Запуск = «явное да» владельца.
_SHOWCASE_CAPTION = (
    "🔥 <b>Зацени, что теперь умеет Нейра</b>\n\n"
    "Мрамор, неон, под водой, кибер-портрет — большая коллекция дерзких сцен. "
    "<b>И для парней, и для девушек:</b> бот спросит, для кого фото, "
    "и подберёт образ и стиль.\n\n"
    "Лицо в кадре — твоё."
)
_SHOWCASE_CTA = (
    "<b>Как сделать:</b>\n"
    "1️⃣ Жми кнопку ниже\n"
    "2️⃣ Укажи, для кого фото — парень или девушка\n"
    "3️⃣ Пришли селфи (можно файлом) и выбери сцену\n\n"
    "Готово за пару минут 👇"
)
# Кнопка showcase ведёт на change_gender (а НЕ gallery_open), чтобы выбор пола
# показался ВСЕМ получателям — и тем, кто пол не выбирал, и тем, кто выбирал.
# Иначе у выбравших шаг «укажи пол» был бы враньём (gallery_open их пускает
# сразу в категории). После выбора пола → категории → пресет → селфи.
_SHOWCASE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton.model_construct(
        text='Сделать фото с моим лицом',
        callback_data='change_gender',
        icon_custom_emoji_id=PE_SPARKLES, style='success',
    )],
])
# Приоритеты превью: первый найденный из списка. Мужское — position=1, женское — 0.
# marble_punk убран (жест-фак отталкивает) — 27.05.
_SHOWCASE_MALE = ('cyber_neon', 'skull_saint', 'underwater_red')
_SHOWCASE_FEMALE = ('underwater_red', 'goldfish_dream', 'korean_red_wall')


async def _build_showcase_collage(bot, male_fid: str, female_fid: str) -> bytes:
    """Склеивает мужское+женское превью в ОДНУ картинку (рядом). Так можно
    отправить одним сообщением с inline-кнопкой — у альбома (media_group)
    кнопки нет, а у одиночного фото есть."""
    import io
    from PIL import Image

    async def _dl(fid: str) -> bytes:
        f = await bot.get_file(fid)
        buf = io.BytesIO()
        await bot.download_file(f.file_path, buf)
        return buf.getvalue()

    imgs = [Image.open(io.BytesIO(await _dl(f))).convert('RGB')
            for f in (male_fid, female_fid)]
    h = min(im.height for im in imgs)
    resized = [im.resize((max(1, int(im.width * h / im.height)), h)) for im in imgs]
    gap = 16
    width = sum(im.width for im in resized) + gap * (len(resized) - 1)
    canvas = Image.new('RGB', (width, h), (18, 18, 20))
    x = 0
    for im in resized:
        canvas.paste(im, (x, 0))
        x += im.width + gap
    out = io.BytesIO()
    canvas.save(out, 'JPEG', quality=92, optimize=True)
    return out.getvalue()


@router.message(Command('showcase'))
async def cmd_showcase(message: Message) -> None:
    if not is_admin(message):
        return
    mode = message.text.removeprefix('/showcase').strip().lower()

    from app.database.repo.preset_previews import get_preview_file_id
    async with AsyncSessionFactory() as session:
        male_fid = female_fid = None
        for k in _SHOWCASE_MALE:
            male_fid = await get_preview_file_id(session, k, position=1)
            if male_fid:
                break
        for k in _SHOWCASE_FEMALE:
            female_fid = await get_preview_file_id(session, k, position=0)
            if female_fid:
                break

    if not (male_fid and female_fid):
        await message.answer('❌ Нет пары превью (муж+жен) для showcase в БД.')
        return

    caption = _SHOWCASE_CAPTION + '\n\n' + _SHOWCASE_CTA

    # Коллаж муж+жен в ОДНОЙ картинке: грузим один раз себе → file_id, дальше
    # рассылаем по file_id (не пере-загружая байты каждому получателю).
    from aiogram.types import BufferedInputFile
    try:
        collage = await _build_showcase_collage(message.bot, male_fid, female_fid)
    except Exception as exc:
        await message.answer(f'❌ Не удалось собрать коллаж: {str(exc)[:150]}')
        return
    first = await message.bot.send_photo(
        message.from_user.id, BufferedInputFile(collage, 'neyra_showcase.jpg'),
        caption=caption, parse_mode='HTML', reply_markup=_SHOWCASE_KB,
    )
    collage_fid = first.photo[-1].file_id

    if mode != 'send':
        await message.answer(
            '✅ Превью отправлено тебе — <b>одно сообщение</b>: 2 фото (муж+жен) '
            '+ кнопка. Тапни кнопку и пройди путь как юзер. Если ок — '
            '<code>/showcase send</code> разошлёт всем активным (≤30д).',
            parse_mode='HTML',
        )
        return

    from sqlalchemy import select, and_
    from app.database.models import User
    cutoff = datetime.utcnow() - timedelta(days=30)
    async with AsyncSessionFactory() as session:
        ids = (await session.execute(select(User.telegram_id).where(and_(
            User.is_banned == False, User.last_active_at >= cutoff,
        )))).scalars().all()

    total = len(ids)
    status = await message.answer(f'📤 Showcase: рассылаю на {total} активных…')
    sent = failed = 0
    for tg_id in ids:
        if tg_id == message.from_user.id:
            sent += 1  # уже получил превью выше — не дублируем
            continue
        try:
            await message.bot.send_photo(
                tg_id, collage_fid, caption=caption,
                parse_mode='HTML', reply_markup=_SHOWCASE_KB,
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(
        f'✅ <b>Showcase разослан</b>\n\n📨 Отправлено: {sent}\n❌ Не доставлено: {failed}',
        parse_mode='HTML',
    )
    logger.info('Showcase broadcast done', sent=sent, failed=failed, total=total)

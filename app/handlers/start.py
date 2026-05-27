from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select

import structlog
from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.constants import HELP_TEXT, MODEL_LABEL, REFERRAL_BONUS, WELCOME_TEXT, build_help_text
from app.database.base import AsyncSessionFactory
from app.database.models import Plan
from app.database.repo.bot_config import get_text
from app.database.repo.credits import add_credits
from app.database.repo.events import log_event_bg
from app.database.repo.experiments import get_user_variant
from app.database.repo.promos import get_promo, has_redeemed, redeem_promo
from app.database.repo.subscriptions import get_subscription, get_user_plan
from app.database.repo.usage import PLAN_LIMITS, get_daily_count
from app.database.repo.users import add_referral_bonus, get_or_create_user, get_user
from app.keyboards.main import (
    PE_BACK, PE_CHECK, PE_GEAR, PE_HANDSHAKE, PE_HOME, PE_MEGAPHONE, PE_MONEY,
    PE_ROBOT, PE_SPARKLES, PE_SPEAKER,
    main_menu_kb, model_selection_kb,
)
from app.keyboards.subscription import plans_kb
from app.utils import plural_ru, premium_text

logger = structlog.get_logger(__name__)
router = Router()

PLAN_EMOJI = {Plan.FREE: '🆓', Plan.BASIC: '⚡', Plan.PRO: '🚀'}
PLAN_NAME  = {Plan.FREE: 'Free', Plan.BASIC: 'Basic', Plan.PRO: 'Pro'}

REFERRAL_PENDING_KEY = 'referral_pending:{user_id}'


async def _send_welcome_with_collage(message, text: str, reply_markup, parse_mode: str = 'HTML', collage_fid: str | None = None) -> None:
    """Шлёт стартовое сообщение СРАЗУ с welcome-коллажем (3 пресета в одной
    картинке). Используется во ВСЕХ /start ветках — новый/существующий/промо-
    успех/промо-fail/реф/посевной диплинк. fal не дёргается: коллаж склеен один
    раз из готовых превью и кешируется как file_id в bot_config.

    Robustness:
      • Если коллаж не построился — fallback на чистый текст.
      • Caption limit Telegram = 1024 UTF-16 units (эмодзи занимают по 2), поэтому
        порог 900 chars + safety margin. Длинный текст → фото без подписи +
        отдельное сообщение с кнопками.
      • Если send_photo упал (протухший file_id и т.п.) — сбрасываем кэш и
        шлём чистый текст, чтобы /start не вернул 500."""
    from app.services.welcome_collage import (
        get_or_build_welcome_collage_fid, invalidate_welcome_collage,
    )
    # collage_fid override (напр. showcase новых сцен для канальных промо).
    # Если не задан — обычный welcome-коллаж.
    used_welcome = collage_fid is None
    if collage_fid:
        fid = collage_fid
    else:
        try:
            fid = await get_or_build_welcome_collage_fid(message.bot)
        except Exception as exc:
            logger.warning('welcome collage fetch failed', error=str(exc)[:200])
            fid = None
    if not fid:
        await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    # Безопасный caption-лимит с запасом на UTF-16 от эмодзи.
    CAPTION_SAFE_LIMIT = 900
    try:
        if len(text) <= CAPTION_SAFE_LIMIT:
            await message.answer_photo(fid, caption=text, parse_mode=parse_mode,
                                       reply_markup=reply_markup)
        else:
            await message.answer_photo(fid)
            await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as exc:
        # Файл протух / caption всё равно отверг / другая TG-ошибка — чистим
        # кэш (следующий /start пересоберёт) и фолбэк на текст, чтобы /start
        # не падал в 500.
        logger.warning('welcome collage send failed, falling back to text',
                       error=str(exc)[:200])
        try:
            if used_welcome:  # не сбрасываем welcome-кэш из-за чужого fid
                await invalidate_welcome_collage()
        except Exception:
            pass
        await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def build_menu_text(session, bot, user) -> str:
    """Единый текст главного меню — одинаковый для /start (существующий юзер),
    кнопки «В главное меню» и «Отмена».

    Раньше эти пути собирали текст по-разному (одни с балансом/соц-пруфом,
    другие — голый welcome), и меню «скакало»: то короткое, то длинное.
    Теперь все идут через один билдер. Состав:
      • welcome (из БД, fallback на константу)
      • строка баланса
      • строка про @conturD — подписчикам больше бесплатных кредитов в день.
        Если не подписан → CTA, если подписан → благодарность. Этой инфы
        раньше не было нигде в интерфейсе.
    """
    from app.database.repo.credits import DAILY_FREE_CREDITS, DAILY_CHANNEL_CREDITS
    from app.services import channel_service

    welcome = await get_text(session, 'text_welcome', WELCOME_TEXT)
    parts = [welcome]

    if user is not None:
        if user.credits > 0:
            parts.append(f'💳 <b>На счету: {user.credits:,} кр</b>')
        else:
            parts.append(
                '💳 <b>На счету: 0 кр</b> — пополни или подожди до завтра, '
                'начислю бесплатные.'
            )

    subscribed = False
    if bot is not None and user is not None:
        subscribed = await channel_service.is_subscribed(bot, user.telegram_id)
    if subscribed:
        parts.append(
            f'📣 <b>Ты в канале @conturD</b> — поэтому каждый день получаешь '
            f'<b>{DAILY_CHANNEL_CREDITS} бесплатных кр</b> вместо {DAILY_FREE_CREDITS}.'
        )
    else:
        # 24.05: было «подпишись и больше кредитов». Сухо, не работало —
        # 60% юзеров игнорят, не понимая что там за канал. Теперь продаём
        # реальную ценность: лайфхаки по боту, секретные промокоды,
        # анонсы новых сцен. Кредиты — приятный бонус, не главное.
        parts.append(
            f'📣 <b>Подпишись на @conturD</b> — и получишь:\n'
            f'• <b>{DAILY_CHANNEL_CREDITS} бесплатных кр каждый день</b> '
            f'вместо {DAILY_FREE_CREDITS}\n'
            f'• 🎁 секретные промокоды (только в постах, не в боте)\n'
            f'• 💡 фишки и лайфхаки по Нейре\n'
            f'• 🆕 анонсы новых сцен'
        )
    return premium_text('\n\n'.join(parts))


async def _notify_admins_new_user(bot, user, source: str, extra: str = '') -> None:
    """Шлёт админам уведомление о новой регистрации. Никогда не падает."""
    admin_ids = settings.admin_ids_list
    if not admin_ids:
        return

    username = f'@{user.username}' if user.username else '—'
    full_name = user.full_name or '—'
    extra_line = f'\n{extra}' if extra else ''
    ref_line = f'\n👥 Реферер: <code>{user.referrer_id}</code>' if user.referrer_id else ''

    text = (
        f'🆕 <b>Новый пользователь Нейры</b>\n\n'
        f'👤 {full_name}\n'
        f'🔗 {username}\n'
        f'🆔 <code>{user.telegram_id}</code>\n'
        f'🌍 Язык: {user.language_code or "—"}\n'
        f'📍 Источник: {source}'
        f'{ref_line}'
        f'{extra_line}'
    )

    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode='HTML')
        except Exception as exc:
            logger.warning('admin notify failed', admin_id=admin_id, error=str(exc)[:200])


def _onboarding_kb() -> InlineKeyboardMarkup:
    """Для новичков: hero — каталог (юзер видит результат → проще решиться),
    плюс face-swap. Лишнее («Свой вопрос», «Картинка с нуля») вынесено в Ещё.
    Основной CTA «пришли селфи» теперь в welcome-тексте → кнопка про сцены."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Посмотреть готовые сцены',
            callback_data='gallery_open', icon_custom_emoji_id=PE_SPARKLES, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Подменить лицо в чужом фото',
            callback_data='face_swap_start', icon_custom_emoji_id=PE_ROBOT, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='Ещё (рисунок, баланс, помощь)',
            callback_data='settings_menu', icon_custom_emoji_id=PE_GEAR,
        )],
    ])


def _promo_scenario_kb() -> InlineKeyboardMarkup:
    """После активации промо: подталкиваем сразу в каталог — самое яркое демо
    того, на что потрачены кредиты."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Посмотреть примеры и сделать своё',
            callback_data='gallery_open', icon_custom_emoji_id=PE_SPARKLES, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Подменить лицо в чужом фото',
            callback_data='face_swap_start', icon_custom_emoji_id=PE_ROBOT, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='Ещё', callback_data='settings_menu',
            icon_custom_emoji_id=PE_GEAR,
        )],
    ])


def _promo_scene_kb() -> InlineKeyboardMarkup:
    """Photo-first CTA для канальных промо. Юзер пришёл из поста про НОВЫЕ сцены
    → ведём в «🆕 Новинки» (первая категория каталога), плюс быстрый путь
    «сразу селфи» для решительных."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Смотреть новые сцены',
            callback_data='gallery_open',
            icon_custom_emoji_id=PE_SPARKLES, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Сразу прислать селфи',
            callback_data='edit_start',
            icon_custom_emoji_id=PE_CHECK, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='В главное меню', callback_data='main_menu',
            icon_custom_emoji_id=PE_HOME, style='danger',
        )],
    ])


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject, state: FSMContext,
    redis=None, is_new_user: bool = False,
) -> None:
    tg_user = message.from_user
    ref_arg = command.args or ''

    referrer_telegram_id: int | None = None
    promo_code_arg: str | None = None

    if ref_arg.startswith('ref_'):
        try:
            referrer_telegram_id = int(ref_arg[4:])
            if referrer_telegram_id == tg_user.id:
                referrer_telegram_id = None
        except ValueError:
            referrer_telegram_id = None
    elif ref_arg.startswith('promo_'):
        promo_code_arg = ref_arg[6:].upper().strip()

    async with AsyncSessionFactory() as session:
        user, is_new = await get_or_create_user(
            session,
            telegram_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name or '',
            language_code=tg_user.language_code or 'ru',
            referrer_id=referrer_telegram_id,
        )

        # Funnel-метрика активации: каждое /start (новички и возвращающиеся).
        # На дашборде distinct user даёт «дошли до welcome» — для замера, где
        # утечка между регистрацией и 1-м кадром (фокусный KPI Нейры).
        try:
            from app.database.repo.events import log_event
            await log_event(user.id, 'start_command', is_new_user=is_new,
                            has_promo=bool(promo_code_arg))
        except Exception as exc:
            logger.warning('start_command event log failed', error=str(exc)[:150])

        # Запоминаем реферера в Redis — бонус выдадим при первом сообщении
        if referrer_telegram_id and redis:
            already_has_referrer = bool(user.referrer_id)
            if is_new or not already_has_referrer:
                if not already_has_referrer:
                    user.referrer_id = referrer_telegram_id
                key = REFERRAL_PENDING_KEY.format(user_id=tg_user.id)
                await redis.set(key, str(referrer_telegram_id), ex=7 * 24 * 3600)

        # Автоматическая активация промокода из диплинка
        promo_activated: dict | None = None
        promo_fail_reason: str | None = None
        if promo_code_arg:
            promo = await get_promo(session, promo_code_arg)
            if not promo:
                promo_fail_reason = 'not_found'
            elif await has_redeemed(session, user.id, promo.id):
                promo_fail_reason = 'already_used'
            elif promo.max_uses > 0 and promo.used_count >= promo.max_uses:
                promo_fail_reason = 'exhausted'
            else:
                # Канал-эксклюзивные коды (SCENE*) — требуем подписку на @conturD.
                # Защита от вирусного растекания promo-ссылки за пределы канала.
                # Без redis (форс-мажор) — пропускаем чек чтобы не ломать legit-юзеров.
                require_channel = promo_code_arg.startswith('SCENE')
                channel_ok = True
                if require_channel and redis:
                    try:
                        from app.services import channel_service
                        channel_ok = await channel_service.check(
                            message.bot, redis, tg_user.id,
                        )
                    except Exception as exc:
                        # Падение channel API — не блокируем юзера, логируем
                        logger.warning('promo_channel_check_failed',
                                       code=promo_code_arg, error=str(exc)[:200])
                        channel_ok = True
                if not channel_ok:
                    promo_fail_reason = 'channel_required'
                else:
                    await add_credits(session, user, promo.credits)
                    await redeem_promo(session, user.id, promo)
                    promo_activated = {'code': promo_code_arg, 'credits': promo.credits}
                    user.promo_activated_at = datetime.now(timezone.utc)
                    user.time_bonus_granted = False
                    log_event_bg(user.id, 'promo_redeemed', credits_spent=-promo.credits, code=promo_code_arg)

        # Welcome bonus — ВСЕМ новичкам, независимо от промо/рефера.
        # Раньше: исключали юзеров с промо/рефером → юзер из канала с SCENE200 получал
        # только 200 кр и недоумевал почему в welcome обещано 300. Теперь стэкается:
        # SCENE200 + welcome 300 = 500 кр; рефер 150 + welcome 300 = 450 кр; органика = 300.
        welcome_bonus = 0
        if is_new_user:
            from app.database.repo.bot_config import get_config_value
            raw = (await get_config_value(session, 'welcome_bonus_credits') or '300').strip()
            try:
                welcome_bonus = max(0, int(raw))
            except ValueError:
                welcome_bonus = 300
            if welcome_bonus:
                await add_credits(session, user, welcome_bonus)

        # Login-бонус: начисляем ежедневный грант при заходе в бота, а не только
        # при фото-действии. Так обещание «ежедневный бонус» становится честным
        # для всех, кто открывает бота (free/канал/подписка). Новичкам не нужно —
        # у них welcome_bonus за этот день. Идемпотентно (раз в сутки по UTC).
        if not is_new_user:
            from app.services import channel_service
            await channel_service.grant_daily_aware(message.bot, session, user)

        # Серия заходов подряд: отмечаем сегодняшний заход (включая новичков —
        # у них стартует день 1). Начисляет milestone-награду на 3/7/14/30/60/100.
        from app.database.repo.credits import update_streak
        _streak_days, streak_milestone = await update_streak(session, user)

        user_db_id = user.id
        await session.commit()

    # Промокод активирован через диплинк
    if promo_activated:
        if is_new_user:
            log_event_bg(user_db_id, 'user_registered')
            await _notify_admins_new_user(
                message.bot, user,
                source=f'промокод <code>{promo_activated["code"]}</code>',
                extra=f'🎁 Зачислено: +{promo_activated["credits"]} кр',
            )
        code = promo_activated['code']
        credits = promo_activated['credits']

        # Если код от канала закодировал preset (CHAN_<PRESET>_xxx) — открываем
        # карточку этого пресета в галерее: юзер видит тот же пример что в посте
        # + кнопку «Хочу такое со своим лицом» (она и попросит селфи). Так от
        # клика по ссылке до результата — наглядный, понятный путь.
        from app.services.channel_publisher import _preset_key_from_promo
        preset_key = _preset_key_from_promo(code)
        if preset_key:
            from app.handlers.image_edit import (
                EDIT_PRESETS, _gallery_index_for_preset, _send_gallery_card,
            )
            if preset_key in EDIT_PRESETS:
                idx = await _gallery_index_for_preset(preset_key)
                if idx is not None:
                    clean_label = re.sub(r'^\W+', '', EDIT_PRESETS[preset_key][0]).strip()
                    total_bonus = credits + (welcome_bonus if is_new_user else 0)
                    # Welcome-коллаж (3 пресета) → подпись с подарком → карточка
                    # конкретного preset из ссылки. Особенно важно для посевных
                    # CHAN_<PRESET>_* — юзер видит ассортимент и сразу нужную сцену.
                    await _send_welcome_with_collage(
                        message,
                        premium_text(
                            f'🎉 <b>Лови подарок — +{total_bonus} кредитов зачислено!</b>\n\n'
                            f'Вот сцена <b>«{clean_label}»</b> 👇 Нравится — жми '
                            f'<b>«Хочу такое со своим лицом»</b>, пришлю готовый кадр с твоим '
                            f'лицом за минуту.\n\n'
                            f'<i>Не та сцена? Листай стрелками ◀️ ▶️ — внутри десятки готовых '
                            f'образов, выбирай любой.</i>'
                        ),
                        reply_markup=None,
                    )
                    await _send_gallery_card(message, idx=idx)
                    return
        total_credits = credits + (welcome_bonus if is_new_user else 0)
        credits_w_total = plural_ru(total_credits, 'кредит', 'кредита', 'кредитов')
        scenes_n = max(1, total_credits // 200)
        scenes_w = plural_ru(scenes_n, 'сцена', 'сцены', 'сцен')

        # Заголовок-целебрация. Адаптируем под источник, чтобы у юзера
        # сразу было ощущение «бот меня узнал, я не случайный».
        if code.startswith('AURUM'):
            hero = f'🎁 <b>Aurum дарит тебе {total_credits} {credits_w_total}</b>'
        elif code.startswith('VELUM'):
            hero = f'🎁 <b>Velum дарит тебе {total_credits} {credits_w_total}</b>'
        elif code.startswith('SCENE') or code.startswith('CHAN'):
            hero = f'🎁 <b>+{total_credits} {credits_w_total} от @conturD — твои!</b>'
        elif code.startswith('TG'):
            hero = f'🎁 <b>Добро пожаловать! {total_credits} {credits_w_total} в подарок</b>'
        else:
            hero = f'🎁 <b>Промокод активирован — {total_credits} {credits_w_total} на счёт</b>'

        # Бонус-строка: БЕЗ внутреннего кода промо (юзеру он не нужен — лишний
        # шум). Показываем только подарок новичку, если он есть. 24.05 фикс.
        if is_new_user and welcome_bonus:
            bonus_breakdown = f'<i>+{welcome_bonus} кр в подарок новичку уже внутри</i>'
        else:
            bonus_breakdown = ''

        is_channel = code.startswith('SCENE') or code.startswith('CHAN')
        if is_channel:
            # Посадка с канала: коллаж и текст про ТЕ ЖЕ новые сцены, что в посте.
            # CTA ведёт в «🆕 Новинки». Коллаж — showcase из bot_config (фолбэк
            # на welcome внутри _send_welcome_with_collage).
            hero_block = hero + (f'\n{bonus_breakdown}' if bonus_breakdown else '')
            text = premium_text(
                f'{hero_block}\n\n'
                f'✨ <b>Это те самые новые сцены 👆</b>\n'
                f'Пришли одно селфи → выбери сцену → через минуту ты в ней. '
                f'Лицо <b>1-в-1 твоё</b>, не чужая модель.\n\n'
                f'🏙 Карниз небоскрёба\n'
                f'🍜 Ночь в раменной\n'
                f'🕶 Неоновый портрет\n'
                f'🎆 Огни фейерверка\n'
                f'🕹 Зал автоматов\n\n'
                f'💎 На счету <b>{total_credits} кр</b> — хватит на фото с твоим лицом прямо сейчас.\n\n'
                f'<b>👇 Жми «Смотреть новые сцены» — или сразу пришли селфи</b>'
            )
            async with AsyncSessionFactory() as _s:
                from app.database.repo.bot_config import get_config_value as _gcv
                sc_fid = await _gcv(_s, 'showcase_collage_fid')
            await _send_welcome_with_collage(
                message, text, _promo_scene_kb(), collage_fid=sc_fid or None,
            )
        else:
            hero_block = hero + (f'\n{bonus_breakdown}' if bonus_breakdown else '')
            text = premium_text(
                f'{hero_block}\n\n'
                f'✨ <b>Нейра делает фото с твоим лицом в любой сцене.</b>\n'
                f'Пришли селфи → выбери сцену → через минуту готовый кадр. '
                f'Лицо <b>1-в-1 твоё</b>, не модели.\n\n'
                f'<b>В коллекции десятки сцен, свежие сверху:</b>\n'
                f'🏙 На карнизе высоток\n'
                f'🍜 Ночь в раменной\n'
                f'🕶 Неоновый портрет\n'
                f'🌹 Полночь в Париже\n\n'
                f'💎 На счету <b>{total_credits} кр</b> — хватит на фото с твоим лицом.\n\n'
                f'<b>📎 Жми кнопку и присылай селфи 👇</b>'
            )
            await _send_welcome_with_collage(message, text, _promo_scenario_kb())
        return

    # Промокод в диплинке не сработал — показываем причину
    if promo_fail_reason:
        if promo_fail_reason == 'channel_required':
            note = (
                f'🔒 Промокод <b>{promo_code_arg}</b> только для подписчиков канала.\n\n'
                f'Подпишись на @conturD и нажми кнопку снова — кредиты начислятся.'
            )
            retry_link = f'https://t.me/NeyraChatBot?start=promo_{promo_code_arg}'
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton.model_construct(
                    text='Открыть @conturD',
                    url='https://t.me/conturD',
                    icon_custom_emoji_id=PE_MEGAPHONE, style='primary',
                )],
                # Green: момент награды — «я подписался, давай кредиты»
                [InlineKeyboardButton.model_construct(
                    text='Я подписался — забрать кредиты',
                    url=retry_link,
                    icon_custom_emoji_id=PE_CHECK, style='success',
                )],
                [InlineKeyboardButton.model_construct(
                    text='В главное меню', callback_data='main_menu',
                    icon_custom_emoji_id=PE_HOME, style='danger',
                )],
            ])
            await message.answer(note, reply_markup=kb)
            return
        if promo_fail_reason == 'already_used':
            note = f'ℹ️ Промокод <b>{promo_code_arg}</b> уже был активирован на твоём аккаунте.'
        elif promo_fail_reason == 'exhausted':
            note = f'😔 Промокод <b>{promo_code_arg}</b> уже закончился — все места разобраны.'
        else:
            note = f'❌ Промокод <b>{promo_code_arg}</b> не найден или недействителен.'
        await message.answer(note, reply_markup=main_menu_kb())
        return

    async with AsyncSessionFactory() as session:
        base_welcome = await get_text(session, 'text_welcome', WELCOME_TEXT)
        social_proof = await get_text(session, 'text_social_proof', '')
        # A/B test override: if an experiment targets 'welcome', use its variant
        try:
            variant_text, _, _ = await get_user_variant(session, user_db_id, 'welcome')
            welcome_text = variant_text or base_welcome
        except Exception:
            welcome_text = base_welcome
        if social_proof.strip():
            welcome_text = f"{social_proof.strip()}\n\n{welcome_text}"

    # Достаём актуальный баланс юзера для персонализированной приписки
    async with AsyncSessionFactory() as session:
        fresh_user = await get_user(session, tg_user.id)
        current_credits = fresh_user.credits if fresh_user else 0

    if is_new_user:
        log_event_bg(user_db_id, 'user_registered')
        source = 'реферальная ссылка' if referrer_telegram_id else 'органика'
        await _notify_admins_new_user(message.bot, user, source=source)
        # Подарок настроен в bot_config.welcome_bonus_credits. Если новичок пришёл
        # с рефером/промо — там свой бонус, поэтому welcome_bonus здесь 0.
        if welcome_bonus >= 200:
            gift_line = (
                f'\n\n🎁 <b>Подарок новичку: {welcome_bonus} кредитов</b>\n'
                f'<i>Хватит на пару фото с твоим лицом (Полночь в Париже, Балет на ступенях…) — '
                f'выбирай сцену и пробуй.</i>'
            )
        elif welcome_bonus:
            gift_line = (
                f'\n\n🎁 <b>Подарок новичку: {welcome_bonus} кредитов</b>\n'
                f'<i>Хватит на пару пресетов — выбирай и пробуй.</i>'
            )
        else:
            gift_line = ''
        await _send_welcome_with_collage(
            message, welcome_text + gift_line, _onboarding_kb(),
        )
    else:
        # Для уже существующего юзера — единый текст меню (welcome + баланс +
        # строка про @conturD), идентичный кнопке «В главное меню», чтобы меню
        # не «скакало». Сверху — momentary streak-празднование (welcome-back).
        from app.database.repo.credits import next_streak_milestone
        streak_bits: list[str] = []
        if streak_milestone:
            streak_bits.append(
                f'🎉 <b>{_streak_days} дней подряд — держи +{streak_milestone} кр в подарок!</b>'
            )
        if _streak_days >= 1:
            s = f'🔥 Серия: <b>{_streak_days}</b> дн подряд'
            nm = next_streak_milestone(_streak_days)
            if nm:
                d, amt = nm
                s += f' · награда <b>+{amt} кр</b> на {d}-й день'
            streak_bits.append(s)
        streak_tail = ('\n\n' + '\n'.join(streak_bits)) if streak_bits else ''
        async with AsyncSessionFactory() as session:
            menu_user = await get_user(session, tg_user.id)
            menu_body = await build_menu_text(session, message.bot, menu_user)
        from app.keyboards.main import main_menu_kb_for_user
        kb = await main_menu_kb_for_user(
            tg_user.id, menu_user.credits if menu_user else 0,
        )
        await _send_welcome_with_collage(message, menu_body + streak_tail, kb)


@router.message(Command('help'))
async def cmd_help(message: Message) -> None:
    # build_help_text() собирается на лету — актуальные цены, число сцен,
    # дневной бонус. Игнорим text_help из БД (он быстро устаревал).
    help_text = await build_help_text()
    await message.answer(help_text, reply_markup=main_menu_kb())


@router.message(Command('history'))
async def cmd_history(message: Message) -> None:
    """Экспортирует историю чата пользователя в TXT-файл."""
    from aiogram.types import BufferedInputFile
    from sqlalchemy import select
    from app.database.models import ChatMessage
    from app.database.repo.users import get_user

    tg_id = message.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.asc())
        )
        messages = result.scalars().all()

    if not messages:
        await message.answer(
            'ℹ️ <b>История пока пуста</b>\n\n'
            'Я начала запоминать диалоги недавно — пиши вопросы, '
            'и через какое-то время сможешь скачать всю переписку.',
        )
        return

    lines = [
        f'# История диалога с Нейрой',
        f'# Пользователь: @{user.username or user.telegram_id}',
        f'# Всего сообщений: {len(messages)}',
        '',
    ]
    for m in messages:
        ts = m.created_at.strftime('%Y-%m-%d %H:%M:%S') if m.created_at else ''
        prefix = '👤 Я' if m.role == 'user' else f'🤖 Нейра'
        lines.append(f'[{ts}] {prefix}:')
        lines.append(m.content)
        lines.append('')

    body = '\n'.join(lines).encode('utf-8')
    fname = f'neyra-history-{tg_id}.txt'
    await message.answer_document(
        BufferedInputFile(body, filename=fname),
        caption=f'📜 <b>Твоя история ({len(messages)} сообщений)</b>',
    )


@router.message(Command('status'))
async def cmd_status(message: Message, redis=None) -> None:
    await _show_status(message, redis=redis)


@router.message(Command('selectmodel'))
async def cmd_select_model(message: Message) -> None:
    from app.handlers.chat import show_model_selection
    await show_model_selection(message)


@router.message(Command('ref'))
async def cmd_ref(message: Message) -> None:
    tg_id = message.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return
        invited = user.referral_bonus  # теперь хранит кол-во приглашённых

    kb, text = _ref_content(tg_id, invited)
    await message.answer(text, parse_mode='HTML', reply_markup=kb)


def _ref_content(tg_id: int, invited: int):
    from urllib.parse import quote
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    bot_username = settings.BOT_USERNAME or 'NeyraChatBot'
    link = f'https://t.me/{bot_username}?start=ref_{tg_id}'
    # 24.05: было «умный AI-помощник, отвечает на вопросы» — но Нейра уже год
    # как фото-бот, не Q&A. Старый текст вводил в заблуждение, конверсии 0.
    share_text = (
        'Смотри, что я нашёл — Нейра делает фото с твоим лицом в любой сцене. '
        'Шлёшь селфи → за минуту получаешь себя в Париже, на обложке глянца, '
        'в неоне будущего. 500 кр в подарок новичку.'
    )
    share_url = f'https://t.me/share/url?url={quote(link, safe="")}&text={quote(share_text, safe="")}'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(text='Поделиться с друзьями', url=share_url, icon_custom_emoji_id=PE_HANDSHAKE, style='success')],
        [InlineKeyboardButton.model_construct(text='В главное меню', callback_data='main_menu', icon_custom_emoji_id=PE_BACK, style='danger')],
    ])

    earned = invited * REFERRAL_BONUS
    milestones = ''
    if invited < 5:
        milestones = f'\n🎯 Пригласи {5 - invited} ещё — получи +200 кр бонусом'
    elif invited < 10:
        milestones = f'\n🎯 Пригласи {10 - invited} ещё — получи +500 кр бонусом'
    else:
        milestones = f'\n🏆 Топ-реферер! Ты пригласил {invited} друзей'

    text = (
        f'🤝 <b>Приглашай друзей — получай кредиты</b>\n\n'
        f'За каждого друга, который сделает первое фото в Нейре:\n'
        f'<b>+{REFERRAL_BONUS} кредитов</b> на твой счёт.\n\n'
        f'<b>Дополнительные подарки:</b>\n'
        f'• 5 друзей — ещё <b>+200 кредитов</b>\n'
        f'• 10 друзей — ещё <b>+500 кредитов</b>\n\n'
        f'👥 Уже приглашено: <b>{invited}</b>\n'
        f'🎁 Заработано: <b>{earned:,} кредитов</b>'
        f'{milestones}\n\n'
        f'<b>Твоя ссылка для друзей:</b>\n'
        f'<code>{link}</code>'
    )
    return kb, text


@router.callback_query(lambda c: c.data == 'main_menu')
async def cb_main_menu(callback: CallbackQuery) -> None:
    """Возврат в главное меню.

    24.05 оптимизация: раньше всегда delete+send_new_photo → в чате копилось
    по 5+ копий коллажа за сессию. Теперь:
      • если текущее сообщение УЖЕ фото с caption → edit_caption (коллаж
        остаётся тем же, только меняется текст + кнопки)
      • иначе (текстовое сообщение типа /sub, /shop) → delete + новое фото
        (нужен ВИЗУАЛ коллажа в меню)
    """
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        text = await build_menu_text(session, callback.message.bot, user)
    from app.keyboards.main import main_menu_kb_for_user
    kb = await main_menu_kb_for_user(
        callback.from_user.id, user.credits if user else 0,
    )

    msg = callback.message
    # Telegram caption limit ~1024; усекаем безопасно если меню разрослось.
    CAPTION_SAFE_LIMIT = 900
    if msg.caption is not None and len(text) <= CAPTION_SAFE_LIMIT:
        from aiogram.exceptions import TelegramBadRequest
        try:
            await msg.edit_caption(caption=text, parse_mode='HTML', reply_markup=kb)
            await callback.answer()
            return
        except TelegramBadRequest:
            pass
        except Exception:
            pass
    # Фолбэк: delete + новый коллаж.
    try:
        await msg.delete()
    except Exception:
        pass
    await _send_welcome_with_collage(callback.message, text, kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == 'img_hint')
async def cb_img_hint(callback: CallbackQuery) -> None:
    await callback.answer(
        'Напиши /img и описание картинки. Пример:\n/img закат над горами, масло',
        show_alert=True,
    )


@router.callback_query(lambda c: c.data == 'ref_menu')
async def cb_ref_menu(callback: CallbackQuery) -> None:
    tg_id = callback.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, tg_id)
        invited = user.referral_bonus if user else 0  # кол-во приглашённых

    kb, text = _ref_content(tg_id, invited)
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == 'my_status')
async def cb_my_status(callback: CallbackQuery, redis=None) -> None:
    """Кнопка 'Мой баланс' — сначала ответ, потом действие."""
    await callback.answer()
    await _show_status(callback.message, edit=True, redis=redis)


@router.callback_query(lambda c: c.data == 'new_chat')
async def cb_new_chat(callback: CallbackQuery, redis=None) -> None:
    """Кнопка 'Новый чат' — очищает контекст и показывает приятное подтверждение."""
    user_tg_id = callback.from_user.id
    if redis:
        from app.services import context_service
        await context_service.clear_context(redis, user_tg_id)
    text = (
        '🔄 <b>Новый чат начат</b>\n\n'
        'Память очищена — я забыл предыдущий разговор.\n'
        'Можешь начинать с новой темы.\n\n'
        '💡 Просто напиши свой вопрос.'
    )
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(callback, text, reply_markup=main_menu_kb())
    await callback.answer('Память очищена ✨', show_alert=False)


@router.callback_query(lambda c: c.data == 'help')
async def cb_help(callback: CallbackQuery) -> None:
    from app.keyboards.main import settings_menu_kb
    from app.utils import safe_edit_or_answer
    help_text = await build_help_text()
    await safe_edit_or_answer(callback, help_text, reply_markup=settings_menu_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data == 'onboard_ask')
async def cb_onboard_ask(callback: CallbackQuery) -> None:
    await callback.message.answer(
        premium_text(
            '🗣 <b>Напиши свой вопрос — я отвечу за секунды.</b>\n\n'
            'Можно текстом, можно голосовым — мне всё равно.'
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == 'onboard_advice')
async def cb_onboard_advice(callback: CallbackQuery) -> None:
    await callback.message.answer(
        premium_text(
            '⚡ <b>Расскажи о ситуации — подскажу как поступить.</b>\n\n'
            'Например: «Что подарить на годовщину?», «Как договориться о повышении?», '
            '«Чем занять ребёнка в дождь?»'
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == 'onboard_write')
async def cb_onboard_write(callback: CallbackQuery) -> None:
    await callback.message.answer(
        premium_text(
            '🎁 <b>Опиши что нужно написать — сделаю.</b>\n\n'
            'Письмо, пост, поздравление, резюме, объявление — '
            'просто скажи о чём и для кого.'
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == 'onboard_translate')
async def cb_onboard_translate(callback: CallbackQuery) -> None:
    await callback.message.answer(
        premium_text(
            '🌍 <b>Пришли любой иностранный текст — переведу.</b>\n\n'
            'Можно текстом, картинкой с текстом, или голосовым сообщением на любом языке.\n\n'
            'Я понимаю английский, испанский, немецкий, китайский, японский, и ещё 50+ языков.'
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == 'voice_hint')
async def cb_voice_hint(callback: CallbackQuery) -> None:
    await callback.message.answer(
        premium_text(
            '🎤 Просто запиши голосовое — я расшифрую и отвечу тоже текстом или голосом.\n\n'
            'Удобно когда печатать лень.'
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def _show_status(message: Message, edit: bool = False, redis=None) -> None:
    from datetime import date as _date, timedelta as _td
    from app.database.repo.credits import (
        DAILY_FREE_CREDITS, DAILY_CHANNEL_CREDITS, MODEL_COSTS, SUBSCRIPTION_PLANS,
    )
    from app.database.models import Subscription
    from app.services import channel_service
    tg_id = message.chat.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, tg_id)
        if not user:
            text = '❌ Пользователь не найден. Нажми /start'
            if edit:
                await message.edit_text(text)
            else:
                await message.answer(text)
            return

        # Подчищаем сгоревшие burn-кредиты на чтении (lazy cleanup).
        # Если burn_expires_at в прошлом — обнуляем, перед показом баланса.
        from app.database.repo.credits import _clean_expired_burn, update_streak
        _clean_expired_burn(user)
        # Отмечаем заход в серию (идемпотентно за сутки). Если статус — первый
        # заход за день, серия растёт здесь и начисляется milestone-награда.
        streak_days, streak_milestone = await update_streak(session, user)
        credits = user.credits
        burn_credits = user.burn_credits or 0
        burn_until = user.burn_expires_at
        invited = user.referral_bonus
        streak_last = user.streak_last_date
        trial_left = getattr(user, 'trial_pro_messages_left', 0) or 0

        # Активная подписка — приоритет для расчёта daily bonus
        sub_plan_label: str | None = None
        sub_daily_bonus: int | None = None
        sub_expires: str | None = None
        sub = await session.scalar(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        from datetime import datetime as _dt, timezone as _tz
        if sub and sub.is_active and sub.expires_at:
            exp = sub.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            if exp > _dt.now(_tz.utc):
                plan_val = sub.plan.value if hasattr(sub.plan, 'value') else str(sub.plan)
                plan_dict = next((p for p in SUBSCRIPTION_PLANS if p['plan_enum'] == plan_val), None)
                if plan_dict:
                    sub_plan_label = f"{plan_dict['emoji']} {plan_dict['name']}"
                    sub_daily_bonus = plan_dict['daily_bonus']
                    sub_expires = exp.strftime('%d.%m.%Y')

        # Фиксируем обновление серии (update_streak делал только flush).
        await session.commit()

    # Проверка канала (с кэшем 4ч в Redis)
    is_channel_sub = False
    if redis and not sub_daily_bonus:
        try:
            is_channel_sub = await channel_service.check(message.bot, redis, tg_id)
        except Exception:
            is_channel_sub = False

    # Фактический ежедневный бонус для этого юзера
    if sub_daily_bonus is not None:
        actual_bonus = sub_daily_bonus
        bonus_source = sub_plan_label or 'подписка'
    elif is_channel_sub:
        actual_bonus = DAILY_CHANNEL_CREDITS
        bonus_source = 'подписан на @conturD'
    else:
        actual_bonus = DAILY_FREE_CREDITS
        bonus_source = None

    # Streak считаем актуальным только если последний день не позже вчера.
    # Время МСК — иначе ночью 0-3 МСК считает streak как протухший.
    from app.utils import today_msk
    today = today_msk()
    streak_active = bool(
        streak_last and streak_last >= (today - _td(days=1)) and streak_days >= 1
    )

    # Промо-инфо для опционального тонкого баннера в конце
    from app.services.pricing import get_promo_info
    promo = await get_promo_info()

    # ── Компоновка: один экран, 4 смысловых блока ──
    # 1. Баланс (главный)
    # 2. Подписка (если есть) ИЛИ daily-бонус (если нет)
    # 3. Стрик/рефералы — только когда есть что показать (минимум визуального шума)
    # 4. Запуск-цена — одной строкой, в самом конце

    total = credits + burn_credits
    lines: list[str] = [f'💳 <b>{total:,} кредитов</b>']
    if streak_milestone:
        lines.append(f'🎉 <b>{streak_days} дней подряд — держи +{streak_milestone} кр в подарок!</b>')
    if burn_credits > 0 and burn_until:
        from datetime import datetime as _dt, timezone as _tz
        exp = burn_until
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_tz.utc)
        days_left = max(0, (exp - _dt.now(_tz.utc)).days)
        exp_str = exp.strftime('%d.%m')
        lines.append(
            f'<i>🔥 {burn_credits:,} сгорят через {days_left} дн ({exp_str}) · '
            f'♾ {credits:,} без срока · тратятся burn первыми</i>'
        )

    if sub_plan_label:
        lines.append(f'{sub_plan_label} · до {sub_expires}')
        lines.append(f'📅 +{actual_bonus} кр/день')
    elif is_channel_sub:
        lines.append(f'📅 +{actual_bonus} кр/день · ты в @conturD')
    else:
        # Без подписки — показываем сильный хук на канал (2.5× бонус)
        lines.append(f'📅 +{actual_bonus} кр/день')
        lines.append(
            f'<i>↑ Подпишись на <a href="https://t.me/conturD">@conturD</a> — '
            f'будет +{DAILY_CHANNEL_CREDITS} кр/день (2.5× больше).</i>'
        )

    # Достижения / референсы — только если применимо.
    # trial_left убран: после visual-only pivot Claude Sonnet больше не доступен,
    # упоминание триала вводит в заблуждение.
    extras: list[str] = []
    if streak_days >= 1:
        from app.database.repo.credits import next_streak_milestone
        s = f'🔥 Серия: <b>{streak_days}</b> дн подряд'
        nm = next_streak_milestone(streak_days)
        if nm:
            d, amt = nm
            s += f' · награда <b>+{amt} кр</b> на {d}-й день'
        extras.append(s)
    if invited > 0:
        extras.append(f'🤝 {invited} друзей · +{invited * REFERRAL_BONUS:,} кр')
    if extras:
        lines.append('')
        lines.append('\n'.join(extras))

    # Промо запуска фото — одна строка, без таблиц
    if promo['active']:
        lines.append('')
        lines.append(
            f'🎁 До {promo["until"]} — фото-правки и face-swap по '
            f'<b>{promo["effective_costs"]["image_edit"]} кр</b> '
            f'(обычно {promo["base_costs"]["image_edit"]})'
        )

    text = premium_text('\n'.join(lines))

    # Кнопки. Текст БЕЗ ведущих эмодзи — иконку даёт icon_custom_emoji_id (premium),
    # дублирование в тексте отображается лишним символом у premium-юзеров.
    # Подбираем premium-иконки точно по смыслу действия.
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from app.keyboards.main import PE_AU_GOLD, PE_CARD
    rows: list[list[InlineKeyboardButton]] = []
    if sub_plan_label:
        # У юзера есть подписка → главное действие = докупить кредиты (для сцен).
        # Green — единственное money-action на экране.
        rows.append([InlineKeyboardButton.model_construct(
            text='Купить кредиты', callback_data='shop',
            icon_custom_emoji_id=PE_CARD, style='success',
        )])
        rows.append([InlineKeyboardButton.model_construct(
            text='Изменить подписку', callback_data='subscription',
            icon_custom_emoji_id=PE_AU_GOLD,
        )])
    else:
        rows.append([InlineKeyboardButton.model_construct(
            text='Оформить подписку', callback_data='subscription',
            icon_custom_emoji_id=PE_AU_GOLD, style='success',
        )])
        rows.append([InlineKeyboardButton.model_construct(
            text='Купить кредиты разово', callback_data='shop',
            icon_custom_emoji_id=PE_CARD, style='primary',
        )])
    rows.append([InlineKeyboardButton.model_construct(
        text='В главное меню', callback_data='main_menu',
        icon_custom_emoji_id=PE_BACK, style='danger',
    )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        # message может быть photo/video (welcome-коллаж) → edit_text упадёт.
        # Пробуем text → caption → фолбэк delete+answer.
        done = False
        try:
            if message.text is not None:
                await message.edit_text(text, reply_markup=kb)
                done = True
            elif message.caption is not None:
                await message.edit_caption(caption=text, reply_markup=kb)
                done = True
        except Exception:
            done = False
        if not done:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from datetime import date

from app.constants import MODEL_LABEL
from app.database.base import AsyncSessionFactory
from app.database.models import Plan, User
from app.database.repo.credits import (
    DAILY_CHANNEL_CREDITS,
    DAILY_FREE_CREDITS,
    MODEL_COSTS,
    add_credits,
    deduct_credits,
    grant_daily_if_needed,
    update_streak,
)
from app.database.repo.events import log_event_bg
from app.database.repo.messages import save_message_bg
from app.database.repo.users import add_referral_bonus, get_user, set_preferred_model
from app.keyboards.main import (
    PE_AU_GOLD, PE_CARD, PE_CHECK, PE_FRAME, PE_HOME, PE_MEGAPHONE, PE_ROBOT,
    buy_or_invite_kb, main_menu_kb, model_selection_kb, settings_menu_kb,
)
from app.services import ai_service, channel_service, context_service
from app.utils import markdown_to_html, send_long as _send_long

LOW_CREDITS_NUDGE = (
    "\n\n━━━━━━━━━━━━━━━━━━━━\n"
    "⚡ <b>У тебя осталось {credits} кредитов.</b>\n"
    "Пополни сейчас — не прерывай работу."
)

logger = structlog.get_logger(__name__)
router = Router()

_BUY_KB = buy_or_invite_kb()


def _pick_model(user: User, credits: int) -> tuple[str, bool]:
    """Возвращает (model_key, is_trial). После visual-only pivot чат вторичен:
    всегда отвечает DeepSeek (free), платные модели и триал Claude отключены.
    Если когда-нибудь вернёмся к платным моделям — восстановить из git.
    """
    if credits >= MODEL_COSTS['free']:
        return 'free', False
    return '', False


async def show_model_selection(target: CallbackQuery | Message) -> None:
    """Показать экран выбора модели (работает и с callback, и с message)."""
    user_tg_id = target.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            text = 'Ошибка. Нажми /start'
            if isinstance(target, CallbackQuery):
                await target.answer(text, show_alert=True)
            else:
                await target.answer(text)
            return
        current_model = getattr(user, 'preferred_model', 'free')
    
    text = ('🤖 <b>Модель чата</b>\n\n'
            'Сейчас в боте одна стабильная модель для текстовых ответов:\n\n'
            '🧠 <b>DeepSeek V3.2</b> · 2 кр / ответ\n'
            'Отвечает на простые вопросы за пару секунд.\n\n'
            '<i>Главная фишка Нейры — фото. Жми /edit чтобы превратить '
            'селфи в кадр в любой сцене.</i>')
    
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode='HTML', reply_markup=model_selection_kb(current_model))
        await target.answer()
    else:
        await target.answer(text, parse_mode='HTML', reply_markup=model_selection_kb(current_model))


@router.message(Command('new'))
async def cmd_new_chat(message: Message, redis=None) -> None:
    if redis:
        await context_service.clear_context(redis, message.from_user.id)
    text = (
        '🔄 <b>Новый разговор</b>\n\n'
        'Память очищена — я забыл предыдущую беседу.\n'
        'Пиши о чём хочешь 👇'
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(lambda c: c.data == 'clear_memory')
async def cb_clear_memory(callback: CallbackQuery, redis=None) -> None:
    """Кнопка 'Очистить память' из настроек."""
    if redis:
        await context_service.clear_context(redis, callback.from_user.id)
    text = (
        '🧹 <b>Память очищена</b>\n\n'
        'Я забыл предыдущий разговор.\n'
        'Следующее твоё сообщение будет началом новой беседы.'
    )
    await callback.message.edit_text(text, reply_markup=settings_menu_kb())
    await callback.answer('Память очищена ✨')


@router.callback_query(lambda c: c.data == 'settings_menu')
async def cb_settings_menu(callback: CallbackQuery) -> None:
    settings_text = (
        '⚙️ <b>Ещё</b>\n\n'
        '• <b>Пригласить друга</b> — +150 кр за каждого\n'
        '• <b>Промокод</b> — активировать\n'
        '• <b>Сменить пол</b> — для какого гендера каталог\n'
        '• <b>Помощь</b> — как пользоваться ботом\n\n'
        'Выбери что нужно 👇'
    )
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(callback, settings_text, reply_markup=settings_menu_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data == 'select_model')
async def cb_select_model(callback: CallbackQuery) -> None:
    """Показать экран выбора модели ИИ."""
    await show_model_selection(callback)


@router.callback_query(lambda c: c.data and c.data.startswith('set_model:'))
async def cb_set_model(callback: CallbackQuery) -> None:
    """Установить выбранную модель.
    Visual-only: пускаем только 'free' (DeepSeek). Старые callback'и (pro/ultra) с
    deeplink'ов и кэшированных сообщений мягко сворачиваем в free."""
    model = callback.data.split(':', 1)[1]
    if model != 'free':
        # Любые попытки выставить дорогую модель — silently на free, без шок-алертов
        model = 'free'
    
    async with AsyncSessionFactory() as session:
        user = await get_user(session, callback.from_user.id)
        if not user or not await set_preferred_model(session, callback.from_user.id, model):
            await callback.answer('Ошибка сохранения', show_alert=True)
            return
        
        await session.commit()
        current_model = model
    
    await callback.message.edit_text(
        f'✅ <b>Модель изменена!</b>\n\n'
        f'Теперь по умолчанию используется: <b>{MODEL_LABEL[model]}</b>\n'
        f'Если кредитов недостаточно, бот автоматически выберет доступную модель.',
        parse_mode='HTML',
        reply_markup=model_selection_kb(current_model)
    )
    await callback.answer('Модель сохранена!')


_CHANNEL_NUDGE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton.model_construct(
        text='Подписаться на @conturD',
        url='https://t.me/conturD',
        icon_custom_emoji_id=PE_MEGAPHONE, style='success',
    )],
    [InlineKeyboardButton.model_construct(
        text='Уже подписан(а) — проверить',
        callback_data='check_channel_sub',
        icon_custom_emoji_id=PE_CHECK, style='primary',
    )],
])


@router.callback_query(lambda c: c.data == 'check_channel_sub')
async def cb_check_channel_sub(callback: CallbackQuery, redis=None) -> None:
    user_tg_id = callback.from_user.id
    if not redis:
        await callback.answer('Ошибка. Попробуй позже.', show_alert=True)
        return

    await channel_service.invalidate(redis, user_tg_id)
    is_sub = await channel_service.check(callback.bot, redis, user_tg_id)

    if not is_sub:
        await callback.answer(
            'Подписка не найдена 👀\nПодпишись на @conturD и нажми снова.',
            show_alert=True,
        )
        return

    # Atomic GETDEL: двойной клик «Я подписался» не даст двойного top-up,
    # потому что флаг исчезает в момент чтения внутри Redis.
    from app.utils import today_msk
    today = today_msk().isoformat()
    if await channel_service.consume_free_tier(redis, user_tg_id, today):
        bonus = DAILY_CHANNEL_CREDITS - DAILY_FREE_CREDITS  # разница за подписку на канал
        async with AsyncSessionFactory() as session:
            user = await get_user(session, user_tg_id)
            if user:
                await add_credits(session, user, bonus)
                await session.commit()
        await callback.answer(f'🎉 +{bonus} кредитов начислено! Спасибо за подписку!', show_alert=True)
    else:
        await callback.answer(f'✅ Подписка подтверждена! С завтра будет {DAILY_CHANNEL_CREDITS} кр/день.', show_alert=True)

    try:
        await callback.message.delete()
    except Exception:
        pass


REFERRAL_MILESTONES = {5: 200, 10: 500}  # invited count -> bonus credits


async def _maybe_grant_referral_bonus(redis, user_tg_id: int, bot) -> None:
    """Выдаёт реферальный бонус при первом сообщении нового пользователя.
    После начисления проверяет milestone (5/10 друзей) и выдаёт доп. бонус."""
    from app.handlers.start import REFERRAL_BONUS, REFERRAL_PENDING_KEY
    key = REFERRAL_PENDING_KEY.format(user_id=user_tg_id)
    referrer_id_bytes = await redis.get(key)
    if not referrer_id_bytes:
        return
    await redis.delete(key)
    referrer_id = int(referrer_id_bytes)
    milestone_bonus = 0
    new_count = 0
    async with AsyncSessionFactory() as session:
        await add_referral_bonus(session, referrer_id, amount=REFERRAL_BONUS)
        # Проверяем достижение milestone
        referrer = await get_user(session, referrer_id)
        if referrer:
            new_count = referrer.referral_bonus  # это счётчик приглашённых
            milestone_bonus = REFERRAL_MILESTONES.get(new_count, 0)
            if milestone_bonus:
                await add_credits(session, referrer, milestone_bonus)
        await session.commit()
    try:
        text = (
            f'🎁 <b>+{REFERRAL_BONUS} кредитов!</b>\n\n'
            'Твой друг написал первое сообщение — бонус зачислен.'
        )
        if milestone_bonus:
            text += (
                f'\n\n🏆 <b>Milestone: {new_count} друзей!</b>\n'
                f'Дополнительно зачислено <b>+{milestone_bonus} кредитов</b>.'
            )
        await bot.send_message(referrer_id, text, parse_mode='HTML')
    except Exception:
        pass


@router.message(F.text & ~F.text.startswith('/'))
async def handle_message(message: Message, state: FSMContext = None, redis=None) -> None:
    """Visual-only pivot: чат отключён. Текст → редирект в фото-воронку.
    ИСКЛЮЧЕНИЕ: если у юзера в FSM-state есть свежий last_result_file_id
    (моложе 10 мин) — трактуем текст как правку последнего результата,
    автоматически перезапускаем _run_edit с тем же фото."""
    user_tg_id = message.from_user.id

    if redis:
        # Реферал-бонус всё ещё выдаём — иначе сломаем реф-воронку
        await _maybe_grant_referral_bonus(redis, user_tg_id, message.bot)

    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return
        if user.is_banned:
            await message.answer('🚫 Ваш аккаунт заблокирован.')
            return

    # Перехват: правка последнего результата текстом (живёт 10 мин в FSM).
    if state is not None:
        import time as _time
        data = await state.get_data()
        last_fid = data.get('last_result_file_id')
        last_at = data.get('last_result_at', 0) or 0
        last_ctx = (data.get('last_prompt_label') or '').strip()
        if last_fid and (_time.time() - float(last_at)) < 600:
            user_text = message.text.strip()
            # Склейка контекста: один шаг назад, явная формулировка «уже
            # применено» → модель не повторяет, а уточняет. Без контекста
            # модель не знала, что «надпись» = твой граффити из прошлого шага.
            if last_ctx:
                refine_prompt = (
                    f'(Previous request, ALREADY APPLIED to this image: '
                    f'«{last_ctx}». Do NOT re-apply it.)\n\n'
                    f'Now apply this REFINEMENT to the same image:\n'
                    f'{user_text}'
                )
            else:
                refine_prompt = user_text
            # Освобождаем слот (одна правка за раз), готовим state для _run_edit
            await state.clear()
            await state.update_data(file_id=last_fid, face_photos=[last_fid])
            from app.states import ImageEditState
            await state.set_state(ImageEditState.waiting_prompt)
            from app.handlers.image_edit import _run_edit
            await _run_edit(message, state, refine_prompt,
                            source_user_id=user_tg_id)
            return

    # Короткий редирект-ответ. Не показываем баланс/нагружающие детали —
    # цель сообщения — вернуть юзера в фото-воронку.
    redirect_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Открыть каталог настроений',
            callback_data='gallery_open',
            icon_custom_emoji_id=PE_FRAME, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Подменить лицо в чужом фото',
            callback_data='face_swap_start',
            icon_custom_emoji_id=PE_ROBOT, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='В главное меню',
            callback_data='main_menu',
            icon_custom_emoji_id=PE_HOME, style='danger',
        )],
    ])
    # Подсказка про правку текстом — упоминаем явно, чтобы юзер знал «как».
    await message.answer(
        '🪄 <b>Я делаю фото с твоим лицом в любой сцене — не общаюсь текстом.</b>\n\n'
        'Открой каталог настроений или пришли селфи 👇\n'
        '<i>Десятки готовых сцен: балет у статуи, старинное авто, Париж, '
        'обложка глянца, неон будущего и не только.</i>\n\n'
        '💡 <i>Сразу после генерации фото можно дописать правку текстом — '
        'например, «убери чашку» или «сделай надпись ярче» — переделаю с тем '
        'же кадром.</i>',
        parse_mode='HTML',
        reply_markup=redirect_kb,
    )

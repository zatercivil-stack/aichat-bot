from __future__ import annotations

import re

import structlog
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import add_credits
from app.database.repo.promos import get_promo, has_redeemed, redeem_promo
from app.database.repo.users import get_user
from app.keyboards.main import back_kb, main_menu_kb
from app.states import PromoState

logger = structlog.get_logger(__name__)
router = Router()

_RATE_KEY = 'promo_rate:{user_id}'
_MAX_ATTEMPTS = 5
_RATE_TTL = 3600  # 1 час
_CODE_RE = re.compile(r'^[A-ZА-ЯЁ0-9_-]{3,32}$')


@router.callback_query(lambda c: c.data == 'enter_promo')
async def cb_enter_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoState.waiting_code)
    # safe_edit_or_answer — кнопка доступна из «Ещё» (text-msg, edit ОК) И
    # из welcome-collage flow (фото-msg, edit_text падает). 24.05 фикс.
    from app.utils import safe_edit_or_answer
    await safe_edit_or_answer(
        callback,
        '🎟 <b>Введи промокод</b>\n\n'
        'Промокод даёт бесплатные кредиты.\n'
        'Просто напиши его в чат:',
        reply_markup=back_kb('settings_menu'),
    )
    await callback.answer()


@router.message(PromoState.waiting_code)
async def handle_promo_input(message: Message, state: FSMContext, redis=None) -> None:
    await state.clear()
    raw = (message.text or '').strip()
    code = raw.upper()

    # If the text clearly isn't a promo code (e.g. plain question with spaces /
    # lowercase / cyrillic words / a slash command), exit gracefully instead of
    # showing a scary error. The state is already cleared — the user can resend.
    looks_like_promo = bool(_CODE_RE.match(code)) and ' ' not in raw and len(raw) <= 32
    if not looks_like_promo:
        await message.answer(
            'ℹ️ Я ждал промокод, но это похоже на обычный вопрос.\n'
            'Промо-ожидание снято — повтори сообщение и я отвечу как обычно. 🤖',
            reply_markup=main_menu_kb(),
        )
        return

    await _activate_promo(message, code, redis)


@router.message(Command('promo'))
async def cmd_promo(message: Message, command: CommandObject, redis=None) -> None:
    code = (command.args or '').strip().upper()
    if not code:
        await message.answer(
            '🎟 <b>Введи промокод</b>\n\n'
            'Напиши: <code>/promo КОД</code>\n'
            'или нажми кнопку в главном меню.',
            parse_mode='HTML',
        )
        return
    await _activate_promo(message, code, redis)


async def _activate_promo(message: Message, code: str, redis) -> None:
    user_tg_id = message.from_user.id

    if not code:
        await message.answer('❌ Промокод не может быть пустым.')
        return

    if not _CODE_RE.match(code):
        await message.answer('❌ Неверный формат промокода.')
        return

    # Rate limiting: не более 5 неверных попыток в час
    if redis:
        rate_key = _RATE_KEY.format(user_id=user_tg_id)
        attempts = await redis.get(rate_key)
        if attempts and int(attempts) >= _MAX_ATTEMPTS:
            await message.answer(
                '🚫 Слишком много попыток. Попробуй через час.',
            )
            return

    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return

        promo = await get_promo(session, code)

        if not promo:
            await _wrong_attempt(message, redis, user_tg_id)
            return

        if promo.max_uses > 0 and promo.used_count >= promo.max_uses:
            await _wrong_attempt(message, redis, user_tg_id)
            return

        if await has_redeemed(session, user.id, promo.id):
            await message.answer('❌ Ты уже активировал этот промокод.')
            return

        # Канал-эксклюзивные коды (SCENE*) — те же гейтинг что в /start deep-link
        if code.startswith('SCENE') and redis:
            try:
                from app.services import channel_service
                if not await channel_service.check(message.bot, redis, user_tg_id):
                    from aiogram.types import InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
                    kb = IM(inline_keyboard=[
                        [IB(text='Открыть @conturD', url='https://t.me/conturD', style='primary')],
                        [IB(text='Я подписался — забрать кредиты',
                            url=f'https://t.me/NeyraChatBot?start=promo_{code}', style='success')],
                    ])
                    await message.answer(
                        f'🔒 Промокод <b>{code}</b> — только для подписчиков '
                        f'@conturD.\nПодпишись и нажми кнопку.',
                        parse_mode='HTML', reply_markup=kb,
                    )
                    return
            except Exception as exc:
                logger.warning('promo_channel_check_failed',
                               code=code, error=str(exc)[:200])

        await add_credits(session, user, promo.credits)
        await redeem_promo(session, user.id, promo)
        await session.commit()
        new_balance = user.credits

    # Сброс счётчика при успехе
    if redis:
        await redis.delete(_RATE_KEY.format(user_id=user_tg_id))

    logger.info('Promo redeemed', code=code, user_id=user_tg_id, credits=promo.credits)

    await message.answer(
        f'✅ <b>Промокод активирован!</b>\n\n'
        f'🎁 Зачислено: <b>+{promo.credits} кредитов</b>\n'
        f'💳 Твой баланс: <b>{new_balance} кредитов</b>\n\n'
        f'Просто напиши мне — я готов помочь 🚀',
        parse_mode='HTML',
        reply_markup=main_menu_kb(),
    )


async def _wrong_attempt(message: Message, redis, user_tg_id: int) -> None:
    if redis:
        rate_key = _RATE_KEY.format(user_id=user_tg_id)
        attempts = await redis.incr(rate_key)
        if attempts == 1:
            await redis.expire(rate_key, _RATE_TTL)
        remaining = _MAX_ATTEMPTS - attempts
        if remaining <= 0:
            await message.answer('❌ Промокод не найден.\n🚫 Лимит попыток исчерпан. Попробуй через час.')
        elif remaining <= 2:
            await message.answer(f'❌ Промокод не найден. Осталось попыток: {remaining}.')
        else:
            await message.answer('❌ Промокод не найден или недействителен.')
    else:
        await message.answer('❌ Промокод не найден или недействителен.')

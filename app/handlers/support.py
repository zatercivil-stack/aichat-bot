from __future__ import annotations

import asyncio
import html

import structlog
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app.database.base import AsyncSessionFactory
from app.database.repo.events import log_event_bg
from app.database.repo.support import (
    add_user_message, close_ticket, get_ticket, open_or_append_ticket,
)
from app.database.repo.users import get_user
from app.services.admin_notify import alert_admins
from app.states import SupportState

logger = structlog.get_logger(__name__)
router = Router()

_INTRO = (
    "🆘 <b>Поддержка</b>\n\n"
    "Опиши одним сообщением, что случилось — можно текстом или приложить фото. "
    "Передам, ответ придёт сюда же."
)
_REPLY_INTRO = (
    "🆘 <b>Поддержка</b>\n\n"
    "Напиши, что добавить к обращению — текстом или фото."
)
_BAN_MSG = "Сейчас не могу принять обращение."
_CANCEL_MSG = "Окей, отменяю. Кнопка поддержки на месте — пиши, если что."
_NEED_MEDIA = "Пришли текст или фото — так я смогу передать обращение."


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Отмена', callback_data='support_cancel')],
    ])


async def _start_intro(target: Message | CallbackQuery, state: FSMContext) -> bool:
    """Общий старт: проверка бана, set_state, интро. True если запустили."""
    user_tg_id = target.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
    if user and user.is_banned:
        if isinstance(target, CallbackQuery):
            await target.message.answer(_BAN_MSG)
        else:
            await target.answer(_BAN_MSG)
        return False
    await state.set_state(SupportState.waiting_message)
    if isinstance(target, CallbackQuery):
        await target.message.answer(_INTRO, parse_mode='HTML', reply_markup=_cancel_kb())
    else:
        await target.answer(_INTRO, parse_mode='HTML', reply_markup=_cancel_kb())
    return True


@router.message(Command('support'))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await _start_intro(message, state)


@router.callback_query(lambda c: c.data == 'support_open')
async def cb_support_open(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_intro(callback, state)
    await callback.answer()


@router.callback_query(lambda c: c.data == 'support_cancel')
async def cb_support_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer(_CANCEL_MSG)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith('support_reply:'))
async def cb_support_reply(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        ticket_id = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    # Проверка владения: кнопка из чужого сообщения не должна дописывать чужой тикет.
    async with AsyncSessionFactory() as session:
        ticket = await get_ticket(session, ticket_id)
        user = await get_user(session, callback.from_user.id)
    if not ticket or not user or ticket.user_id != user.id:
        await callback.answer()
        return
    await state.set_state(SupportState.waiting_message)
    await state.update_data(ticket_id=ticket_id)
    await callback.message.answer(_REPLY_INTRO, parse_mode='HTML', reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith('support_close:'))
async def cb_support_close(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        ticket_id = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    async with AsyncSessionFactory() as session:
        ticket = await get_ticket(session, ticket_id)
        user = await get_user(session, callback.from_user.id)
        if not ticket or not user or ticket.user_id != user.id:
            await callback.answer()
            return
        await close_ticket(session, ticket_id)
        await session.commit()
    await callback.message.answer("Отлично, закрываю обращение. Обращайся, если что.")
    await callback.answer()


async def _process_ticket_submission(
    reply_to: Message, user_tg_id: int, body: str | None,
    file_ids: list[str], existing_ticket_id: int | None,
) -> None:
    """Единая запись обращения (для одиночного сообщения и для альбома).
    Первое фото идёт в одно сообщение с текстом, остальные — доп. записями."""
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await reply_to.answer("Нажми /start для начала работы.")
            return
        if user.is_banned:  # бан мог наступить уже в процессе — перепроверяем
            await reply_to.answer(_BAN_MSG)
            return

        username = user.username
        user_db_id = user.id

        ticket = None
        if existing_ticket_id:
            ticket = await get_ticket(session, existing_ticket_id)
            # Чужой тикет или уже закрытый — не дописываем/не переоткрываем его,
            # уводим в текущий активный (или новый), сохраняя инвариант
            # «один активный тикет на юзера».
            if ticket is None or ticket.user_id != user_db_id or ticket.status == 'closed':
                ticket = None
        if ticket is not None:
            created = False
        else:
            ticket, created = await open_or_append_ticket(session, user_db_id)

        await session.flush()
        is_new = created
        ticket_id = ticket.id

        await add_user_message(session, ticket_id, body, file_ids[0] if file_ids else None)
        for fid in file_ids[1:]:
            await add_user_message(session, ticket_id, None, fid)
        await session.commit()

    log_event_bg(
        user_db_id,
        'support_ticket_created' if is_new else 'support_user_reply',
        ticket_id=ticket_id,
    )

    if is_new:
        await reply_to.answer(f"✅ Обращение №{ticket_id} принято. Отвечу здесь же, как только разберу.")
    else:
        await reply_to.answer(f"✅ Добавлено к обращению №{ticket_id} — поддержка увидит.")

    # Тело юзера экранируем — алерт уходит с parse_mode='HTML' (инъекция/слом разметки).
    preview = html.escape((body or '📎 фото')[:200])
    safe_username = html.escape(username) if username else '—'
    await alert_admins(
        f"🆘 <b>Новое обращение №{ticket_id}</b>\n\n"
        f"Юзер: @{safe_username} (<code>{user_tg_id}</code>)\n"
        f"{preview}\n\n"
        f"<a href=\"http://202.148.55.149:8181/admin/support/{ticket_id}\">Открыть в панели</a>"
    )


# Альбомы (media group) приходят отдельными апдейтами. Буферизуем по
# media_group_id с дебаунсом и собираем все фото в один тикет — иначе первое
# фото уходило в тикет, а остальные (после state.clear) проваливались в
# vision-обработчик и открывали каталог пресетов.
_ALBUM_DEBOUNCE = 1.2
_album_buffers: dict[str, dict] = {}


async def _finalize_album(gid: str) -> None:
    try:
        await asyncio.sleep(_ALBUM_DEBOUNCE)
    except asyncio.CancelledError:
        return  # пришло ещё одно фото альбома — таймер перезапущен
    buf = _album_buffers.pop(gid, None)
    if not buf or not buf['msgs']:
        return
    msgs = buf['msgs']
    state = buf['state']
    data = await state.get_data()
    existing_ticket_id = data.get('ticket_id')
    await state.clear()

    body = next((m.caption for m in msgs if m.caption), None)
    file_ids = [m.photo[-1].file_id for m in msgs if m.photo]
    if not body and not file_ids:
        return
    try:
        await _process_ticket_submission(
            msgs[-1], msgs[-1].from_user.id, body, file_ids, existing_ticket_id,
        )
    except Exception as exc:
        logger.warning('support album finalize failed', gid=gid, error=str(exc)[:200])


@router.message(StateFilter(SupportState.waiting_message), F.media_group_id)
async def handle_ticket_album(message: Message, state: FSMContext) -> None:
    gid = str(message.media_group_id)
    buf = _album_buffers.get(gid)
    if buf is None:
        buf = {'msgs': [], 'state': state, 'task': None}
        _album_buffers[gid] = buf
    buf['msgs'].append(message)
    if buf['task'] is not None:
        buf['task'].cancel()
    buf['task'] = asyncio.create_task(_finalize_album(gid))


@router.message(StateFilter(SupportState.waiting_message), F.text | F.photo)
async def handle_ticket_message(message: Message, state: FSMContext) -> None:
    body = message.text or message.caption
    file_id = message.photo[-1].file_id if message.photo else None

    if not body and not file_id:
        await message.answer(_NEED_MEDIA)
        return  # state не сбрасываем

    data = await state.get_data()
    existing_ticket_id = data.get('ticket_id')
    await state.clear()
    await _process_ticket_submission(
        message, message.from_user.id, body,
        [file_id] if file_id else [], existing_ticket_id,
    )

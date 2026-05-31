from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.bot_config import get_config_value, set_config_value
from app.states import ChannelDraftState

logger = structlog.get_logger(__name__)
router = Router()

ADMIN_ID = 0
_DRAFT_KEY = 'pending_review_post'  # JSON {caption, image_fid, topic, cta_type}


def _is_admin(message: Message) -> bool:
    return message.from_user.id in settings.admin_ids_list or message.from_user.id == ADMIN_ID


def _is_admin_uid(uid: int) -> bool:
    return uid in settings.admin_ids_list or uid == ADMIN_ID


def _draft_kb() -> InlineKeyboardMarkup:
    """Кнопки под черновиком: публикация / правка текста / правка фото / отмена.
    Цвета: зелёный = публикация (главное), синие = правки, красный = отмена."""
    from app.keyboards.main import PE_CHECK, PE_CROSS, PE_FRAME, PE_BUBBLE
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Опубликовать в канал', callback_data='chp:pub',
            icon_custom_emoji_id=PE_CHECK, style='success')],
        [InlineKeyboardButton.model_construct(
            text='Изменить текст', callback_data='chp:txt',
            icon_custom_emoji_id=PE_BUBBLE, style='primary'),
         InlineKeyboardButton.model_construct(
            text='Изменить фото', callback_data='chp:photo',
            icon_custom_emoji_id=PE_FRAME, style='primary')],
        [InlineKeyboardButton.model_construct(
            text='Отмена', callback_data='chp:cancel',
            icon_custom_emoji_id=PE_CROSS, style='danger')],
    ])


async def _load_draft() -> dict | None:
    async with AsyncSessionFactory() as s:
        raw = await get_config_value(s, _DRAFT_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _save_draft(data: dict) -> None:
    async with AsyncSessionFactory() as s:
        await set_config_value(s, _DRAFT_KEY, json.dumps(data))
        await s.commit()


async def _clear_draft() -> None:
    async with AsyncSessionFactory() as s:
        await set_config_value(s, _DRAFT_KEY, '')
        await s.commit()


async def _send_preview(bot, chat_id: int, data: dict) -> str | None:
    """Шлёт превью черновика (фото+подпись) с кнопками. Возвращает актуальный
    image_fid (стабильный file_id из отправленного сообщения)."""
    from app.services.channel_publisher import _to_photo_payload
    caption = data.get('caption', '')
    image = data.get('image_fid')
    kb = _draft_kb()
    payload = _to_photo_payload(image)
    if payload is not None:
        sent = await bot.send_photo(chat_id, payload, caption=caption,
                                    parse_mode='HTML', reply_markup=kb)
        return image if isinstance(image, str) else (
            sent.photo[-1].file_id if sent.photo else None)
    await bot.send_message(chat_id, caption, parse_mode='HTML',
                           reply_markup=kb, disable_web_page_preview=True)
    return None


@router.message(Command("channel_skip"))
async def cmd_channel_skip(message: Message) -> None:
    """Skip the next scheduled post. One-shot."""
    if not _is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_skip_next", "1")
        await session.commit()

    await message.answer(
        "⏭ <b>Следующий пост будет пропущен.</b>\n"
        "Автопубликация возобновится на следующий день.",
        parse_mode="HTML",
    )


@router.message(Command("channel_pause"))
async def cmd_channel_pause(message: Message) -> None:
    """Disable auto-publishing until /channel_resume."""
    if not _is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_paused", "1")
        await session.commit()

    await message.answer(
        "⏸ <b>Автопубликация на @conturD приостановлена.</b>\n"
        "Возобнови командой /channel_resume.",
        parse_mode="HTML",
    )


@router.message(Command("channel_resume"))
async def cmd_channel_resume(message: Message) -> None:
    """Re-enable auto-publishing."""
    if not _is_admin(message):
        return

    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_paused", "0")
        await session.commit()

    await message.answer(
        "▶️ <b>Автопубликация на @conturD возобновлена.</b>\n"
        "Следующий пост — сегодня/завтра в 19:00 МСК (Mon–Sat).",
        parse_mode="HTML",
    )


@router.message(Command("channel_post"))
async def cmd_channel_post(message: Message) -> None:
    """Сгенерировать ЧЕРНОВИК поста и прислать на проверку с кнопками.
    Публикация — только по кнопке (после возможного редактирования). Тема
    опциональна: без неё подберём автоматически."""
    if not _is_admin(message):
        return

    topic = (message.text or "").removeprefix("/channel_post").strip()
    status_msg = await message.answer(
        f"⏳ Генерирую черновик{(' на тему: <b>' + topic + '</b>') if topic else ''}…",
        parse_mode="HTML",
    )
    try:
        from app.services.channel_publisher import _build_post, _pick_topic
        if not topic:
            topic = await _pick_topic(mode='daily')
        full_text, image_source, cta_type, _rm = await _build_post(topic, 'daily')
        data = {'caption': full_text, 'image_fid': None,
                'topic': topic, 'cta_type': cta_type}
        fid = await _send_preview(message.bot, message.from_user.id,
                                  {**data, 'image_fid': image_source})
        data['image_fid'] = fid
        await _save_draft(data)
        await status_msg.edit_text(
            "👆 <b>Черновик готов.</b> Проверь, при нужде измени текст/фото — "
            "и жми «Опубликовать».\n\n"
            "<i>⚠️ Авто-картинка иногда не совпадает с текстом — поэтому есть "
            "кнопка «Изменить фото».</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("channel_post draft failed", error=str(exc)[:300])
        await status_msg.edit_text(
            f"❌ Ошибка генерации: <code>{str(exc)[:300]}</code>",
            parse_mode="HTML",
        )


async def _publish_draft(bot, data: dict) -> int:
    """Публикует черновик в @conturD (silent), пишет в channel_posts, чистит."""
    from app.services.channel_publisher import CHANNEL_ID
    from app.database.models import ChannelPost
    from sqlalchemy import insert as sql_insert
    caption = data.get('caption', '')
    fid = data.get('image_fid')
    if fid:
        sent = await bot.send_photo(chat_id=CHANNEL_ID, photo=fid, caption=caption,
                                    parse_mode='HTML', disable_notification=True)
    else:
        sent = await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                      parse_mode='HTML', disable_web_page_preview=True,
                                      disable_notification=True)
    async with AsyncSessionFactory() as s:
        await s.execute(sql_insert(ChannelPost).values(
            topic=data.get('topic', 'manual'), body=caption,
            cta_type=data.get('cta_type', 'neira_plain'),
            scheduled_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            message_id=sent.message_id, status='published',
        ))
        await set_config_value(s, _DRAFT_KEY, '')
        await s.commit()
    return sent.message_id


@router.callback_query(F.data == 'chp:pub')
async def cb_chp_publish(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_uid(callback.from_user.id):
        return
    data = await _load_draft()
    if not data:
        await callback.answer('Черновик не найден (уже опубликован или отменён)', show_alert=True)
        return
    await callback.answer('Публикую…')
    try:
        mid = await _publish_draft(callback.bot, data)
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            f"✅ <b>Опубликовано в @conturD</b> (msg {mid}).", parse_mode='HTML')
    except Exception as exc:
        logger.error('chp publish failed', error=str(exc)[:300])
        await callback.message.answer(
            f"❌ Ошибка публикации: <code>{str(exc)[:300]}</code>", parse_mode='HTML')


@router.callback_query(F.data == 'chp:txt')
async def cb_chp_edit_text(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_uid(callback.from_user.id):
        return
    if not await _load_draft():
        await callback.answer('Черновик не найден', show_alert=True)
        return
    await state.set_state(ChannelDraftState.waiting_caption)
    await callback.answer()
    await callback.message.answer(
        "✏️ Пришли <b>новый текст</b> поста (HTML-разметку <code>&lt;b&gt;</code> "
        "и т.п. можно прямо в тексте). Я заменю подпись и покажу обновлённый "
        "черновик.", parse_mode='HTML')


@router.callback_query(F.data == 'chp:photo')
async def cb_chp_edit_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_uid(callback.from_user.id):
        return
    if not await _load_draft():
        await callback.answer('Черновик не найден', show_alert=True)
        return
    await state.set_state(ChannelDraftState.waiting_photo)
    await callback.answer()
    await callback.message.answer(
        "🖼 Пришли <b>новое фото</b> (как изображение или файл). "
        "Заменю картинку и покажу обновлённый черновик.", parse_mode='HTML')


@router.callback_query(F.data == 'chp:cancel')
async def cb_chp_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_uid(callback.from_user.id):
        return
    await _clear_draft()
    await state.clear()
    await callback.answer('Отменено')
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("❌ Черновик отменён. В канал ничего не ушло.")


@router.message(ChannelDraftState.waiting_caption)
async def on_new_caption(message: Message, state: FSMContext) -> None:
    if not _is_admin(message):
        return
    raw = (message.text or '').strip()
    if raw.startswith('/'):
        await state.clear()
        await message.answer('Редактирование отменено (черновик без изменений). '
                             'Открой его снова через /channel_post при нужде.')
        return
    new_text = (message.html_text or message.text or '').strip()
    if not new_text:
        await message.answer('Пустой текст — пришли текст поста.')
        return
    data = await _load_draft()
    if not data:
        await state.clear()
        await message.answer('Черновик не найден.')
        return
    data['caption'] = new_text
    await _save_draft(data)
    await state.clear()
    await message.answer('✅ Текст обновлён. Вот черновик:')
    await _send_preview(message.bot, message.from_user.id, data)


@router.message(ChannelDraftState.waiting_photo, F.photo | F.document)
async def on_new_photo(message: Message, state: FSMContext) -> None:
    if not _is_admin(message):
        return
    if message.photo:
        fid = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or '').startswith('image/'):
        fid = message.document.file_id
    else:
        await message.answer('Это не картинка. Пришли фото или картинку-файл.')
        return
    data = await _load_draft()
    if not data:
        await state.clear()
        await message.answer('Черновик не найден.')
        return
    data['image_fid'] = fid
    await _save_draft(data)
    await state.clear()
    await message.answer('✅ Фото обновлено. Вот черновик:')
    await _send_preview(message.bot, message.from_user.id, data)


@router.message(Command("channel_status"))
async def cmd_channel_status(message: Message) -> None:
    """Show current publisher state and last 5 posts."""
    if not _is_admin(message):
        return

    from sqlalchemy import select, desc
    from app.database.models import ChannelPost

    async with AsyncSessionFactory() as session:
        paused_val = await get_config_value(session, "channel_publisher_paused")
        skip_val = await get_config_value(session, "channel_publisher_skip_next")

        result = await session.execute(
            select(ChannelPost)
            .where(ChannelPost.status == "published")
            .order_by(desc(ChannelPost.published_at))
            .limit(5)
        )
        last_posts = result.scalars().all()

    paused = paused_val == "1"
    skip_next = skip_val == "1"

    status_icon = "⏸ Приостановлен" if paused else "▶️ Активен"
    skip_label = " | ⏭ Следующий пропускается" if skip_next else ""

    lines = [
        f"📡 <b>Автопубликация @conturD</b>\n",
        f"Статус: <b>{status_icon}{skip_label}</b>",
        f"Расписание: Mon–Sat, 19:03–19:17 МСК\n",
    ]

    if last_posts:
        lines.append("<b>Последние 5 постов:</b>")
        for post in last_posts:
            pub_dt = post.published_at
            if pub_dt:
                date_str = pub_dt.strftime("%d.%m %H:%M")
            else:
                date_str = "—"
            topic_short = post.topic[:60]
            if len(post.topic) > 60:
                topic_short += "…"
            msg_link = ""
            if post.message_id:
                msg_link = f" <a href=\"https://t.me/conturD/{post.message_id}\">🔗</a>"
            lines.append(f"• {date_str} — {topic_short}{msg_link}")
    else:
        lines.append("Постов ещё нет.")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

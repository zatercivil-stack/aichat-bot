from __future__ import annotations

import json
import random
import re
import uuid
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


def _btn_markup(btn: dict | None) -> InlineKeyboardMarkup | None:
    """Строит инлайн-кнопку-CTA из btn={text,url,icon} (или None)."""
    if not (btn and btn.get('url')):
        return None
    bkw = {'text': btn['text'], 'url': btn['url'], 'style': 'success'}
    if btn.get('icon'):
        bkw['icon_custom_emoji_id'] = btn['icon']
    return InlineKeyboardMarkup.model_construct(
        inline_keyboard=[[InlineKeyboardButton.model_construct(**bkw)]])


async def _publish_post(bot, data: dict) -> int:
    """Шлёт пост в @conturD (silent, опц. инлайн-кнопка) + пишет в channel_posts.
    НЕ трогает хранилище черновиков (общий код для ручного и авто-флоу)."""
    from app.services.channel_publisher import CHANNEL_ID
    from app.database.models import ChannelPost
    from sqlalchemy import insert as sql_insert
    caption = data.get('caption', '')
    fid = data.get('image_fid')
    reply_markup = _btn_markup(data.get('btn'))
    if fid:
        sent = await bot.send_photo(chat_id=CHANNEL_ID, photo=fid, caption=caption,
                                    parse_mode='HTML', disable_notification=True,
                                    reply_markup=reply_markup)
    else:
        sent = await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                      parse_mode='HTML', disable_web_page_preview=True,
                                      disable_notification=True, reply_markup=reply_markup)
    async with AsyncSessionFactory() as s:
        await s.execute(sql_insert(ChannelPost).values(
            topic=data.get('topic', 'manual'), body=caption,
            cta_type=data.get('cta_type', 'neira_plain'),
            scheduled_at=datetime.now(timezone.utc),
            published_at=datetime.now(timezone.utc),
            message_id=sent.message_id, status='published',
        ))
        await s.commit()
    return sent.message_id


async def _publish_draft(bot, data: dict) -> int:
    """Публикует одиночный ручной черновик (/channel_post) и очищает его слот."""
    mid = await _publish_post(bot, data)
    await _clear_draft()
    return mid


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


# ──────────────────────────────────────────────────────────────────────────
# Авто-генератор постов (gpt-5.4-mini): 3×/день по 2 варианта на одобрение.
# Чередует режимы: «бесплатные кредиты» (футер +кр) ↔ «инлайн-кнопка» (credits=0).
# Премиум-эмодзи всегда. САМ В КАНАЛ НЕ ПУБЛИКУЕТ — только по тапу владельца.
# Несколько черновиков живут одновременно (очередь по id), каждый одобряется
# отдельно — в отличие от одиночного ручного слота _DRAFT_KEY.
# ──────────────────────────────────────────────────────────────────────────

_AUTO_KEY = 'auto_post_drafts'          # JSON {draft_id: data}
_PE_SPARKLES = '5325547803936572038'    # ✨ — иконка кнопки-CTA


async def _load_auto() -> dict:
    async with AsyncSessionFactory() as s:
        raw = await get_config_value(s, _AUTO_KEY)
    try:
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


async def _save_auto(d: dict) -> None:
    async with AsyncSessionFactory() as s:
        await set_config_value(s, _AUTO_KEY, json.dumps(d))
        await s.commit()


def _auto_kb(draft_id: str) -> InlineKeyboardMarkup:
    from app.keyboards.main import PE_CHECK, PE_CROSS
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton.model_construct(
            text='Опубликовать', callback_data=f'apc:pub:{draft_id}',
            icon_custom_emoji_id=PE_CHECK, style='success'),
        InlineKeyboardButton.model_construct(
            text='Удалить', callback_data=f'apc:del:{draft_id}',
            icon_custom_emoji_id=PE_CROSS, style='danger'),
    ]])


async def run_auto_post_drafts(bot, n_variants: int = 2) -> None:
    """Генерит N вариантов поста через GPT и шлёт владельцу на одобрение.
    Чередует credits-режим (чётный индекс) и button-режим (нечётный)."""
    from app.services.gpt_post import generate_post, is_enabled
    if not is_enabled():
        logger.info('auto_post skipped: OPENAI_API_KEY пуст')
        return
    from app.services.channel_publisher import (
        assemble_manual_post, _create_post_promo, _neyra_link, _link_signature,
        _fix_markdown_bold)
    from app.handlers.image_edit import EDIT_PRESETS
    from app.database.repo.preset_previews import get_preview_file_id
    from app.utils import premium_text
    from sqlalchemy import text as _sqltext

    # пресеты с готовым превью (есть file_id) — из них выбираем образ
    async with AsyncSessionFactory() as s:
        rows = await s.execute(_sqltext(
            "SELECT DISTINCT preset_key FROM preset_previews"))
        preview_keys = [r[0] for r in rows.fetchall() if r[0] in EDIT_PRESETS]
    if not preview_keys:
        logger.warning('auto_post: нет пресетов с превью')
        return
    random.shuffle(preview_keys)

    drafts = await _load_auto()
    sent = 0
    for i in range(n_variants):
        preset = preview_keys[i % len(preview_keys)]
        label = re.sub(r'^\W+', '', EDIT_PRESETS[preset][0]).strip()
        body = await generate_post(label)
        if not body:
            continue
        async with AsyncSessionFactory() as s:
            fid = await get_preview_file_id(s, preset)
        credits_mode = (i % 2 == 0)
        if credits_mode:
            # «бесплатные кредиты»: assemble добавит футер с +кр (премиум в теле)
            caption, fid2, cta_type, _promo = await assemble_manual_post(
                body, preset, mode='feature')
            fid = fid2 or fid
            btn = None
            mode_label = 'бесплатные кредиты'
        else:
            # «инлайн-кнопка»: credits=0, кнопка-CTA под постом, без футера-бонуса
            promo = await _create_post_promo(preset_key=preset, credits=0)
            # тот же порядок пост-обработки, что в assemble_manual_post:
            # markdown→HTML → премиум-эмодзи → подпись-ссылка
            caption = _link_signature(premium_text(_fix_markdown_bold(body)))
            cta_type = 'neira_feature'
            btn = {'text': 'Хочу такой кадр со своим лицом',
                   'url': _neyra_link(promo), 'icon': _PE_SPARKLES}
            mode_label = 'инлайн-кнопка'

        did = uuid.uuid4().hex[:8]
        data = {'caption': caption, 'image_fid': fid, 'topic': f'auto:{preset}',
                'cta_type': cta_type, 'btn': btn}
        drafts[did] = data
        # превью владельцу: реальный вид + отдельное сообщение с управлением
        try:
            await bot.send_photo(ADMIN_ID, fid, caption=caption, parse_mode='HTML',
                                 reply_markup=_btn_markup(btn))
            await bot.send_message(
                ADMIN_ID,
                f"🤖 Авто-черновик «{label}» · режим: <b>{mode_label}</b>. Публикуем?",
                parse_mode='HTML', reply_markup=_auto_kb(did))
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.error('auto_post send failed', error=str(exc)[:200])
            drafts.pop(did, None)
    await _save_auto(drafts)
    logger.info('auto_post done', sent=sent)


@router.message(Command("autopost"))
async def cmd_autopost(message: Message) -> None:
    """Ручной триггер авто-генератора (та же пачка, что по расписанию)."""
    if not _is_admin(message):
        return
    await message.answer("⏳ Генерирую 2 варианта через gpt-5.4-mini…")
    await run_auto_post_drafts(message.bot, n_variants=2)


# ──────────────────────────────────────────────────────────────────────────
# Полностью АВТОМАТИЧЕСКИЙ постинг: генерит ОДИН пост и публикует СРАЗУ в
# @conturD, без одобрения в личку. Безопасен по mismatch: пост строится
# preset-first (текст пишется ПРО конкретный пресет, картинка = превью того же
# пресета), поэтому фото всегда совпадает с текстом — в отличие от старого
# _build_post, где тема и картинка выбирались независимо.
# ──────────────────────────────────────────────────────────────────────────

_LAST_PRESET_KEY = 'auto_publish_last_preset'  # чтобы не повторять пресет подряд


async def run_auto_post_publish(bot, mode: str = 'feature') -> None:
    """Генерит один пост (gpt-5.4-mini на OPENAI_API_KEY) и сразу публикует его
    в @conturD. Уважает паузу/skip (общие флаги с /channel_pause /channel_skip).
    mode: 'feature' (+100 кр в CTA) утром / 'daily' (+50 кр) вечером."""
    from app.services.gpt_post import generate_post, is_enabled
    if not is_enabled():
        logger.info('auto_publish skipped: OPENAI_API_KEY пуст')
        return

    # пауза / разовый пропуск — те же конфиг-флаги, что у ручных команд
    async with AsyncSessionFactory() as s:
        paused = await get_config_value(s, 'channel_publisher_paused')
        skip = await get_config_value(s, 'channel_publisher_skip_next')
        last = await get_config_value(s, _LAST_PRESET_KEY)
    if paused == '1':
        logger.info('auto_publish: paused')
        return
    if skip == '1':
        async with AsyncSessionFactory() as s:
            await set_config_value(s, 'channel_publisher_skip_next', '0')
            await s.commit()
        logger.info('auto_publish: skip_next (one-shot)')
        return

    from app.services.channel_publisher import assemble_manual_post
    from app.handlers.image_edit import EDIT_PRESETS
    from sqlalchemy import text as _sqltext

    # пресеты с готовым превью (file_id уже есть → fal.ai не тратится)
    async with AsyncSessionFactory() as s:
        rows = await s.execute(_sqltext(
            "SELECT DISTINCT preset_key FROM preset_previews"))
        preview_keys = [r[0] for r in rows.fetchall() if r[0] in EDIT_PRESETS]
    if not preview_keys:
        logger.warning('auto_publish: нет пресетов с превью')
        return
    # не повторяем тот же образ два поста подряд (если есть выбор)
    pool = [k for k in preview_keys if k != last] or preview_keys
    preset = random.choice(pool)

    label = re.sub(r'^\W+', '', EDIT_PRESETS[preset][0]).strip()
    body = await generate_post(label)
    if not body:
        logger.warning('auto_publish: пустой текст от gpt', preset=preset)
        return

    caption, image_fid, cta_type, _promo = await assemble_manual_post(
        body, preset, mode=mode)
    if not image_fid:
        logger.warning('auto_publish: нет превью у пресета', preset=preset)
        return

    data = {'caption': caption, 'image_fid': image_fid,
            'topic': f'auto:{preset}', 'cta_type': cta_type, 'btn': None}
    try:
        mid = await _publish_post(bot, data)
        async with AsyncSessionFactory() as s:
            await set_config_value(s, _LAST_PRESET_KEY, preset)
            await s.commit()
        logger.info('auto_publish done', preset=preset, mid=mid, mode=mode)
    except Exception as exc:  # noqa: BLE001
        logger.error('auto_publish failed', preset=preset, error=str(exc)[:300])


@router.message(Command("autopublish"))
async def cmd_autopublish(message: Message) -> None:
    """Ручной триггер ПОЛНОГО автопостинга: сгенерить и сразу опубликовать один
    пост в @conturD (для проверки расписания без ожидания слота)."""
    if not _is_admin(message):
        return
    await message.answer("⏳ Генерирую и публикую один пост в @conturD…")
    await run_auto_post_publish(message.bot, mode='feature')
    await message.answer("✅ Готово (проверь @conturD).")


@router.callback_query(F.data.startswith('apc:pub:'))
async def cb_apc_publish(callback: CallbackQuery) -> None:
    if not _is_admin_uid(callback.from_user.id):
        return
    did = callback.data.split(':', 2)[2]
    drafts = await _load_auto()
    data = drafts.get(did)
    if not data:
        await callback.answer('Черновик уже использован или удалён', show_alert=True)
        return
    await callback.answer('Публикую…')
    try:
        mid = await _publish_post(callback.bot, data)
        drafts.pop(did, None)
        await _save_auto(drafts)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await callback.message.answer(
            f"✅ <b>Опубликовано в @conturD</b> (msg {mid}).", parse_mode='HTML')
    except Exception as exc:  # noqa: BLE001
        logger.error('apc publish failed', error=str(exc)[:300])
        await callback.message.answer(
            f"❌ Ошибка публикации: <code>{str(exc)[:300]}</code>", parse_mode='HTML')


@router.callback_query(F.data.startswith('apc:del:'))
async def cb_apc_delete(callback: CallbackQuery) -> None:
    if not _is_admin_uid(callback.from_user.id):
        return
    did = callback.data.split(':', 2)[2]
    drafts = await _load_auto()
    if drafts.pop(did, None) is not None:
        await _save_auto(drafts)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await callback.answer('Удалено')

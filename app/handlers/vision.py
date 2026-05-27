from __future__ import annotations

import io
from datetime import date
import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.base import AsyncSessionFactory
from app.database.repo.credits import (
    DAILY_CHANNEL_CREDITS,
    DAILY_FREE_CREDITS,
    MODEL_COSTS,
    add_credits,
    deduct_credits,
    grant_daily_if_needed,
)
from app.database.repo.events import log_event_bg
from app.database.repo.users import get_user
from app.handlers.chat import LOW_CREDITS_NUDGE, _CHANNEL_NUDGE_KB
from app.keyboards.main import (
    PE_AU_GOLD, PE_BACK, PE_CARD, PE_ROBOT, PE_SPARKLES, PE_SPEAKER,
    buy_or_invite_kb,
)
from app.services import channel_service, context_service, vision_service
from app.states import ImageEditState
from app.utils import edit_or_send_long, markdown_to_html

logger = structlog.get_logger(__name__)
router = Router()

VISION_COST = MODEL_COSTS['vision']            # 10 кр
EDIT_COST = MODEL_COSTS['image_edit']          # 70 кр
FACE_SWAP_COST = MODEL_COSTS['face_swap']      # 100 кр

# Глаголы-маркеры edit-намерения в caption к фото. Если присутствуют — выделяем
# кнопку "Изменить" как primary, чтобы юзер не промахнулся.
_EDIT_HINT_WORDS = (
    'убери', 'удали', 'добавь', 'замени', 'поменяй', 'преврати',
    'сделай', 'поставь', 'стилизуй', 'нарисуй', 'дорисуй', 'одень',
    'переодень', 'свапни', 'свап', 'face swap',
)


def _detect_edit_intent(caption: str) -> bool:
    if not caption:
        return False
    low = caption.lower()
    return any(w in low for w in _EDIT_HINT_WORDS)


async def _photo_action_kb(edit_primary: bool = False, page_idx: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура после загрузки фото:
      [ пресеты 2×N ]
      [ Чужое лицо · X кр ] [ Описать ]   ← спец-действия, всегда видны
      [ ◀ Назад | · K/N · | Вперёд ▶ ]    ← пагинация — последний навигационный ряд
    Лейблы и страницы — из image_edit.EDIT_PRESETS / PRESET_PAGES."""
    from app.handlers.image_edit import EDIT_PRESETS, PRESET_PAGES, _nav_row
    from app.services.pricing import effective_cost
    eff_swap = await effective_cost('face_swap')

    def _btn(key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=EDIT_PRESETS[key][0], callback_data=f'edit_preset:{key}')

    page_idx = max(0, min(page_idx, len(PRESET_PAGES) - 1))
    page = PRESET_PAGES[page_idx]

    rows: list[list[InlineKeyboardButton]] = []
    items = page['items']
    for i in range(0, len(items), 2):
        row = [_btn(items[i])]
        if i + 1 < len(items):
            row.append(_btn(items[i + 1]))
        rows.append(row)

    # Спец-действия — компактно в две колонки, под сеткой пресетов
    # Функция «Описать фото» удалена — оставили только face-swap как
    # альтернативное действие под фото.
    rows.append([
        InlineKeyboardButton.model_construct(
            text=f'🎭 Подставить чужое лицо · {eff_swap} кр',
            callback_data='photo_action:face_swap',
            icon_custom_emoji_id=PE_ROBOT,
        ),
    ])

    # Навигация — самым последним рядом, чтобы визуально замкнуть меню
    rows.append(_nav_row(page_idx))
    return InlineKeyboardMarkup(inline_keyboard=rows)

_BUY_KB = buy_or_invite_kb()


async def _start_edit_from_uploaded(
    message: Message, state: FSMContext, file_id: str,
    user_tg_id: int, caption: str, source_label: str,
) -> None:
    """Общая логика для фото/файла, присланного из главного меню (без активного
    flow). Если в caption явный edit-запрос — сразу запускаем edit. Иначе —
    сохраняем file_id как `reuse_file_id` и открываем НОВЫЙ каталог категорий.
    Юзер выбирает пресет из карточки → cb_gallery_try видит reuse_file_id и
    применяет к этому фото БЕЗ повторной загрузки селфи."""
    edit_intent = _detect_edit_intent(caption) if caption else False

    # Шорткат: caption-команда → запускаем редактирование сразу, один проход
    if edit_intent and caption:
        await state.clear()
        from app.handlers.image_edit import _run_edit
        await state.set_state(ImageEditState.waiting_prompt)
        await state.update_data(file_id=file_id)
        await _run_edit(message, state, caption, source_user_id=user_tg_id)
        return

    # Голый upload (или не-edit caption) → новый каталог + reuse_file_id.
    # state=waiting_prompt сохраняем, чтобы свободный текст («сделай высокую
    # моду») тоже работал в течение PHOTO_AUTO_EDIT_TTL_S. Кнопка пресета из
    # каталога пойдёт через cb_gallery_try → ветка reuse_file_id → _run_edit
    # без повторной просьбы прислать селфи.
    import time
    await state.clear()
    await state.set_state(ImageEditState.waiting_prompt)
    await state.update_data(
        file_id=file_id,
        reuse_file_id=file_id,        # ключевое: подхватит cb_gallery_try
        face_photos=[],
        mface_status_msg_id=None,
        pending_gallery_preset=None,
        last_photo_caption=caption,
        last_photo_file_id=file_id,
        photo_session_started_at=time.time(),
    )

    from app.handlers.image_edit import open_catalog_from_message
    intro = (
        f'📸 <b>Принял {source_label}</b>\n\n'
        '<i>Выбери настроение — применю к этому фото, заново слать не нужно. '
        'Или просто напиши, что сделать («сделай в высокой моде», «убери фон»).</i>'
    )
    await open_catalog_from_message(message, user_tg_id, intro_text=intro)


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, redis=None) -> None:
    """Бара-фото из главного меню → новый каталог (категории) с reuse_file_id.
    Поддерживается шорткат: caption с edit-запросом → запускаем сразу."""
    user_tg_id = message.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return
        if user.is_banned:
            await message.answer('🚫 Ваш аккаунт заблокирован.')
            return

    file_id = message.photo[-1].file_id
    caption = (message.caption or '').strip()
    await _start_edit_from_uploaded(
        message, state, file_id, user_tg_id, caption, source_label='фото',
    )


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext, redis=None) -> None:
    """Файл из главного меню (фото-документ — iPhone .heic, RAW и т.п.).
    Раньше документ без активного flow не ловился ни одним хендлером и просто
    игнорировался. Теперь — тот же путь, что и фото: reuse_file_id + новый
    каталог. Не-картинные документы (.pdf, .zip) аккуратно отказываем."""
    doc = message.document
    if not doc or not doc.mime_type or not doc.mime_type.startswith('image/'):
        await message.answer(
            'ℹ️ Я работаю только с картинками. Пришли фото (как изображение или файл).'
        )
        return

    user_tg_id = message.from_user.id
    async with AsyncSessionFactory() as session:
        user = await get_user(session, user_tg_id)
        if not user:
            await message.answer('Нажми /start для начала работы.')
            return
        if user.is_banned:
            await message.answer('🚫 Ваш аккаунт заблокирован.')
            return

    caption = (message.caption or '').strip()
    await _start_edit_from_uploaded(
        message, state, doc.file_id, user_tg_id, caption, source_label='файл',
    )


# Функция «Описать фото» (vision-анализ) удалена. Кнопка с callback'ом
# photo_action:describe больше не показывается, handler удалён. Если когда-то
# понадобится вернуть — восстановить из git-истории.

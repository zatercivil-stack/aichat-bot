from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PromoState(StatesGroup):
    waiting_code = State()


class ImageState(StatesGroup):
    waiting_prompt = State()


class ImageEditState(StatesGroup):
    waiting_photo = State()         # ждём фото для редактирования
    waiting_prompt = State()        # фото загружено, ждём текст
    waiting_face_base = State()     # face swap: ждём целевую фотку (сцену)
    waiting_face_source = State()   # face swap: ждём фотку с лицом
    waiting_face_prompt = State()   # face swap: опциональное уточнение


class ChannelDraftState(StatesGroup):
    """Редактирование черновика канал-поста перед публикацией (админ)."""
    waiting_caption = State()       # ждём новый текст поста
    waiting_photo = State()         # ждём новое фото поста

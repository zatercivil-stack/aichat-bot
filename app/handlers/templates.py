"""Prompt templates — quick-start helpers for common tasks.

Triggered via /templates command or 'templates' callback from the main menu.
Each template shows a starter prompt the user can copy or extend.
"""
from __future__ import annotations

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.keyboards.main import PE_BACK, PE_HOME

logger = structlog.get_logger(__name__)
router = Router()


TEMPLATES: list[dict] = [
    {
        'id': 'resume',
        'label': '📄 Помоги с резюме',
        'prompt': (
            '📄 <b>Помогаю с резюме</b>\n\n'
            'Расскажи коротко:\n'
            '— На какую должность подаёшься?\n'
            '— Твой опыт (сколько лет / где / что делал)\n'
            '— Главные сильные стороны (в свободной форме)\n\n'
            'Я предложу структуру и помогу с формулировками.'
        ),
    },
    {
        'id': 'email',
        'label': '✉️ Написать письмо',
        'prompt': (
            '✉️ <b>Помогаю с письмом</b>\n\n'
            'Скажи:\n'
            '— Кому пишешь (роль, отношения)\n'
            '— Цель письма (что нужно от адресата)\n'
            '— Тон (формальный / тёплый / нейтральный)\n\n'
            'Сделаю черновик, ты доработаешь.'
        ),
    },
    {
        'id': 'explain',
        'label': '💡 Объясни простыми словами',
        'prompt': (
            '💡 <b>Объясняю простыми словами</b>\n\n'
            'Напиши тему, которую нужно разобрать.\n'
            'Я объясню так, чтобы было понятно ребёнку или новичку — '
            'с аналогиями, без лишних терминов.'
        ),
    },
    {
        'id': 'summary',
        'label': '📋 Сделай выжимку',
        'prompt': (
            '📋 <b>Делаю выжимку</b>\n\n'
            'Пришли мне текст (статью, главу, разговор) — '
            'я выделю главное в 5–10 пунктов и добавлю короткий вывод.'
        ),
    },
    {
        'id': 'code',
        'label': '🧑‍💻 Помоги с кодом',
        'prompt': (
            '🧑‍💻 <b>Помогаю с кодом</b>\n\n'
            'Опиши, что хочешь сделать (или пришли существующий код).\n'
            'Скажи язык/фреймворк, если знаешь.\n\n'
            'Помогу написать, разобрать ошибку или оптимизировать.'
        ),
    },
    {
        'id': 'plan',
        'label': '🗓 Составь план',
        'prompt': (
            '🗓 <b>Составляю план</b>\n\n'
            'Расскажи:\n'
            '— Чего хочешь добиться (цель)\n'
            '— К какому сроку\n'
            '— Сколько времени можешь уделять\n\n'
            'Я разобью на этапы и предложу первые шаги.'
        ),
    },
    {
        'id': 'gift',
        'label': '🎁 Подбери подарок',
        'prompt': (
            '🎁 <b>Подбираю подарок</b>\n\n'
            'Скажи:\n'
            '— Кому (возраст, увлечения)\n'
            '— Бюджет\n'
            '— Повод (если есть)\n\n'
            'Дам несколько идей разного формата.'
        ),
    },
    {
        'id': 'translate',
        'label': '🌐 Переведи',
        'prompt': (
            '🌐 <b>Перевожу</b>\n\n'
            'Пришли текст и укажи язык (или просто пришли — определю сам).\n'
            'Сделаю естественный перевод, без машинного звучания.'
        ),
    },
]


def _templates_kb() -> InlineKeyboardMarkup:
    rows = []
    # 2 buttons per row
    for i in range(0, len(TEMPLATES), 2):
        row = [
            InlineKeyboardButton(text=t['label'], callback_data=f'tpl:{t["id"]}')
            for t in TEMPLATES[i:i + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton.model_construct(
        text='В меню', callback_data='main_menu',
        icon_custom_emoji_id=PE_HOME, style='danger',
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_TEMPLATES_HEADER = (
    '📚 <b>Шаблоны быстрого старта</b>\n\n'
    'Выбери, с чем помочь — пришлю готовую заготовку, по которой '
    'просто напишешь свой ответ. Можно использовать когда не знаешь, '
    'как сформулировать вопрос.'
)


@router.message(Command('templates'))
async def cmd_templates(message: Message) -> None:
    await message.answer(_TEMPLATES_HEADER, reply_markup=_templates_kb())


@router.callback_query(lambda c: c.data == 'templates')
async def cb_templates(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_TEMPLATES_HEADER, reply_markup=_templates_kb())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith('tpl:'))
async def cb_template_pick(callback: CallbackQuery) -> None:
    tpl_id = callback.data.split(':', 1)[1]
    tpl = next((t for t in TEMPLATES if t['id'] == tpl_id), None)
    if not tpl:
        await callback.answer('Шаблон не найден', show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Другие шаблоны', callback_data='templates',
            icon_custom_emoji_id=PE_BACK,
        )],
        [InlineKeyboardButton.model_construct(
            text='Главное меню', callback_data='main_menu',
            icon_custom_emoji_id=PE_HOME, style='danger',
        )],
    ])
    await callback.message.answer(tpl['prompt'], reply_markup=kb)
    await callback.answer()

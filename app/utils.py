from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone

from aiogram.types import Message

TG_LIMIT = 4096

# ── МСК для business-логики (юзеры в России) ────────────────────────────────
# Контейнер живёт по UTC. Прямой date.today() / datetime.utcnow() даёт «вчера»
# для юзера с 00:00 до 02:59 МСК → daily-бонус не выдаётся, streak врёт,
# top-up за подписку не находит флаг. Все «новый день/вчера» решения для
# юзера должны идти через эти helpers.
MSK_TZ = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    """Текущее МСК как aware datetime."""
    return datetime.now(MSK_TZ)


def today_msk() -> date:
    """Сегодняшняя МСК-дата (date)."""
    return now_msk().date()


def yesterday_msk() -> date:
    """Вчерашняя МСК-дата — для сравнения streak_last_date."""
    return today_msk() - timedelta(days=1)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian pluralization. Example: plural_ru(5, 'ответ', 'ответа', 'ответов') -> 'ответов'."""
    n = abs(n)
    if n % 100 in (11, 12, 13, 14):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


_RE_FENCED = re.compile(r'```([a-zA-Z0-9_+\-]*)\n?(.*?)```', re.DOTALL)
_RE_INLINE = re.compile(r'`([^`\n]+)`')
_RE_BOLD = re.compile(r'\*\*([^*\n]+?)\*\*')
_RE_BOLD_UNDER = re.compile(r'(?<!_)__([^_\n]+?)__(?!_)')
_RE_ITALIC_STAR = re.compile(r'(?<![\w*])\*([^*\n]+?)\*(?![\w*])')
_RE_ITALIC_UNDER = re.compile(r'(?<![\w_])_([^_\n]+?)_(?![\w_])')
_RE_STRIKE = re.compile(r'~~([^~\n]+?)~~')
_RE_HEADER = re.compile(r'^#{1,6}\s+(.+?)\s*#*\s*$', re.MULTILINE)
_RE_LINK = re.compile(r'\[([^\]\n]+?)\]\(([^)\s]+?)\)')


def markdown_to_html(text: str) -> str:
    """Convert AI markdown reply to Telegram-safe HTML.

    Escapes <, >, & first so model output can't break the parser, then maps
    **bold**, *italic*, `code`, ```fenced```, ~~strike~~, # headers,
    and [link](url) into Telegram-supported HTML tags.
    """
    if not text:
        return text

    blocks: list[str] = []
    inlines: list[str] = []

    def stash_block(match: re.Match) -> str:
        blocks.append(match.group(2))
        return f'\x00B{len(blocks) - 1}\x00'

    text = _RE_FENCED.sub(stash_block, text)

    def stash_inline(match: re.Match) -> str:
        inlines.append(match.group(1))
        return f'\x00C{len(inlines) - 1}\x00'

    text = _RE_INLINE.sub(stash_inline, text)

    text = html.escape(text, quote=False)

    text = _RE_LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = _RE_BOLD.sub(r'<b>\1</b>', text)
    text = _RE_BOLD_UNDER.sub(r'<b>\1</b>', text)
    text = _RE_STRIKE.sub(r'<s>\1</s>', text)
    text = _RE_ITALIC_STAR.sub(r'<i>\1</i>', text)
    text = _RE_ITALIC_UNDER.sub(r'<i>\1</i>', text)
    text = _RE_HEADER.sub(r'<b>\1</b>', text)

    text = re.sub(
        r'\x00C(\d+)\x00',
        lambda m: f'<code>{html.escape(inlines[int(m.group(1))], quote=False)}</code>',
        text,
    )
    text = re.sub(
        r'\x00B(\d+)\x00',
        lambda m: f'<pre>{html.escape(blocks[int(m.group(1))], quote=False)}</pre>',
        text,
    )
    return text


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split text into chunks ≤ limit chars at paragraph/line/word boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        pos = text.rfind('\n\n', 0, limit)
        if pos == -1:
            pos = text.rfind('\n', 0, limit)
        if pos == -1:
            pos = text.rfind(' ', 0, limit)
        if pos == -1:
            pos = limit
        chunks.append(text[:pos].rstrip())
        text = text[pos:].lstrip()
    return chunks


async def safe_edit_or_answer(callback, text: str, reply_markup=None, parse_mode: str = 'HTML') -> None:
    """Безопасное обновление сообщения, к которому привязан callback.

    Проблема: с welcome-коллажем главное меню — это PHOTO с caption (нет text).
    `callback.message.edit_text(...)` на фото-сообщении падает с
    "Bad Request: there is no text in the message to edit" → handler крашится →
    юзер видит, что кнопка «не нажимается».

    Логика: если сообщение текстовое — edit_text; если caption (photo/video) —
    edit_caption; иначе (или при любой ошибке) — удалить старое и отправить
    новое. Так одна и та же кнопка работает и на тексте, и на фото-меню."""
    from aiogram.exceptions import TelegramBadRequest
    msg = callback.message
    try:
        if msg.text is not None:
            await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        if msg.caption is not None:
            await msg.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
    except TelegramBadRequest:
        pass
    except Exception:
        pass
    # Фолбэк: удалить старое + новое сообщение (всегда сработает).
    try:
        await msg.delete()
    except Exception:
        pass
    await msg.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def send_long(message: Message, text: str, reply_markup=None, parse_mode: str = 'HTML') -> None:
    """Send potentially long text, splitting into multiple messages if needed."""
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        kb = reply_markup if i == len(chunks) - 1 else None
        await message.answer(chunk, parse_mode=parse_mode, reply_markup=kb)


async def edit_or_send_long(status_msg: Message, message: Message, text: str, reply_markup=None) -> None:
    """Edit status message with first chunk; send overflow as new messages."""
    chunks = split_text(text)
    try:
        await status_msg.edit_text(chunks[0], parse_mode='HTML',
                                   reply_markup=reply_markup if len(chunks) == 1 else None)
    except Exception:
        await message.answer(chunks[0], parse_mode='HTML',
                             reply_markup=reply_markup if len(chunks) == 1 else None)
    for i, chunk in enumerate(chunks[1:], 1):
        kb = reply_markup if i == len(chunks) - 1 else None
        await message.answer(chunk, parse_mode='HTML', reply_markup=kb)


# ─── Premium emoji helpers ────────────────────────────────────────────────────

PREMIUM_EMOJI: dict[str, str] = {
    '🥇': '5346258552849713801', '💎': '5346258552849713801', '🏆': '5346258552849713801',
    '⚡': '5258203794772085854', '⚡️': '5258203794772085854',
    '🌍': '5256041734005090333',
    '🚀': '5368654333397199941',
    '🔥': '5445088653728568921',
    '✨': '5325547803936572038',
    '🎁': '5440502878491930005',
    '🛡': '5458847521331768461', '🔒': '5458847521331768461',
    '💳': '5213403875670765022',
    '❌': '5465665476971471368',
    '✅': '5190782321577441092', '✔️': '5190782321577441092',
    '📱': '5357380130144535965',
    '⬅': '5456187398977247949', '⬅️': '5456187398977247949',
    '◀️': '5256247952564825322',
    '▶️': '5255835489675519149',
    '🔓': '5429405838345265327',
    '⏱': '5458640241915084025', '⏱️': '5458640241915084025', '⏰': '5458640241915084025', '⏳': '5458640241915084025',
    '🤖': '6147670072706995009',
    '🗣': '5397849751548884255',
    '🎉': '5461151367559141950',
    '📅': '5413879192267805083', '🗓': '5413879192267805083',
    '💰': '5445256208992718797', '💸': '5445256208992718797',
    '🤝': '5352795355635276043',
    '👋': '5947404268909629849',
    '🌇': '5870528297091206639',  # закатный город — для городских тем
    '🎭': '5359441070201513074',  # театр-маски — перевоплощения, сцены-образы
    '🌒': '5188666899860298925',  # тёмный полумесяц — магия, мистика
}

_SORTED_EMOJI_KEYS = sorted(PREMIUM_EMOJI.keys(), key=len, reverse=True)


def pe(emoji: str) -> str:
    """Wrap a single emoji in a premium tg-emoji tag if known; otherwise return as-is."""
    eid = PREMIUM_EMOJI.get(emoji)
    if not eid:
        return emoji
    return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>'


def premium_text(text: str) -> str:
    """Replace all known emojis in text with premium tg-emoji tags."""
    if not text:
        return text
    result = text
    for emoji in _SORTED_EMOJI_KEYS:
        result = result.replace(emoji, pe(emoji))
    return result

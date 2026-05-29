from __future__ import annotations

import random
from datetime import datetime, timezone

import structlog
from aiogram import Bot
from openai import AsyncOpenAI

import re

from aiogram.types import BufferedInputFile

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.repo.bot_config import get_config_value, set_config_value
from app.services.channel_topics import CHANNEL_TOPICS
from app.services.channel_feature_topics import FEATURE_TOPICS
from app.services import fal_image_service, openrouter_image_service
from app.services.image_service import generate_image as _flux_generate_image
from app.utils import premium_text


def _fix_markdown_bold(text: str) -> str:
    """Safety net: convert any **markdown bold** AI accidentally emits into <b>...</b>.
    Does NOT escape existing HTML tags — AI is instructed to output HTML directly."""
    return re.sub(r'\*\*([^*\n]+?)\*\*', r'<b>\1</b>', text)


# Канал-сигнатура → гиперссылка на сам канал. При репостах остаётся кликабельной.
_CHANNEL_SIGNATURE_URL = 'https://t.me/conturD'
# Подпись = текущее короткое имя канала (26.05: «🟣 Нейра», не «Контур доступа»).
# Матчим оба варианта на случай если AI выдаст старый — оба линкуем/нормализуем.
_SIGNATURE_RE = re.compile(r'(🟣\s*Нейра|😈\s*Контур доступа)(?!\s*</a>)')


def _link_signature(text: str) -> str:
    """Оборачивает подпись «🟣 Нейра» в гиперссылку на канал.
    Если AI забыл подпись — добавляет её в конец.

    Дополнительно: всё что AI вывел ПОСЛЕ подписи — мусор (комментарии,
    thinking-traces, второй пост). Безусловно режем — после подписи в посте
    не должно быть ничего, кроме CTA, который мы добавляем сами уже после.
    """
    m = _SIGNATURE_RE.search(text)
    if m:
        # Берём всё до подписи + саму подпись (linkified, новое имя), хвост режем
        before = text[:m.start()].rstrip()
        return f'{before}\n\n<a href="{_CHANNEL_SIGNATURE_URL}">🟣 Нейра</a>'
    # AI забыл подпись — добавляем
    return text.rstrip() + f'\n\n<a href="{_CHANNEL_SIGNATURE_URL}">🟣 Нейра</a>'


def _extract_image_prompt(raw: str) -> tuple[str, str | None]:
    """Strip the trailing 'IMAGE_PROMPT: ...' line. Returns (body, image_prompt)."""
    m = re.search(r'\n\s*IMAGE_PROMPT:\s*(.+?)\s*$', raw, re.DOTALL)
    if not m:
        return raw, None
    body = raw[:m.start()].rstrip()
    image_prompt = m.group(1).strip()
    return body, image_prompt


def _extract_prompt_copy(raw: str) -> tuple[str, str | None]:
    """Legacy. PROMPT_COPY мы больше не используем (юзеру некуда вставлять
    промпт в Нейре — есть только пресеты). Парсер оставлен ради backward-compat
    если AI всё-таки выдаст эту строку — режем и игнорим."""
    m = re.search(r'\n\s*PROMPT_COPY:\s*([^\n]+)\s*$', raw, re.MULTILINE)
    if not m:
        return raw, None
    body = (raw[:m.start()] + raw[m.end():]).rstrip()
    return body, m.group(1).strip()


def _extract_preset_key(raw: str) -> tuple[str, str | None]:
    """Извлекает строку 'PRESET: <key>' из AI-ответа. Возвращает (тело, key).

    key — ключ пресета из EDIT_PRESETS (vogue / cinema / retro_90s / ...).
    Если AI вернул unknown key — игнорируем (вернётся None).
    """
    from app.handlers.image_edit import EDIT_PRESETS
    m = re.search(r'\n\s*PRESET:\s*([a-z_0-9]+)\s*$', raw, re.MULTILINE)
    if not m:
        return raw, None
    key = m.group(1).strip().lower()
    body = (raw[:m.start()] + raw[m.end():]).rstrip()
    if key not in EDIT_PRESETS:
        logger.warning('AI gave unknown preset_key', key=key)
        return body, None
    return body, key


def _infer_preset_from_body(body: str) -> str | None:
    """Fallback: если AI забыл вернуть PRESET строку в neyra-посте, пытаемся
    найти упоминание пресета по labels в самом теле поста.

    Пример: пост написан про Vogue → label '✨ Vogue' → значимое слово 'vogue' →
    находим в body.lower() → возвращаем preset_key='vogue'.

    Это лучше чем retry AI (экономия токенов и времени) и лучше чем random
    fallback (который дал бы рассинхрон между текстом поста и кнопкой).
    """
    from app.handlers.image_edit import EDIT_PRESETS
    body_lower = body.lower()
    # Сначала пробуем точный матч по сложным русским названиям
    # (label вида '🌹 Полночь в Париже', '🩰 Балет на ступенях' и т.п.)
    for key, (label, _) in EDIT_PRESETS.items():
        # Берём 2-3-словные фразы из label (без эмодзи)
        clean = re.sub(r'[^\w\s\-]', '', label).strip().lower()
        if len(clean) >= 5 and clean in body_lower:
            return key
    # Fallback: одиночные слова длиннее 4 символов
    for key, (label, _) in EDIT_PRESETS.items():
        for word in re.findall(r'[A-Za-zА-Яа-я\-]{5,}', label):
            if word.lower() in body_lower:
                return key
    return None


# Telegram-лимит для caption под медиа = 1024 символа. Чтобы пост гарантированно
# уходил одним сообщением (фото + текст), общая длина не должна превышать этот лимит.
TG_CAPTION_LIMIT = 1024


# Sanitizer: фильтрует подозрительные служебные/thinking-строки которые модель
# иногда эмитит вне основного поста. Видели в @conturD: «Krilling...»,
# «Session Status: current», «Image: Что изображено на этом скриншоте?...».
# Эти паттерны выглядят как заглушки thinking-trace, status-line или префиксы
# vision-промпта. Реальный пост такого никогда не содержит — режем без сомнений.
_AI_NOISE_PATTERNS = (
    re.compile(r'^\s*(?:k|К)rilling\.{2,}.*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:📊|📈|📉)?\s*Session\s+Status\s*:.*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:🌅|🖼|📷)?\s*Image\s*:\s*Что изображено.*$', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*(?:Thinking|Reasoning|Thought)\s*:.*$', re.IGNORECASE | re.MULTILINE),
    # Любая «…ing…» строка из ≤2 слов с многоточием — типичный thinking-маркер
    re.compile(r'^\s*\w{3,15}ing\.{2,}\s*$', re.MULTILINE),
)


def _strip_ai_noise(text: str) -> tuple[str, list[str]]:
    """Удаляет служебные/thinking-строки из AI-ответа. Возвращает (чистый_текст, [что_удалили]).
    Каждое срабатывание логируется — если будут новые шаблоны, увидим в логах."""
    removed: list[str] = []
    cleaned = text
    for pat in _AI_NOISE_PATTERNS:
        matches = pat.findall(cleaned)
        if matches:
            removed.extend(m.strip()[:80] for m in matches if m.strip())
            cleaned = pat.sub('', cleaned)
    # Схлопываем 3+ переводов строки в 2 (после удаления могут остаться пустоты)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned, removed


def _fit_to_caption(full_text: str) -> str:
    """Если итоговый текст превышает caption-лимит — режем body максимально
    щадяще, сохраняя CTA-блок и **последний абзац основного поста** (это блок
    SO WHAT — самый продающий, главный вывод).

    Стратегия:
    1. Отделяем CTA-трейлинг (после `\\n\\n—\\n`)
    2. В body ищем последний абзац (после последнего `\\n\\n`)
    3. Считаем budget = LIMIT - tail - last_paragraph
    4. Режем СЕРЕДИНУ body, оставляя начало (заголовок + hook) и конец (so what)
    5. Если ничего не помогает — обрезаем по границе абзаца с края.

    Лучше потерять кусок body чем отправить рваный пост.
    """
    if len(full_text) <= TG_CAPTION_LIMIT:
        return full_text

    # Разрезаем на body / трейлинг (CTA + подпись).
    parts = full_text.rsplit('\n\n—\n', 1)
    if len(parts) == 2:
        body, tail = parts[0], '\n\n—\n' + parts[1]
    else:
        body, tail = full_text, ''

    budget = TG_CAPTION_LIMIT - len(tail)
    if budget <= 0:
        return full_text[:TG_CAPTION_LIMIT]
    if len(body) <= budget:
        return body + tail

    # Защищаем последний абзац (SO WHAT) — это главный продающий блок.
    paragraphs = body.split('\n\n')
    if len(paragraphs) >= 3:
        last_para = paragraphs[-1]
        head = '\n\n'.join(paragraphs[:-1])
        sep = '\n\n…\n\n'
        # Сколько влезает в head чтобы итог = head_cut + sep + last_para + tail ≤ limit
        head_budget = budget - len(last_para) - len(sep)
        if head_budget > 100 and len(last_para) < budget * 0.6:
            # Режем head по границе абзаца
            head_cut = head[:head_budget]
            last_para_in_head = head_cut.rfind('\n\n')
            if last_para_in_head > head_budget * 0.3:
                head_cut = head_cut[:last_para_in_head]
            return head_cut.rstrip() + sep + last_para + tail

    # Fallback: тупо режем по последнему влезающему абзацу.
    cut = body[:budget - 1]  # -1 на «…»
    last_para_break = cut.rfind('\n\n')
    if last_para_break > budget * 0.5:
        cut = cut[:last_para_break]
    return cut.rstrip() + '…' + tail

logger = structlog.get_logger(__name__)

CHANNEL_ID = "@conturD"
ADMIN_ID = 0

# AI system prompt: каркас постов канала «🟣 Нейра».
# Главная цель — посты должны быть НАТИВНОЙ воронкой к нашим продуктам
# (Aurum VPN, AI-фото бот «Нейра») и одновременно интересными к прочтению.
# Структура — формула 5 блоков (HOOK → STORY → MECHANIC → SO WHAT → CTA),
# проверенная для лонгридов в TG: цепляет за секунду, объясняет за минуту,
# нативно подводит к действию.
_SYSTEM_PROMPT = """Ты — автор Telegram-канала «🟣 Нейра», фотостудии для тех кто хочет получать вау-результат без фотошопа и студий. Твой формат — короткий tactical пост: один сильный кейс / приём + конкретный совет что попробовать прямо сейчас.

ГОЛОС: энтузиаст-практик. Пробуешь сам, делишься находкой как с другом. Не «давайте разберём», а «вчера наткнулся, держи». Уверенный, без официоза, с авторским мнением. Можно лёгкая ирония.

═══════════════════════════════════════════════
ЯЗЫК — МАКСИМАЛЬНО ПРОСТОЙ (КРИТИЧНО):
═══════════════════════════════════════════════

Канал читают ОБЫЧНЫЕ люди, не фотографы и не айтишники. Пиши так, чтобы понял каждый с первого раза.

ЗАПРЕЩЕНО (вообще не используй):
- Английские слова и термины: fisheye, low angle, wide lens, anamorphic, chiaroscuro, bokeh, grain, flash, midtones, RGB, teal, grade, editorial, shallow depth, rim light и любые подобные.
- Профессиональный жаргон фото/кино/AI: «контровый свет», «цветокоррекция», «динамический диапазон», «фокусное расстояние», «экспозиция» и т.п.
- Названия-фичи на английском («Identity Lock» и пр.) — объясняй простыми словами: «лицо в кадре остаётся точно твоим, а не похожего человека».

КАК НАДО:
- Вместо «fisheye-угол с бочкообразным искажением» → «кадр будто снят широким объективом впритык — ты крупно, а машина уходит вдаль».
- Вместо «warm flash, 35mm grain» → «тёплый свет и лёгкая плёночная шероховатость, как на старых фото».
- Любой приём описывай через ЧУВСТВО и РЕЗУЛЬТАТ, а не через термин: что человек увидит и почему это круто.

Правило проверки: если слово не сказала бы твоя мама — замени на простое.

═══════════════════════════════════════════════
ПРОДУКТЫ — нативно встраивай в текст:
═══════════════════════════════════════════════

- <b>Нейра</b> (@NeyraChatBot) — фотостудия в Telegram. Юзер шлёт селфи и
  выбирает готовый пресет → получает фото в этой сцене с СВОИМ лицом.
  Лицо в кадре в точности твоё. Большой каталог готовых сцен (десятки).
  Цена: <b>99₽ за 1500 кредитов</b> (~15 сцен), новичкам <b>+500 кр в подарок</b>.

- <b>Aurum</b> (@Shop_Aurum_Bot) — VPN для тех кому нужно пользоваться
  AI-сервисами с геоблоком (Midjourney, Sora, ChatGPT Plus и т.п. в РФ).

═══════════════════════════════════════════════
КАК ВЫБИРАТЬ ПРЕСЕТ ИЗ КАТАЛОГА НЕЙРЫ:
═══════════════════════════════════════════════

КРИТИЧНО ВАЖНО: у Нейры есть готовый каталог пресетов. Юзер НЕ вводит
кастомные промпты вручную (этой опции нет в UI). Он ТАПАЕТ кнопку и
получает пресет. Поэтому ВСЕ neyra-посты должны рассказывать про
КОНКРЕТНЫЙ пресет из списка ниже, а не про абстрактный приём.

ДОСТУПНЫЕ ПРЕСЕТЫ — выбери ОДИН, наиболее близкий к теме:

__PRESETS_BLOCK__

ЕСЛИ ТЕМА ПРО AI-ФОТО → выбери ОДИН пресет → в финальной строке вывода:
  PRESET: <key>
(например `PRESET: vogue` или `PRESET: retro_90s`)

Если ни один пресет НЕ подходит к теме (например тема про VPN или общая
AI-новость) — НЕ пиши PRESET строку, оставь пустой.

═══════════════════════════════════════════════
ВОРОНКА — как писать AI-фото пост:
═══════════════════════════════════════════════

Пост = демо одного пресета. Структура:

1. Hook — почему эта сцена/образ цепляет (простыми словами, бьёт в эмоцию)
2. Что получит человек — как он будет выглядеть, какое настроение у кадра (без терминов)
3. ⚙️ В чём фишка — 1 простое объяснение почему получается круто (по-человечески, не технически)
4. ЗАКРЫТИЕ → короткая фраза-обещание результата («одно селфи — и ты в этой сцене за минуту»)

ВАЖНО ПРО ЗАКРЫТИЕ:
- Под постом НЕТ кнопки. НИКОГДА не пиши «жми кнопку», «жми ссылку под постом»,
  «нажми ниже», «тапни» — этого нет, юзер запутается.
- НЕ упоминай слова «ссылка», «кнопка», «промокод» и НЕ пиши URL. Кликабельная
  CTA-строка добавится кодом сама в самом конце поста.
- НИКОГДА не предлагай копировать промпт — такой опции нет.
- Твоё закрытие — просто короткая фраза-обещание результата. Пример:
  «Одно селфи — и ты в этой сцене уже через минуту». И всё.

ЗАПРЕТЫ ПО ТОНУ:
- «Стоит отметить», «в современном мире», «невероятно важно», «поговорим о», «давайте разберём», «безусловно», «нельзя не упомянуть» — НИКОГДА.
- Никаких «мы с вами», «друзья», «дорогие подписчики». Обращайся на «ты».
- Не выдумывай статистики и даты. Конкретика только проверяемая.
- «Москвичка Кира, 90 секунд, 25 000₽» в 10 раз лучше «многие люди».

═══════════════════════════════════════════════
КОНТРАКТ ЗАГОЛОВКА:
═══════════════════════════════════════════════

Тема — обещание. Пост его ВЫПОЛНЯЕТ:

- «N признаков / способов / ошибок» → ровно N пунктов в посте. Не 3, не 6.
- «Как сделать X» → конкретный алгоритм или один приём с шагами.
- «Почему X» → прямой ответ на «почему», без ухода в сторону.

Заголовок можно цеплять переформулировать (даже жёстко), но смысл обещания обязан сохраниться.

═══════════════════════════════════════════════
ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (4 БЛОКА, СТРОГО):
═══════════════════════════════════════════════

[1] ЗАГОЛОВОК · 1 строка
Эмодзи + <b>ЗАГОЛОВОК В ВЕРХНЕМ РЕГИСТРЕ</b>

[2] HOOK · 1-2 коротких предложения
Сильное открытие. Без префикса-эмодзи. Без воды. Без «Сегодня расскажу…»,
«Привет, друзья…». Сразу бьёт.

ИСПОЛЬЗУЙ ОДИН ИЗ 4 ПРОВЕРЕННЫХ ШАБЛОНОВ HOOK:

  (а) КАРТИНКА-РЕЗУЛЬТАТ — сразу ставит читателя ВНУТРЬ кадра.
      Пример: «Ты в смокинге, рядом — два доббермана. За спиной — красный Ferrari.»

  (б) КОНТР-ИНТУИТИВНЫЙ ФАКТ — ломает ожидание.
      Пример: «Чтобы выглядеть как с обложки, селфи делать не надо.
      Достаточно одного — но не того, что ты обычно постишь.»

  (в) ЛЮБОПЫТСТВО / GAP — даёт половину ответа, остальное в посте.
      Пример: «Есть один пресет, который держится в топе третий день подряд.
      Ни один новый его не сместил.»

  (г) ПРЯМОЙ ВЫЗОВ — короткий вопрос, требующий действия.
      Пример: «А сегодня ты кто? Гонщик на капоте Porsche или святой
      с короной из черепов?»

Не повторяй один шаблон два поста подряд — чередуй.

[3] CORE · сердце поста (выбери ОДИН формат под тему):

  Вариант A — список «N приёмов / признаков / шагов»:
     ✦ короткая подводка в 1 строку (без префикса)
     → <b>Ключевое слово</b> — раскрытие 1-2 предложения с конкретикой
     (ровно N пунктов — следуй контракту заголовка)

  Вариант B — кейс / образ:
     ✦ префикс не нужен
     2-3 связных предложения: что за сцена, как человек в ней выглядит, какое настроение.
     Можно ОДИН подблок «⚙️ В чём фишка» — простое объяснение почему кадр цепляет (без терминов).

[4] ЗАКРЫТИЕ · 1 строка, обязательно
Префикс: →
Короткая фраза-обещание результата: «Одно селфи — и ты в этой сцене за минуту». НЕ упоминай ссылки, кнопки, клики, промокоды — кликабельный CTA добавится кодом сразу под текстом.

[5] ПОДПИСЬ · в новой строке
🟣 Нейра

═══════════════════════════════════════════════
ДИНАМИКА ФОРМАТИРОВАНИЯ — обязательно:
═══════════════════════════════════════════════

Используй РАЗНЫЕ HTML-теги. Один тип = скучный пост. 2-4 типа = живой:
- <b>жирный</b> — ключевые слова, цифры, выводы. 4-8 раз.
- <i>курсив</i> — авторская ремарка, нюанс, цитата. 1-3 раза.
- <code>моноширинный</code> — только цена или короткая цифра-якорь (например <code>99₽</code>). 0-1 раз. НЕ для английских слов.
- <s>зачёркнутый</s> — устаревший миф или «то как было». 0-1 раз.
- <tg-spoiler>спойлер</tg-spoiler> — интригующая деталь, цифра-якорь. 0-1 раз.

Эмодзи — в заголовке и в начале блоков-секций. Внутри текста — 0-1 эмодзи на абзац (не пестри).

═══════════════════════════════════════════════
ПРИМЕР 1 — вариант B (кейс):
═══════════════════════════════════════════════
Тема: «Один промпт превращает обычное селфи в Polaroid 80-х»

📷 <b>ТВОЁ СЕЛФИ — КАК ФОТО ИЗ 90-Х</b>

<i>Помнишь снимки из родительского альбома? Тёплые, чуть выцветшие, живые.</i>

Пресет <b>«Плёнка 90-х»</b> делает ровно такой кадр с твоим лицом: мягкий тёплый свет, лёгкая шероховатость как на старой плёнке, цвета чуть уходят в жёлтое. Не холодное цифровое фото, а тёплое и душевное — будто его правда сняли двадцать лет назад.

⚙️ В чём фишка: обычные фильтры просто красят готовое фото сверху. Тут картинка собирается заново — поэтому выглядит по-настоящему плёночной, а не «фото с фильтром».

→ Одно селфи — и ты в кадре из 90-х уже через минуту.

🟣 Нейра
PRESET: retro_90s

═══════════════════════════════════════════════
ПРИМЕР 2 — вариант A (список):
═══════════════════════════════════════════════
Тема: «Что делает селфи похожим на кадр из фильма»

🎬 <b>ПОЧЕМУ КАДР ИЗ ФИЛЬМА ВЫГЛЯДИТ ДОРОГО, А СЕЛФИ — НЕТ</b>

Дело не в дорогой камере. Дело в нескольких вещах, которые ты замечаешь на любом киноплакате — даже не задумываясь.

✦ Что отличает «киношный» кадр:

→ <b>Свет сбоку</b> — лицо наполовину в тени. Сразу появляется драма и объём.
→ <b>Размытый фон</b> — взгляд держится на тебе, а не на том что сзади.
→ <b>Глубокие тени</b> — не плоско и пересвечено, а с настроением.
→ <b>Спокойный, чуть холодный цвет</b> — как в серьёзном кино, без «телефонной» пестроты.

Пресет <b>«Кино-нуар»</b> собирает всё это сам — тебе остаётся только прислать селфи.

→ Одно селфи — и ты выглядишь как с киноплаката.

🟣 Нейра
PRESET: cinema

═══════════════════════════════════════════════
ЗАПРЕТЫ (КРИТИЧНО):
═══════════════════════════════════════════════

ФАКТ-ОШИБКИ:
- НЕ утверждай, что какой-то сервис «работает» или «не работает» в РФ.
- НЕ называй процент замедления YouTube/конкретных сервисов (выдумано).
- НЕ заявляй «WireGuard работает» / «Shadowsocks работает» / «VPN протокол X не блокируется».
- НЕ выдавай списки «что работает / что не работает».

ПОЛИТИКА:
- НЕ критикуй и не хейти государство РФ, правительство, ведомства, законы, политиков, ФСБ, МВД, Роскомнадзор, суды, президента.
- НЕ обсуждай блокировки/ограничения в негативном политическом ключе.
- НЕ призывай к нарушению законов.
- НЕ затрагивай острые политические темы (война, оппозиция, выборы).

ФОРМАТИРОВАНИЕ:
- Telegram парсит HTML, НЕ markdown. Только <b>, <i>, <s>, <u>, <code>, <tg-spoiler>.
- НЕ используй **звёздочки** или __подчёркивания__.

ЛИМИТ ДЛИНЫ — КРИТИЧНО:
- Telegram-caption под фото = 1024 символа. Минус CTA (~120) и подпись.
- У тебя <b>СТРОГО до 800 символов</b> на весь ответ (заголовок + тело + подпись 😈).
- Целься в <b>500-700 символов</b> — короткий и плотный пост читают целиком,
  длинный — пролистывают. AI-студия = быстрая ценность за один скролл.
- Превысишь 800 → пост обрежется. Лучше короче, чем длиннее.

СЛУЖЕБНЫЙ МУСОР — НЕ ВЫВОДИ:
- Никаких thinking-фраз: «Thinking…», «Reasoning…», «Processing…», «Generating…», «Krilling…».
- Никаких статус-строк: «Session Status», «Status:», «State:».
- Никаких префиксов: «Image:», «Description:», «Reply:», «Output:», «Готово:», «Вот пост:».
- Никаких вопросов самой себе.

═══════════════════════════════════════════════
ВЫВОД — финальные строки:
═══════════════════════════════════════════════

После подписи 🟣 Нейра ДОБАВЬ служебные строки:

1) PRESET: <key>  (опционально, ТОЛЬКО для AI-фото постов)
   Один из ключей из списка пресетов выше. Если не подходит — не пиши.

2) IMAGE_PROMPT: <строка>  (ОБЯЗАТЕЛЬНО)
   1 предложение на английском с визуальным описанием поста.
   Абстрактный стиль, без текста и логотипов, без узнаваемых лиц и брендов.

Пример AI-фото пост:
🟣 Нейра
PRESET: retro_90s
IMAGE_PROMPT: warm vintage polaroid photo aesthetic, soft film grain, retro 80s color palette, abstract no faces

Пример НЕ-фото пост (VPN/общий AI):
🟣 Нейра
IMAGE_PROMPT: cinematic neon city skyline at night, abstract, no text

Возвращай ТОЛЬКО текст поста + (опционально) PRESET + IMAGE_PROMPT."""

# Image style suffix — keeps visual consistent across channel
_IMAGE_STYLE = "minimalist digital illustration, dark theme, neon blue and purple accents, abstract, high contrast, no text, professional"

# ── Tracked deep links ───────────────────────────────────────────────────
# Промокоды-маркеры для атрибуции трафика из канала. Юзер кликает →
# /start promo_X → бонус начисляется, источник виден в /admin/analytics.
NEYRA_CHANNEL_DAILY_PROMO = 'CHAN_DAILY'        # ежедневные посты, +50 кр
NEYRA_CHANNEL_FEATURE_PROMO = 'CHAN_FEAT'       # фишечные посты Ср/Вс, +100 кр

def _neyra_link(promo_code: str) -> str:
    return f'https://t.me/NeyraChatBot?start=promo_{promo_code}'

def _aurum_link(source: str) -> str:
    """Прямая deep-link на Aurum-бот. Aurum-сторона ловит source в /start args,
    записывает в users.traffic_source + логирует event 'traffic_landed' в events.
    Видно в БД Aurum: SELECT traffic_source, COUNT(*) FROM users GROUP BY traffic_source."""
    return f'https://t.me/Shop_Aurum_Bot?start={source}'


# CTA footer options for DAILY posts (evening): random rotation, mild promo.
# 24.05: усилены — активный глагол («забрать», «получить»), конкретный
# результат-обещание, бонус как награда. Раньше был мягкий «попробовать»,
# что плохо конвертит. Сильнее цеплять = больше кликов = больше выручки.
_CTA_OPTIONS_DAILY: list[tuple[str, str | None]] = [
    ("aurum_daily",
     "—\n🛡 <a href=\"" + _aurum_link('chan_daily') +
     "\">VPN, который не палится — Aurum</a>"),
    ("neira_daily",
     "—\n🎁 <a href=\"" + _neyra_link(NEYRA_CHANNEL_DAILY_PROMO) +
     "\"><b>Забрать +50 кр и сделать это фото со своим лицом → в Нейре</b></a>"),
    ("none", None),
]

# Ключевые слова темы → какой продукт релевантнее для CTA.
# Если тема явно про VPN/блокировки/доступ → подсовываем Aurum (это VPN-бот),
# чтобы не ставить ссылку на фото-бот «Нейра» под VPN-постом.
# Если тема про AI/фото/нейросети → ссылка на Нейру (наш фото-бот).
_VPN_KEYWORDS = (
    'vpn', 'впн', 'блокиров', 'обход', 'доступ к', 'роскомнадзор', 'ркн',
    'прокс', 'shadowsock', 'wireguard', 'tor', 'трафик', 'провайдер',
    'замедл', 'youtube', 'discord', 'whatsapp', 'инстагра', 'фейсбук',
    'meta', 'instagram', 'facebook', 'twitter', 'сервис', 'утечк',
    'приватн', 'анонимн', 'шифров', 'dns', 'dpi',
)
_AI_KEYWORDS = (
    'нейросет', 'нейронк', 'ai', 'аи ', 'gpt', 'claude', 'llm', 'midjourney',
    'фото', 'селфи', 'портрет', 'аватар', 'image', 'картинк', 'промпт',
    'prompt', 'midj', 'flux', 'sora', 'banana', 'lora', 'stable diff',
    'face swap', 'фейс', 'персонаж',
)


def _topic_category(topic: str) -> str:
    """Определяет столп темы: 'vpn'/'neyra'/'ai'/'privacy'/'other'.
    Если тема в одном из 4 пулов TOPICS_BY_PILLAR — возвращает имя столпа.
    Иначе fallback на keyword-сканирование (для legacy-тем).
    """
    from app.services.channel_topics import get_pillar_for_topic
    pillar = get_pillar_for_topic(topic)
    if pillar:
        return pillar
    # Fallback: если тема не в пуле (например из FEATURE_TOPICS или передана
    # вручную через /channel_post), классифицируем по ключевым словам.
    t = topic.lower()
    if any(k in t for k in _VPN_KEYWORDS):
        return 'vpn'
    if any(k in t for k in _AI_KEYWORDS):
        return 'neyra'  # AI-фото темы → Нейра
    return 'other'


def _cta_for_topic(topic: str) -> tuple[str, str | None]:
    """CTA по столпу темы.

    После переориентации канала на 100% AI-фото (neyra-pillar) — почти все
    темы попадают в neyra и получают per-post промо-ссылку на Нейру. Это
    максимизирует внутреннюю воронку: каждый пост = +50 кр клик-через.

    Ветки сохранены для legacy/ручных постов через /channel_post с не-neyra
    темой (vpn → Aurum, ai/other → neyra fallback вместо random, чтобы
    промо-слот не пустовал).
    """
    category = _topic_category(topic)
    if category in ('vpn', 'privacy'):
        return _CTA_OPTIONS_DAILY[0]  # aurum_daily
    # Все остальные категории (neyra/ai/other) → Нейра. CTA `none` больше не
    # выпадает автоматически — рекламный слот всегда занят своей воронкой.
    return _CTA_OPTIONS_DAILY[1]  # neira_daily

# CTA footer for FEATURE posts (Ср + Вс): всегда жирная промо Нейры с большим
# бонусом — это и есть смысл «фишечного» поста.
_CTA_FOOTER_FEATURE = (
    "—\n🎁 <a href=\"" + _neyra_link(NEYRA_CHANNEL_FEATURE_PROMO) +
    "\"><b>Попробовать в Нейре → +100 кр в подарок</b></a>"
)


def _strip_label_emoji(label: str) -> str:
    """«🏎 На капоте купе» → «На капоте купе». Убирает ведущий эмодзи и пробел,
    чтобы имя пресета чисто легло внутрь «кавычек» в тексте ссылки."""
    return re.sub(r'^\W+', '', label).strip()


def _neyra_cta_footer(cta_type: str, preset_key: str | None, promo_code: str) -> str:
    """Единственная кликабельная CTA-ссылка поста. Ведёт СРАЗУ в нужный пресет
    (deeplink promo_<code> где code=CHAN_<PRESET>_*, start.py открывает edit-flow
    с этим пресетом) и называет пресет по имени — юзер понимает куда попадёт.

    Без inline-кнопок: это гиперссылка в теле поста, переносится при репостах
    и видна в превью канала.
    """
    url = _neyra_link(promo_code)
    bonus = '+100 кр' if cta_type == 'neira_feature' else '+50 кр'
    if preset_key:
        from app.handlers.image_edit import EDIT_PRESETS
        label = EDIT_PRESETS.get(preset_key, ('', ''))[0]
        clean = _strip_label_emoji(label)
        if clean:
            # Вся строка — одна кликабельная ссылка (ярче, выше CTR). Первое лицо
            # «Хочу … со своим лицом» + явный подарок.
            return (f'—\n🎁 <a href="{url}"><b>Хочу «{clean}» со своим лицом '
                    f'→ забрать {bonus} в подарок</b></a>')
    return (f'—\n🎁 <a href="{url}"><b>Хочу такой кадр со своим лицом '
            f'→ забрать {bonus} в подарок</b></a>')

# ── Секретные промо ─────────────────────────────────────────────────────
# ~15% постов получают спрятанный под спойлером промокод на Нейру.
# Уникальный, на 24 часа, лимит 50 активаций. Виральный mechanic —
# юзер тапает спойлер, видит код, тестит в боте.
SECRET_PROMO_CHANCE = 0.15
SECRET_PROMO_CREDITS = 75
SECRET_PROMO_MAX_USES = 50
SECRET_PROMO_TTL_HOURS = 24


async def _maybe_inject_secret_promo(post_text: str) -> str:
    """С вероятностью SECRET_PROMO_CHANCE генерит уникальный промокод и
    встраивает его в конец поста под <tg-spoiler>.

    Если пост уже близок к caption-лимиту 1024 (с учётом spoiler-блока
    ~230 симв) — пропускаем инжект. Лучше пост без promo чем рваный пост.
    """
    if random.random() > SECRET_PROMO_CHANCE:
        return post_text
    # Spoiler-блок весит ~230 символов. Оставляем 50 запас.
    if len(post_text) > TG_CAPTION_LIMIT - 280:
        logger.info('skip secret promo: post too long', post_len=len(post_text))
        return post_text
    try:
        from app.database.models import PromoCode
        from datetime import timedelta
        code = f'SECRET{random.randint(1000, 9999)}'
        expires = datetime.now(timezone.utc) + timedelta(hours=SECRET_PROMO_TTL_HOURS)
        async with AsyncSessionFactory() as session:
            session.add(PromoCode(
                code=code, credits=SECRET_PROMO_CREDITS,
                max_uses=SECRET_PROMO_MAX_USES,
                expires_at=expires, is_active=True,
            ))
            await session.commit()
        spoiler = (
            f'\n\n<tg-spoiler>🤫 <b>Секретный промокод дня:</b> <code>{code}</code>\n'
            f'+{SECRET_PROMO_CREDITS} кр в <a href="https://t.me/NeyraChatBot?start=promo_{code}">@NeyraChatBot</a> · '
            f'действует {SECRET_PROMO_TTL_HOURS} ч · только первые {SECRET_PROMO_MAX_USES} человек</tg-spoiler>'
        )
        logger.info('Secret promo injected', code=code, credits=SECRET_PROMO_CREDITS)
        return post_text + spoiler
    except Exception as exc:
        logger.warning('Secret promo injection failed', error=str(exc)[:200])
        return post_text


def _get_openrouter_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://t.me/",
            "X-Title": "Kontur AI Publisher",
        },
    )


# Лимит ответа AI (без IMAGE_PROMPT-хвоста): тело поста + подпись 😈.
# При его превышении делается ретрай с явной инструкцией сократиться.
# 850 = 800 (target) + 50 запас под округление AI.
_AI_BODY_TARGET = 750  # AI-студия: короткие плотные посты лучше работают


def _strip_image_prompt_for_length(text: str) -> str:
    """Вырезает строку IMAGE_PROMPT: ... чтобы считать длину только основного
    тела поста (AI всё равно вернёт её в конце ответа)."""
    m = re.search(r'\n\s*IMAGE_PROMPT:', text)
    return text[:m.start()].rstrip() if m else text


async def _generate_post(topic: str, web_context: str = '') -> str:
    """Generate a channel post via Claude Sonnet through OpenRouter.

    Если AI превысил _AI_BODY_TARGET — делаем 1 ретрай с конкретной обратной
    связью о длине и просьбой переписать строго в лимит. Это сильно снижает
    случаи когда _fit_to_caption режет пост на середине предложения.

    web_context: опциональный блок с результатами Brave-поиска.
    Добавляется в system-prompt чтобы AI оперировал реальными свежими фактами
    вместо галлюцинированных цифр и дат.
    """
    client = _get_openrouter_client()
    # 24.05: список пресетов подставляем ДИНАМИЧЕСКИ из EDIT_PRESETS, а не
    # хардкодом — иначе свежие custom-пресеты (доберманы, дайнер-постер и т.д.)
    # никогда не выбирались бы AI для постов. Раньше пропадали 19 новых сцен.
    from app.handlers.image_edit import EDIT_PRESETS
    presets_block = '\n'.join(f'  {k} — {EDIT_PRESETS[k][0]}' for k in EDIT_PRESETS)
    system_msg = _SYSTEM_PROMPT.replace('__PRESETS_BLOCK__', presets_block)
    if web_context:
        system_msg += (
            '\n\n═══════════════════════════════════════════════\n'
            'ВЕБ-КОНТЕКСТ (используй эти факты в посте):\n'
            '═══════════════════════════════════════════════\n'
            + web_context +
            '\n\nИспользуй цифры, имена, даты ТОЛЬКО из этого блока. '
            'Если факта нет в контексте — не выдумывай, перефразируй мягче.'
        )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": topic},
    ]
    response = await client.chat.completions.create(
        model="anthropic/claude-sonnet-4.6",
        messages=messages, max_tokens=1024, temperature=0.85,
    )
    text = (response.choices[0].message.content or "").strip()

    body_only = _strip_image_prompt_for_length(text)
    if len(body_only) <= _AI_BODY_TARGET:
        return text

    # Превысил — просим переписать с конкретными цифрами.
    logger.info('post too long — asking AI to shorten',
                topic=topic, body_len=len(body_only), target=_AI_BODY_TARGET)
    messages.append({"role": "assistant", "content": text})
    messages.append({"role": "user", "content": (
        f"Твой ответ {len(body_only)} символов в теле поста (без IMAGE_PROMPT). "
        f"Это слишком длинно — пост будет обрезан Telegram. Перепиши тот же пост "
        f"короче, чтобы тело поста (от заголовка до подписи 🟣 Нейра) "
        f"было <b>не более {_AI_BODY_TARGET - 50} символов</b>. Сохрани суть и "
        f"структуру (HOOK, CORE, SO WHAT, подпись), но убери лишние слова, "
        f"объединяй короткие фразы. Не теряй обещание заголовка — если в нём "
        f"число пунктов, сохрани все. Снова добавь строку IMAGE_PROMPT в конце."
    )})
    retry = await client.chat.completions.create(
        model="anthropic/claude-sonnet-4.6",
        messages=messages, max_tokens=1024, temperature=0.7,
    )
    retry_text = (retry.choices[0].message.content or "").strip()
    retry_body = _strip_image_prompt_for_length(retry_text)
    if len(retry_body) < len(body_only):
        logger.info('retry shorter accepted', retry_len=len(retry_body))
        return retry_text
    # Ретрай не помог — возвращаем оригинал, дальше fit_to_caption отрежет.
    logger.warning('retry did not shorten', original=len(body_only), retry=len(retry_body))
    return text


# Расписание AI-фотостудии. Канал = «Нейросети для твоих фото в любой сцене.
# Каждый пост — один готовый приём или пресет». Поэтому ВСЕ daily-слоты =
# 'neyra'. AI/VPN-пулы остаются в channel_topics.py как резерв для ручных
# постов через /channel_post (см. cta_override).
#
# Расписание: 5 daily (Пн/Вт/Чт/Пт/Сб) + 2 feature (Ср/Вс) = 7 постов/нед.
# 100% постов — про AI-фото/пресеты Нейры. Это:
#   - Соответствует описанию канала (нет диссонанса для подписчика)
#   - Поднимает CPM будущей рекламы (узкая ниша = выше ставка)
#   - Каждый пост работает как воронка в Нейру (per-post промо в footer)
_WEEKDAY_PILLARS: dict[int, str] = {
    0: 'neyra',  # Пн
    1: 'neyra',  # Вт
    2: 'neyra',  # Ср — fallback (daily обычно не сработает, идёт feature)
    3: 'neyra',  # Чт
    4: 'neyra',  # Пт
    5: 'neyra',  # Сб
    6: 'neyra',  # Вс — fallback
}


async def _pick_topic(mode: str = 'daily') -> str:
    """Pick a topic from the appropriate pool, avoiding the last 30 used.

    mode='daily'   → CHANNEL_TOPICS, столп выбирается по дню недели в МСК
                     (см. _WEEKDAY_PILLARS). Если в текущем столпе всё уже
                     использовалось — fallback на общий пул.
    mode='feature' → FEATURE_TOPICS (Neyra-focused, Wed+Sun lunch slot)
    """
    from app.services.channel_topics import TOPICS_BY_PILLAR

    if mode == 'feature':
        pool = FEATURE_TOPICS
        recent_key = 'channel_recent_topics_feature'
    else:
        # МСК = UTC+3. Используем naive datetime + offset чтобы не тянуть pytz.
        from datetime import timedelta as _td
        weekday = (datetime.now(timezone.utc) + _td(hours=3)).weekday()
        pillar = _WEEKDAY_PILLARS.get(weekday, 'vpn')
        pool = TOPICS_BY_PILLAR.get(pillar) or CHANNEL_TOPICS
        recent_key = 'channel_recent_topics'
        logger.info('topic pillar selected', weekday=weekday, pillar=pillar,
                   pool_size=len(pool))

    async with AsyncSessionFactory() as session:
        recent_raw = await get_config_value(session, recent_key)

    recent: list[str] = []
    if recent_raw:
        recent = [t for t in recent_raw.split("|||") if t]

    available = [t for t in pool if t not in recent[-30:]]
    if not available:
        # В этом столпе все темы недавно использовались — берём из общего пула
        # чтобы не повторяться.
        logger.info('pillar pool exhausted, falling back to full pool')
        available = [t for t in CHANNEL_TOPICS if t not in recent[-30:]] or list(CHANNEL_TOPICS)

    topic = random.choice(available)

    # Update recent list
    recent.append(topic)
    recent = recent[-30:]
    async with AsyncSessionFactory() as session:
        await set_config_value(session, recent_key, "|||".join(recent))
        await session.commit()

    return topic


async def _is_paused() -> bool:
    async with AsyncSessionFactory() as session:
        val = await get_config_value(session, "channel_publisher_paused")
    return val == "1"


async def _is_skip_next() -> bool:
    async with AsyncSessionFactory() as session:
        val = await get_config_value(session, "channel_publisher_skip_next")
    return val == "1"


async def _clear_skip_next() -> None:
    async with AsyncSessionFactory() as session:
        await set_config_value(session, "channel_publisher_skip_next", "0")
        await session.commit()


async def publish_pending_channel_post(bot) -> None:
    """Minute-check: публикует ЗАРАНЕЕ заготовленный пост в @conturD, когда
    наступает время. Конфиг bot_config['pending_channel_post'] = JSON
    {collage_fid, caption, publish_at_iso, topic}. После успеха — очищает
    конфиг. На ошибке НЕ чистит — повторит через минуту. Для ручных
    отложенных постов (showcase новинок и т.п.)."""
    import json
    from datetime import datetime, timezone
    try:
        async with AsyncSessionFactory() as session:
            raw = await get_config_value(session, "pending_channel_post")
    except Exception:
        return
    if not raw:
        return
    try:
        data = json.loads(raw)
        pub_at = datetime.fromisoformat(data["publish_at_iso"])
        if pub_at.tzinfo is None:
            pub_at = pub_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < pub_at:
            return  # ещё не время
    except Exception as exc:
        logger.error("pending_channel_post: bad config, clearing", error=str(exc)[:200])
        async with AsyncSessionFactory() as session:
            await set_config_value(session, "pending_channel_post", "")
            await session.commit()
        return

    try:
        fid = data.get("collage_fid")
        caption = data["caption"]
        if fid:
            sent = await bot.send_photo(
                chat_id=CHANNEL_ID, photo=fid, caption=caption,
                parse_mode="HTML", disable_notification=True,
            )
        else:
            sent = await bot.send_message(
                chat_id=CHANNEL_ID, text=caption, parse_mode="HTML",
                disable_web_page_preview=True, disable_notification=True,
            )
        from app.database.models import ChannelPost
        from sqlalchemy import insert as sql_insert
        async with AsyncSessionFactory() as session:
            await session.execute(sql_insert(ChannelPost).values(
                topic=data.get("topic", "Showcase новинок"), body=caption,
                cta_type="neyra", scheduled_at=pub_at,
                published_at=datetime.now(timezone.utc),
                message_id=sent.message_id, status="published",
            ))
            await set_config_value(session, "pending_channel_post", "")
            # Чтобы вечерний авто-пост сегодня не задвоил — скипаем ближайший прогон.
            await set_config_value(session, "channel_publisher_skip_next", "1")
            await session.commit()
        logger.info("pending channel post published", message_id=sent.message_id)
    except Exception as exc:
        logger.error("publish_pending_channel_post failed", error=str(exc)[:200])
        # не чистим конфиг — повтор на следующей минуте


async def _create_post_promo(
    preset_key: str | None = None,
    credits: int = 50,
    ttl_days: int = 30,
) -> str | None:
    """Создаёт уникальный промокод под конкретный пост канала.

    Имя кода кодирует preset_key (если задан) — это позволяет start.py
    распарсить пресет из имени промокода и сразу выставить FSM-state
    в edit-mode с этим пресетом.

    Форматы кода:
      - С пресетом:  CHAN_VOGUE_A1B2C3       (preset_key='vogue')
      - Без пресета: CHAN_PA1B2C3            (общий per-post tracking)
    """
    try:
        import uuid as _uuid
        from datetime import timedelta as _td
        from app.database.models import PromoCode
        suffix = _uuid.uuid4().hex[:6].upper()
        if preset_key:
            # Заменяем _ → '' чтобы не путать парсер (он split'ит по '_')
            preset_seg = preset_key.replace('_', '').upper()
            code = f'CHAN_{preset_seg}_{suffix}'
        else:
            code = f'CHAN_P{suffix}'
        async with AsyncSessionFactory() as session:
            session.add(PromoCode(
                code=code, credits=credits, max_uses=0, used_count=0,
                is_active=True,
                expires_at=datetime.now(timezone.utc) + _td(days=ttl_days),
            ))
            await session.commit()
        return code
    except Exception as exc:
        logger.warning('per-post promo creation failed', error=str(exc)[:200])
        return None


def _preset_key_from_promo(code: str) -> str | None:
    """Достаёт preset_key из имени промокода вида CHAN_<PRESETSEG>_<RAND>.

    Поскольку preset_keys содержат `_` (например `retro_90s`, `paris_night`),
    при создании promo мы их сжимаем (`retro_90s` → `RETRO90S`). Поэтому
    тут разворачиваем: ищем такой preset_key в EDIT_PRESETS чей
    `key.replace('_','').upper()` совпадает с центральным сегментом.

    Возвращает None если код не CHAN-формата или не нашли матча.
    """
    if not code or not code.startswith('CHAN_'):
        return None
    parts = code.split('_')
    # Ожидаем минимум 3 сегмента: CHAN, <PRESETSEG>, <RAND>. Иначе это
    # generic CHAN_P-код (формата CHAN_Pxxxxxx — split даст 2 сегмента).
    if len(parts) < 3:
        return None
    preset_seg = parts[1]
    # CHAN_P + 6hex это generic — пресета нет
    if preset_seg.startswith('P') and len(preset_seg) == 7:
        return None
    from app.handlers.image_edit import EDIT_PRESETS
    target = preset_seg.upper()
    for key in EDIT_PRESETS:
        if key.replace('_', '').upper() == target:
            return key
    return None


async def _build_post(
    topic: str, mode: str,
    cta_override: str | None = None,
    search_query: str | None = None,
):
    """Сборка одного поста: AI-генерация текста + sanitize + CTA + защита от
    caption-лимита + генерация картинки + inline-клавиатура с tracked-deeplink
    и copy_text-кнопкой для промпта.

    Args:
        topic: тема поста
        mode:  'daily' / 'feature'
        cta_override: 'none' = нейтральный пост без промо.
                      'aurum_daily' / 'neira_daily' / 'neira_feature' = форс конкретного CTA.
                      None = автовыбор по теме.
        search_query: если задано — делаем Brave-поиск и пихаем top-5 snippet'ов
                      в system-prompt как контекст актуальных фактов.

    Возвращает (full_text, image_bytes, cta_type, reply_markup):
        full_text   — caption поста (HTML)
        image_bytes — bytes или None
        cta_type    — 'aurum_daily' / 'neira_daily' / 'neira_feature' / 'none'
        reply_markup — InlineKeyboardMarkup или None (если cta='none')
    """
    # CTA выбор
    if cta_override == 'none':
        cta_type, cta_footer = 'none', None
    elif cta_override == 'neira_feature':
        cta_type, cta_footer = 'neira_feature', _CTA_FOOTER_FEATURE
    elif cta_override:
        cta_map = {opt[0]: opt for opt in _CTA_OPTIONS_DAILY}
        if cta_override in cta_map:
            cta_type, cta_footer = cta_map[cta_override]
        else:
            logger.warning('unknown cta_override, falling back', cta=cta_override)
            cta_type, cta_footer = _cta_for_topic(topic)
    elif mode == 'feature':
        cta_type, cta_footer = 'neira_feature', _CTA_FOOTER_FEATURE
    else:
        cta_type, cta_footer = _cta_for_topic(topic)

    # Бонус НЕ в каждом посте — ценность через дефицит (правило владельца 27.05).
    # ~65% постов идут БЕЗ «+N кр»: обычный CTA в бота (?start=channel — не
    # кредитует), только контент. ~35% сохраняют бонус-приманку. cta_override
    # (ручной /channel_post) не трогаем — там бонус выставлен осознанно.
    # ТОЛЬКО neyra-CTA гейтим в plain. aurum_daily (VPN-посты) и 'none' не трогаем
    # — иначе VPN-пост получил бы Нейра-футер «фото со своим лицом» (бессмыслица).
    if cta_type in ('neira_daily', 'neira_feature') and not cta_override and random.random() < 0.65:
        cta_type = 'neira_plain'
        cta_footer = ('—\n🎁 <a href="https://t.me/NeyraChatBot?start=channel">'
                      '<b>Хочу такой кадр со своим лицом → в Нейре</b></a>')

    # Опциональный веб-контекст из Brave Search — реальные факты вместо
    # AI-галлюцинаций. Особенно важно для нейтральных постов где репутация
    # канала держится именно на точности информации.
    web_context = ''
    if search_query:
        from app.services import brave_search
        if brave_search.is_enabled():
            results = await brave_search.search(search_query, count=5)
            if results:
                web_context = brave_search.format_for_context(results)
                logger.info('brave search context added',
                            query=search_query[:80], results=len(results))

    raw_body = await _generate_post(topic, web_context=web_context)
    raw_body, noise = _strip_ai_noise(raw_body)
    if noise:
        logger.warning('AI noise removed (pre-split)', topic=topic, removed=noise)
    # Парсим финальные строки в порядке: IMAGE_PROMPT → PRESET → PROMPT_COPY (legacy).
    body, ai_image_prompt = _extract_image_prompt(raw_body)
    body, preset_key = _extract_preset_key(body)
    body, _prompt_copy_legacy = _extract_prompt_copy(body)  # игнорим если есть
    # Если AI «забыл» строку PRESET в neyra-теме — пытаемся inferring по тексту.
    # Так посту достанется правильный preset preview + working deeplink-кнопка
    # без перегенерации (экономия 5-15 сек + токенов).
    if not preset_key and (cta_type in ('neira_daily', 'neira_feature', 'neira_plain')):
        inferred = _infer_preset_from_body(body)
        if inferred:
            preset_key = inferred
            logger.info('preset inferred from body', preset=inferred, topic=topic)
        else:
            logger.warning('neyra post has NO preset (degraded mode)', topic=topic)
    body, more_noise = _strip_ai_noise(body)
    if more_noise:
        logger.warning('AI noise removed (post-split)', topic=topic, removed=more_noise)
    body = _fix_markdown_bold(body)
    body = premium_text(body)
    body = _link_signature(body)

    # Per-post promo: если у поста neyra-CTA — создаём уникальный промокод.
    # Если AI выбрал пресет — кодируем его в имени (start.py распарсит и
    # сразу откроет edit-mode с этим пресетом). Без пресета — generic CHAN_P.
    # CTA-footer перестраиваем динамически: единственная кликабельная ссылка
    # в посте, ведёт СРАЗУ в нужный пресет и называет его по имени.
    post_promo_code: str | None = None
    if cta_type in ('neira_daily', 'neira_feature'):
        # Бонус промокода ДОЛЖЕН совпадать с текстом футера: feature → +100,
        # daily → +50. Иначе юзер кликает на «+100», а получает 50.
        promo_credits = 100 if cta_type == 'neira_feature' else 50
        post_promo_code = await _create_post_promo(
            preset_key=preset_key, credits=promo_credits,
        )
        if post_promo_code:
            cta_footer = _neyra_cta_footer(cta_type, preset_key, post_promo_code)

    # Footer-CTA в caption ВСЕГДА, даже при наличии inline-кнопки. Они служат
    # разным целям: кнопка = быстрый 1-тап action, footer-ссылка = читаемая
    # реклама в тексте поста. Footer виден:
    # - в превью канала (где кнопки не всегда видны)
    # - при репостах в другие чаты (inline-кнопки не переносятся в forward)
    # - в списке чатов (last-message preview)
    # Дубль визуально терпим, потери конверсии от его отсутствия — серьёзнее.
    full_text = body
    if cta_footer:
        full_text = f"{body}\n\n{cta_footer}"
    full_text = await _maybe_inject_secret_promo(full_text)

    original_len = len(full_text)
    full_text = _fit_to_caption(full_text)
    if len(full_text) != original_len:
        logger.warning('post truncated to fit caption',
                       original_len=original_len, truncated_len=len(full_text))

    # Изображение поста. Приоритет:
    # 1. Если есть preset_key → готовое preview из БД (это TG file_id, не bytes —
    #    тогда нет затрат на генерацию + юзер видит ровно то что бот сделает).
    # 2. Иначе → OpenRouter image generation (для VPN/AI-tools постов).
    # 3. Fallback → fal.ai (если включён) и FLUX.
    image_source: bytes | str | None = None  # bytes ИЛИ TG file_id (str)
    if preset_key:
        try:
            from app.database.repo.preset_previews import get_preview_file_id
            async with AsyncSessionFactory() as session:
                file_id = await get_preview_file_id(session, preset_key)
            if file_id:
                image_source = file_id  # TG file_id — пере-использует уже загруженный кадр
                logger.info('post image via preset preview (cached)', preset=preset_key)
        except Exception as exc:
            logger.warning('preset preview lookup failed',
                           preset=preset_key, error=str(exc)[:200])

    if image_source is None:
        try:
            base_prompt = ai_image_prompt or topic
            image_prompt = f"{base_prompt}. {_IMAGE_STYLE}"
            if openrouter_image_service.is_enabled():
                image_source = await openrouter_image_service.generate_image(image_prompt)
                if image_source:
                    logger.info('post image via OpenRouter',
                                model=openrouter_image_service._IMAGE_MODEL)
            if not image_source and fal_image_service.is_enabled():
                image_source = await fal_image_service.generate_image(image_prompt)
                if image_source:
                    logger.info('post image via fal.ai (fallback)')
            if not image_source:
                image_source = await _flux_generate_image(image_prompt)
                if image_source:
                    logger.info('post image via FLUX (fallback)')
        except Exception as img_exc:
            logger.warning('post image generation failed', error=str(img_exc)[:200])

    image_bytes = image_source  # сохранил имя для совместимости с return-ами

    reply_markup = _build_post_keyboard(
        cta_type=cta_type,
        preset_key=preset_key,
        post_promo_code=post_promo_code,
    )

    return full_text, image_bytes, cta_type, reply_markup


async def assemble_manual_post(
    body: str, preset_key: str | None, mode: str = 'feature',
):
    """Собирает пост из ГОТОВОГО текста (написанного вручную / основной моделью),
    БЕЗ вызова OpenRouter `_generate_post`. Для случаев когда пост пишется в
    активной сессии — это дешевле (0 токенов OpenRouter) и качественнее.

    Делает всё то же пост-обработку что `_build_post`, кроме AI-генерации:
    markdown→HTML, premium-эмодзи, линковка подписи, per-post промокод с нужным
    бонусом, именованный CTA-footer, обрезка под caption-лимит, превью пресета
    из БД (TG file_id, бесплатно).

    Args:
        body: текст поста БЕЗ footer-CTA (подпись «🟣 Нейра» можно не
              писать — добавится/слинкуется сама). Обычный HTML Telegram.
        preset_key: ключ пресета из EDIT_PRESETS (для превью + deeplink). None —
              нейтральный пост без пресета.
        mode: 'feature' (+100 кр) или 'daily' (+50 кр).

    Возвращает (full_text, image_source, cta_type, promo_code).
    """
    cta_type = 'neira_feature' if mode == 'feature' else 'neira_daily'

    body = _fix_markdown_bold(body)
    body = premium_text(body)
    body = _link_signature(body)

    promo_credits = 100 if cta_type == 'neira_feature' else 50
    promo_code = await _create_post_promo(preset_key=preset_key, credits=promo_credits)
    cta_footer = _neyra_cta_footer(cta_type, preset_key, promo_code) if promo_code else None

    full_text = f"{body}\n\n{cta_footer}" if cta_footer else body
    full_text = _fit_to_caption(full_text)

    image_source: str | None = None
    if preset_key:
        try:
            from app.database.repo.preset_previews import get_preview_file_id
            async with AsyncSessionFactory() as session:
                image_source = await get_preview_file_id(session, preset_key)
        except Exception as exc:
            logger.warning('manual post preview lookup failed',
                           preset=preset_key, error=str(exc)[:200])

    return full_text, image_source, cta_type, promo_code


def _build_post_keyboard(
    cta_type: str, preset_key: str | None, post_promo_code: str | None,
):
    """Inline-кнопки для постов канала отключены по решению владельца:
    реклама идёт ТОЛЬКО гиперссылкой в footer caption'а. Причина:
    - inline-кнопки не переносятся при forward в другие чаты
    - footer-ссылка с per-post промо (CHAN_<PRESET>_*) и так делает 1-тап
      переход в edit-flow через start.py deeplink-парсер
    - меньше визуального шума под фото
    Если решим вернуть — раскомментировать прошлую логику из git history.
    """
    return None


async def test_post_to_chat(
    bot: Bot, chat_id: int, topic: str | None = None, mode: str = 'daily',
    cta_override: str | None = None, search_query: str | None = None,
) -> int | None:
    """Сгенерировать полный пост (тот же pipeline что @conturD) и отправить
    в произвольный chat_id (приватный чат, тестовый канал и т.п.). Без записи
    в БД, без уведомления админу. Удобно для предпросмотра перед публикацией.

    Args:
        cta_override: 'none' = нейтральный пост без рекламы
        search_query: если задано — добавить контекст из Brave-поиска

    Возвращает message_id отправленного поста или None при провале.
    """
    chosen_topic = topic or await _pick_topic(mode=mode)
    logger.info('test post: building', chat_id=chat_id, topic=chosen_topic,
                mode=mode, cta_override=cta_override, search=bool(search_query))
    full_text, image_source, cta_type, reply_markup = await _build_post(
        chosen_topic, mode, cta_override=cta_override, search_query=search_query,
    )
    photo = _to_photo_payload(image_source)
    if photo is not None:
        sent = await bot.send_photo(
            chat_id=chat_id, photo=photo, caption=full_text,
            parse_mode='HTML', reply_markup=reply_markup,
        )
    else:
        sent = await bot.send_message(
            chat_id=chat_id, text=full_text,
            parse_mode='HTML', disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    return sent.message_id


def _to_photo_payload(source: bytes | str | None):
    """Готовит payload для bot.send_photo: bytes → BufferedInputFile, str → file_id as-is."""
    if source is None:
        return None
    if isinstance(source, str):
        return source  # TG file_id — отдаём как есть
    return BufferedInputFile(source, filename='cover.jpg')


async def generate_and_publish(
    bot: Bot, topic: str | None = None, mode: str = 'daily',
) -> None:
    """
    Core publish function. Called by scheduler and /channel_post command.
    If topic is None, picks one from the appropriate pool by mode.

    mode='daily'   — обычный evergreen-пост, случайный CTA (Aurum/Neyra/none)
    mode='feature' — фишечный пост с прямой промо Нейры (Ср + Вс)
    """
    from app.database.models import ChannelPost
    from sqlalchemy import insert as sql_insert

    scheduled_at = datetime.now(timezone.utc)
    chosen_topic = topic or await _pick_topic(mode=mode)

    # Insert pending record (cta_type заполним после _build_post)
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            sql_insert(ChannelPost)
            .values(
                topic=chosen_topic, body='', cta_type='',
                scheduled_at=scheduled_at, status='pending',
            )
            .returning(ChannelPost.id)
        )
        post_id = result.scalar_one()
        await session.commit()

    try:
        # Единый pipeline — тот же что в test_post_to_chat. Помогает не
        # расходиться между prod и test (раньше были две разные реализации
        # и фиксы в одной не доезжали до другой).
        full_text, image_source, cta_type, reply_markup = await _build_post(
            chosen_topic, mode,
        )

        # disable_notification=True: evergreen-канал, посты идут автоматически
        # вечером и в обед — звуковой пуш всем подписчикам не нужен и снижает
        # удержание. Они и так листают ленту.
        photo = _to_photo_payload(image_source)
        if photo is not None:
            sent = await bot.send_photo(
                chat_id=CHANNEL_ID, photo=photo, caption=full_text,
                parse_mode='HTML', reply_markup=reply_markup,
                disable_notification=True,
            )
        else:
            sent = await bot.send_message(
                chat_id=CHANNEL_ID, text=full_text,
                parse_mode='HTML', disable_web_page_preview=True,
                reply_markup=reply_markup, disable_notification=True,
            )
        message_id = sent.message_id
        published_at = datetime.now(timezone.utc)

        # Update record to published
        async with AsyncSessionFactory() as session:
            from sqlalchemy import update as sql_update
            await session.execute(
                sql_update(ChannelPost)
                .where(ChannelPost.id == post_id)
                .values(
                    body=full_text,
                    cta_type=cta_type,
                    message_id=message_id,
                    published_at=published_at,
                    status='published',
                )
            )
            await session.commit()

        logger.info("Channel post published", topic=chosen_topic, message_id=message_id)

        # Notify admin: чистим preview от HTML-тегов (а то они показываются буквально)
        plain_preview = re.sub(r'<[^>]+>', '', full_text)[:200].strip()
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"✅ <b>Опубликован пост на @conturD</b>\n\n"
                    f"<i>{plain_preview}…</i>\n\n"
                    f"🔗 <a href=\"https://t.me/conturD/{message_id}\">Посмотреть пост</a>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as notify_exc:
            logger.warning("Failed to notify admin about published post", error=str(notify_exc)[:200])

    except Exception as exc:
        error_msg = str(exc)[:500]
        logger.error("Channel post failed", topic=chosen_topic, error=error_msg)

        # Update record to failed
        async with AsyncSessionFactory() as session:
            from sqlalchemy import update as sql_update
            await session.execute(
                sql_update(ChannelPost)
                .where(ChannelPost.id == post_id)
                .values(status="failed", error=error_msg)
            )
            await session.commit()

        # Notify admin of failure
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"❌ <b>Ошибка публикации на @conturD</b>\n\n"
                    f"Тема: {chosen_topic}\n"
                    f"Ошибка: <code>{error_msg[:300]}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def run_scheduled_post(bot: Bot) -> None:
    """Called by APScheduler. Checks paused/skip flags before publishing."""
    if await _is_paused():
        logger.info("Channel publisher is paused, skipping scheduled post")
        return

    if await _is_skip_next():
        await _clear_skip_next()
        logger.info("Skipping next post as requested by admin")
        async with AsyncSessionFactory() as session:
            from sqlalchemy import insert as sql_insert
            from app.database.models import ChannelPost
            await session.execute(
                sql_insert(ChannelPost).values(
                    topic="[skipped by admin]",
                    body="",
                    cta_type="none",
                    scheduled_at=datetime.now(timezone.utc),
                    status="skipped",
                )
            )
            await session.commit()
        return

    await generate_and_publish(bot, mode='daily')


async def run_scheduled_feature_post(bot: Bot) -> None:
    """Feature-пост (Ср + Вс, 12:30 МСК). Узкая промо Нейры с tracked deep-link.
    Уважает те же paused/skip-next флаги."""
    if await _is_paused():
        logger.info("Channel publisher paused — skipping feature post")
        return
    if await _is_skip_next():
        await _clear_skip_next()
        logger.info("Skipping next post (admin flag) — applies to feature too")
        return
    await generate_and_publish(bot, mode='feature')

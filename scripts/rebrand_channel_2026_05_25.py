"""Одноразовый скрипт 25.05: переписать 9 старых не-Нейра постов в @conturD
под текущую тематику + заменить фото на превью из банка пресетов.
Бот-only сообщения (channel_posts table) — ручные посты не трогаются.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto
from sqlalchemy import select, update

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import ChannelPost, PresetPreview

CHANNEL = '@conturD'

@dataclass
class Edit:
    post_id: int       # channel_posts.id
    msg_id: int        # TG message_id
    preset_key: str
    position: int      # 0=фем, 1=муж
    new_topic: str
    new_body: str

# ── 9 новых постов ────────────────────────────────────────────────────────────

EDITS = [
    Edit(2, 59, 'golden_hour', 0,
         '5 признаков, что лицо в AI-фото — точно твоё',
         '''🔍 <b>5 ПРИЗНАКОВ, ЧТО ФОТО — ТОЧНО ТВОЁ</b>

AI-портрет из Нейры выглядит как студийный. По чему понять, что лицо в кадре — реально твоё, а не «среднее по выборке».

→ <b>Линия челюсти</b> — твоя, а не «вытянутая под голливуд»
→ <b>Глаза</b> — тот же цвет и разрез
→ <b>Скулы и нос</b> — пропорции, а не симметрия из шаблона
→ <b>Кожа</b> — родинки и мелкие черты сохранены, не заглажены
→ <b>Выражение</b> — твой характер лица, а не дежурная улыбка

⚙ Если хоть один пункт «не похож» — модель уехала. Нейра ловит 68 точек лица и сверяет с оригиналом.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Пришли селфи — +500 кр новичку</b></a>'''),

    Edit(3, 60, 'anime', 0,
         'Качественный vs быстрый режим: одно лицо, два результата',
         '''⚙ <b>КАЧЕСТВЕННО ИЛИ БЫСТРО — В ЧЁМ РАЗНИЦА</b>

В Нейре две скорости генерации: быстрая (10-15 сек) и качественная (40-60 сек). На простом портрете не отличишь — но в сложной сцене разница видна сразу.

→ <b>Быстрая</b> — селфи в обычной обстановке, минимум мелких объектов, текста нет
→ <b>Качественная</b> — аниме, ретро-плёнка, граффити, надписи в кадре, мелкая фактура

🎯 Правило: если в сцене должен читаться текст или сложный фон — переключай на «качество». Лицо сохраняется одинаково в обоих режимах.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Попробуй оба режима — +500 кр новичку</b></a>'''),

    Edit(4, 61, 'magazine_vibe', 1,
         'AI-фото за 3 минуты — без фотографа и фотошопа',
         '''⏱ <b>AI-ФОТО ЗА 3 МИНУТЫ — БЕЗ ФОТОГРАФА И ФОТОШОПА</b>

Никакой студии, регистраций, программ. Только Telegram и одно селфи.

→ <b>Минута 1</b> — заходишь в @NeyraChatBot, нажимаешь Старт
→ <b>Минута 2</b> — присылаешь селфи, выбираешь сцену из каталога
→ <b>Минута 3</b> — получаешь готовый кадр с твоим лицом

⚙ Под капотом: 68 точек лица, перенос света сцены, фикс композиции. Лицо в кадре — точно твоё, не «похожий на тебя».

🎁 Первое фото бесплатно, новичку — <b>+500 кр</b> в подарок.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Запустить фотостудию в Telegram</b></a>'''),

    Edit(5, 62, 'gold_coffee', 0,
         'Где AI берёт твоё лицо и почему оно не утекает',
         '''🔒 <b>ГДЕ AI БЕРЁТ ТВОЁ ЛИЦО — И ПОЧЕМУ ОНО НЕ УТЕКАЕТ</b>

Логичный вопрос: «Я загружаю селфи в бота — куда оно потом девается?» Разбираемся без воды.

→ <b>Селфи приходит на сервер</b> — только чтобы сгенерить кадр
→ <b>Лицо НЕ уходит в обучение</b> — модели работают на уже готовых датасетах, твоё лицо в них не попадает
→ <b>Никаких внешних баз</b> — фото обрабатывается и удаляется
→ <b>Результат</b> — кадр со сценой, где лицо твоё, но привязки к личности нет

⚙ Совет: можно нажать /reset и стереть всю переписку с ботом одной кнопкой.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Попробовать — +500 кр в подарок</b></a>'''),

    Edit(6, 63, 'retro_90s', 1,
         'Почему ручной Photoshop отстал от AI face swap',
         '''🎨 <b>ПОЧЕМУ ПОТРАТИТЬ ДЕНЬ НА PHOTOSHOP — БОЛЬШЕ НЕ НУЖНО</b>

Раньше «лицо в чужой кадр» — это часы в редакторе. Маски, тени, цветокоррекция, повторное матирование. Сейчас та же задача — минута.

→ <b>Поиск лица</b> — нейросеть видит твоё лицо за 0.2 сек
→ <b>Подгонка ракурса</b> — угол, размер, наклон автоматически
→ <b>Перенос света</b> — лицо перекрашивается под свет сцены, без ручной работы кистью
→ <b>Текстура кожи</b> — родинки и мелкая фактура сохраняются, не «пластиковая маска»

⚙ Если знаешь Photoshop — поймёшь сразу, какая часть работы автоматизирована. Если не знаешь — просто получаешь готовый кадр.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Один кадр — +500 кр новичку</b></a>'''),

    Edit(7, 64, 'graffiti_zine', 0,
         'Промпт пресета — невидимая инструкция за каждой сценой',
         '''🧠 <b>ПРОМПТ ПРЕСЕТА — НЕВИДИМАЯ ИНСТРУКЦИЯ ЗА КАДРОМ</b>

Тыкаешь «Магия заката» — получаешь конкретный кадр. Но за каждым пресетом — длинный текст-инструкция модели. Что в нём?

→ <b>Сцена</b> — где ты стоишь, что вокруг, какой ракурс
→ <b>Свет</b> — мягкий закат, неон, студийная вспышка
→ <b>Стиль</b> — журнальный, ретро-плёнка, кино, аниме
→ <b>Сохранение лица</b> — отдельный блок «лицо человека на фото не менять»

⚙ Хороший пресет — это 200-400 слов точных инструкций. Поэтому «студия в стиле 90-х» из Нейры выглядит ровно как 90-е, а не «куда-то туда».

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Выбрать пресет — +500 кр новичку</b></a>'''),

    Edit(8, 65, 'vogue', 0,
         'Аватарка без фотосессии — за минуту',
         '''📸 <b>АВАТАРКА БЕЗ ФОТОСЕССИИ — ЗА МИНУТУ</b>

Раньше аватарка для соцсетей — это фотограф, локация, ретушь, неделя ожидания. Сейчас одно селфи и сцена на выбор — за минуту.

→ <b>Не нужна студия</b> — лицо вставляется в готовую сцену с правильным светом
→ <b>Не нужен фотограф</b> — кадр уже выстроен в пресете
→ <b>Не нужна ретушь</b> — финальная картинка сразу готова к посту

⚙ Каталог: от классики (закат, дождь, рестораны) до резких стилей (киберпанк, аниме, граффити). 50+ сцен, новые добавляются раз в неделю.

🎯 Совет: для аватарки бери пресеты с близким планом — «Магия заката», «Кофе по-джентльменски», «За пультом диджея».

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Сделать аватарку — +500 кр новичку</b></a>'''),

    Edit(10, 70, 'cyberpunk', 1,
         'Что AI-фото умеет в 2026',
         '''🚀 <b>ЧТО AI-ФОТО УМЕЕТ В 2026</b>

Год назад «лицо в чужой кадр» — это игрушка, видно за километр. Сейчас — почти неотличимо от студийной съёмки. Что изменилось.

→ <b>Лицо</b> — сохраняется 1-в-1, включая мимику и мелкие черты
→ <b>Свет сцены</b> — переносится на лицо, нет «вырезанной головы»
→ <b>Композиция</b> — кадр выстраивается под пресет, не наоборот
→ <b>Текст в сцене</b> — теперь читаемый (надписи на вывесках, постерах)
→ <b>Свободные правки</b> — пишешь «убери очки» — убирает, без потери лица

⚙ Под капотом Нейры: модель nano-banana 2, идентификация по 68 точкам, режим «качество» для сложных сцен.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Проверить — +500 кр новичку</b></a>'''),

    Edit(15, 79, 'rooftop_wine', 1,
         '3 правила селфи, чтобы AI-фото получилось ярче',
         '''📷 <b>3 ПРАВИЛА СЕЛФИ — ЧТОБЫ AI-ФОТО ВЫШЛО ОГОНЬ</b>

Качество результата на 70% зависит от того, что ты прислал. Вот что нужно — и чего лучше избегать.

→ <b>Свет в лицо, не сзади</b> — окно или лампа должны светить НА тебя, иначе модель додумывает черты
→ <b>Без очков и масок</b> — глаза должны быть видны полностью, иначе теряется характер лица
→ <b>Нейтральное выражение</b> — лёгкая улыбка или спокойное лицо лучше гримас

⚙ Бонус: камера на уровне глаз (не снизу), фон без бардака — модели проще ловить лицо.

🎯 Если результат не понравился — нажми «↻ Ещё вариант», часто следующий лучше первого.

<a href="https://t.me/conturD">😈 Контур доступа</a>

—
🎁 <a href="https://t.me/NeyraChatBot?start=channel"><b>Прислать селфи — +500 кр новичку</b></a>'''),
]


async def main():
    # Sanity: caption limit 1024
    for e in EDITS:
        if len(e.new_body) > 1024:
            print(f'❌ post {e.msg_id}: body {len(e.new_body)} > 1024')
            sys.exit(1)

    # Pull file_ids
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(
            select(PresetPreview.preset_key, PresetPreview.position, PresetPreview.file_id)
        )).all()
    file_map = {(k, p): fid for k, p, fid in rows}

    bot = Bot(token=settings.BOT_TOKEN)
    results = []
    try:
        for e in EDITS:
            fid = file_map.get((e.preset_key, e.position))
            if not fid:
                # fallback position
                alt = file_map.get((e.preset_key, 1 - e.position))
                if not alt:
                    results.append((e.msg_id, 'SKIP_NO_PHOTO', f'no file_id for {e.preset_key}'))
                    continue
                fid = alt
                print(f'⚠ msg {e.msg_id}: position {e.position} нет, беру альтернативу')

            try:
                await bot.edit_message_media(
                    chat_id=CHANNEL,
                    message_id=e.msg_id,
                    media=InputMediaPhoto(media=fid, caption=e.new_body, parse_mode='HTML'),
                )
                status = 'OK_MEDIA'
            except TelegramBadRequest as exc:
                msg = str(exc)
                if 'there is no media in the message to edit' in msg.lower() or 'message can\'t be edited' in msg.lower():
                    try:
                        await bot.edit_message_text(
                            chat_id=CHANNEL,
                            message_id=e.msg_id,
                            text=e.new_body,
                            parse_mode='HTML',
                            disable_web_page_preview=True,
                        )
                        status = 'OK_TEXT_FALLBACK'
                    except TelegramBadRequest as exc2:
                        status = f'FAIL: {str(exc2)[:120]}'
                else:
                    status = f'FAIL: {msg[:120]}'

            results.append((e.msg_id, status, e.preset_key))

            # Sync DB
            if status.startswith('OK'):
                async with AsyncSessionFactory() as session:
                    await session.execute(
                        update(ChannelPost)
                        .where(ChannelPost.id == e.post_id)
                        .values(body=e.new_body, topic=e.new_topic)
                    )
                    await session.commit()

            await asyncio.sleep(1.5)  # rate-limit safety
    finally:
        await bot.session.close()

    print()
    print('=== РЕЗУЛЬТАТЫ ===')
    ok = 0
    for msg_id, status, key in results:
        mark = '✅' if status.startswith('OK') else '❌'
        print(f'{mark} msg_id={msg_id:>3}  [{key:<14}]  {status}')
        if status.startswith('OK'):
            ok += 1
    print(f'\nУспешно: {ok}/{len(EDITS)}')


if __name__ == '__main__':
    asyncio.run(main())

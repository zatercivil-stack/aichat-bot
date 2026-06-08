from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.repo.credits import MODEL_COSTS

# Telegram Premium custom emoji IDs (глобальные в TG). Premium-юзеры видят
# анимацию, не-premium видят обычный fallback-emoji в тексте кнопки.
# Берём через model_construct(icon_custom_emoji_id=...) — текст должен быть
# БЕЗ ведущей эмодзи, иначе будет дублирование.
PE_SPARKLES = '5325547803936572038'      # ✨ — для edit/create
PE_ROBOT = '6147670072706995009'         # 🤖 — для AI-фич (face swap)
PE_SPEAKER = '5397849751548884255'       # 🗣 — Задать вопрос
PE_MONEY = '5445256208992718797'         # 💰 — Баланс
PE_GIFT = '5440502878491930005'          # 🎁 — Промокод / Подарок
PE_NEW = '5339477701432462548'           # 🆕 — раздел «Новинки»
PE_HANDSHAKE = '5352795355635276043'     # 🤝 — Пригласить друга
PE_ROCKET = '5368654333397199941'        # 🚀 — Pro / премиум план
PE_FIRE = '5445088653728568921'          # 🔥 — Максимум / акция
PE_AU_GOLD = '5346258552849713801'       # 🥇 — премиум-тариф
PE_BACK = '5456187398977247949'          # ⬅️ — Назад
PE_CROSS = '5465665476971471368'         # ❌ — Отмена
PE_CHECK = '5190782321577441092'         # ✅ — Подтвердить
PE_CARD = '5213403875670765022'          # 💳 — Оплатить
PE_PARTY = '5461151367559141950'         # 🎉 — Успех
PE_LIGHTNING = '5258203794772085854'     # ⚡️ — быстро / Старт-план
PE_PLANET = '5256041734005090333'        # 🌍 — Перевести / международное
PE_WAVE = '5947404268909629849'          # 👋 — Приветствие
PE_CALENDAR = '5413879192267805083'      # 🗓 — Подписка / даты
PE_UNLOCK = '5429405838345265327'        # 🔓 — Разблокировать
PE_TIMER = '5458640241915084025'         # ⏱️ — Время / срок
PE_CLIP = '5305265301917549162'          # 📎 — Прикрепить / Селфи
PE_FRAME = '5375074927252621134'         # 🖼 — Каталог / Галерея
PE_PAINT = '5431456208487716895'         # 🎨 — Создать картинку
PE_GEAR = '5341715473882955310'          # ⚙️ — Настройки / Ещё
PE_INFO = '5452026937172048380'          # ℹ️ — Помощь / Инфо
PE_HOME = '5416041192905265756'          # 🏠 — Главное меню
PE_MEGAPHONE = '5424818078833715060'     # 📣 — Канал / Подписаться
PE_BUBBLE = '5449509905947967949'        # 💬 — Чат / Написать
PE_LEFT = '5256247952564825322'          # ◀️ — Влево / Назад
PE_RIGHT = '5255835489675519149'         # ▶️ — Вправо / Вперёд
PE_CITY = '5870528297091206639'          # 🌇 — Городские сцены / Урбан
PE_THEATER = '5359441070201513074'       # 🎭 — Сцены-образы / Перевоплощения
PE_MOON_DARK = '5188666899860298925'     # 🌒 — Магия и стихии / Тёмная сторона


def main_menu_kb(
    show_wheel: bool = False, low_credits: bool = False,
    mystery_label: str | None = None,
) -> InlineKeyboardMarkup:
    """Главное меню. Layout оптимизирован под фото-меню (с welcome-коллажем
    сверху): media-сообщения в Telegram сжимают inline-кнопки в ширину фото,
    и 3 кнопки в одном ряду делают «Подписка» нечитаемой → юзеру кажется что
    «Баланс» вообще не нажимается. Поэтому Баланс/Подписка стоят парой (2 в
    ряду = шире, текст влезает), «Ещё» отдельным рядом во всю ширину.

    show_wheel=True → кнопка «🎡 Колесо удачи» (context-aware).
    mystery_label=str → кнопка «🎲 Сегодня бесплатно: <label>» (Mystery дня,
        показывается только если юзер ещё не забирал сегодня).
    Обе геймификации сверху, перед Баланс/Подписка — топовое место в виду.
    """
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton.model_construct(
        text='Сделать фото с моим лицом',
        callback_data='gallery_open', icon_custom_emoji_id=PE_SPARKLES, style='success',
    )])
    if mystery_label:
        # Mystery — главный retention-крючок. Ставим ниже hero CTA.
        # 24.05 v3: даже короткое «Бесплатно сегодня · {label}» обрезалось
        # под фото-меню. Убираем label целиком — он будет в caption «Сюрприз
        # дня: X» после тапа. На кнопке достаточно «Подарок дня» + 🎁
        # premium-иконки. Это даже усиливает интригу (mystery-feel).
        rows.append([InlineKeyboardButton.model_construct(
            text='Подарок дня · бесплатно',
            callback_data='mystery_today',
            icon_custom_emoji_id=PE_GIFT, style='success',
        )])
    if show_wheel:
        # БЕЗ 🎡 в тексте — premium PE_GIFT уже даёт значок.
        wheel_text = (
            'Бесплатные кр (крутка дня)' if low_credits
            else 'Колесо удачи · крутка дня'
        )
        rows.append([InlineKeyboardButton.model_construct(
            text=wheel_text, callback_data='wheel_spin',
            icon_custom_emoji_id=PE_GIFT, style='primary',
        )])
    rows.append([
        InlineKeyboardButton.model_construct(
            text='Баланс', callback_data='my_status', icon_custom_emoji_id=PE_MONEY,
        ),
        InlineKeyboardButton.model_construct(
            text='Подписка', callback_data='subscription', icon_custom_emoji_id=PE_AU_GOLD,
        ),
    ])
    rows.append([InlineKeyboardButton.model_construct(
        text='Ещё', callback_data='settings_menu', icon_custom_emoji_id=PE_GEAR,
    )])
    rows.append([InlineKeyboardButton(
        text='🆘 Поддержка', callback_data='support_open',
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def main_menu_kb_for_user(user_tg_id: int, credits: int) -> InlineKeyboardMarkup:
    """Async-обёртка над main_menu_kb: сама резолвит, надо ли показывать
    колесо. Используется в «горячих» местах (welcome / cb_main_menu) где у нас
    уже есть юзер. Места без юзера зовут синхронный main_menu_kb() (без колеса).

    Условия показа колеса:
      • Redis-ключ `wheel:{tg_id}` НЕ установлен (cooldown снят)
      • И при credits < 50 кр (юзеру реально нужна подпитка) — иначе колесо
        мозолит глаза подписчикам с балансом и обесценивает.
    """
    # 24.05 v2: раньше колесо показывали ТОЛЬКО при credits<50 → юзеры с 500кр
    # подарка не видели его никогда. Теперь — показываем всегда когда cooldown
    # снят (один раз в 24ч максимум, не спам). Текст подстраивается под
    # контекст: при низком балансе — «спасательный круг», иначе — приглашение.
    show_wheel = False
    low_credits = credits is not None and credits < 50
    try:
        from redis.asyncio import Redis
        from app.config import settings
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            show_wheel = not await r.exists(f'wheel:{user_tg_id}')
        finally:
            await r.aclose()
    except Exception:
        # Redis тупит — лучше не показывать (FOMO без работающей крутки = баг)
        show_wheel = False

    # Mystery preset of the day — показываем если есть активный и юзер ещё
    # не забирал сегодня. Это retention-механика №1.
    mystery_label: str | None = None
    try:
        from app.services.mystery_preset import get_today_mystery, is_used_today
        mystery = await get_today_mystery()
        if mystery and not await is_used_today(user_tg_id):
            mystery_label = mystery['label']
    except Exception:
        mystery_label = None

    return main_menu_kb(
        show_wheel=show_wheel,
        low_credits=low_credits,
        mystery_label=mystery_label,
    )


def settings_menu_kb() -> InlineKeyboardMarkup:
    """«Ещё» — компактный набор реально нужных вторичных действий.

    24.05: убраны мёртвые пункты (face_swap = 4 события/30д, «Нарисовать с нуля»
    = 0 событий, колесо ушло в context-aware в главное меню). Эти команды
    остаются доступны через /faceswap и /img, но не засоряют меню. «Ещё» теперь
    только то, что юзер реально открывает руками."""
    return InlineKeyboardMarkup(inline_keyboard=[
        # ── Money-action (главное в «Ещё») ──
        [InlineKeyboardButton.model_construct(
            text='Пригласить друга +150 кр',
            callback_data='ref_menu', icon_custom_emoji_id=PE_HANDSHAKE, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='У меня есть промокод',
            callback_data='enter_promo', icon_custom_emoji_id=PE_GIFT,
        )],
        # ── UX-настройка ──
        [InlineKeyboardButton.model_construct(
            text='Для кого фото (сменить М/Ж)',
            callback_data='change_gender', icon_custom_emoji_id=PE_ROBOT,
        )],
        [InlineKeyboardButton.model_construct(
            text='Как пользоваться ботом',
            callback_data='help', icon_custom_emoji_id=PE_INFO,
        )],
        [InlineKeyboardButton.model_construct(
            text='В главное меню',
            callback_data='main_menu', icon_custom_emoji_id=PE_HOME, style='danger',
        )],
    ])


def model_selection_kb(current_model: str) -> InlineKeyboardMarkup:
    """Visual-only pivot: показываем ОДНУ модель чата — DeepSeek (2 кр/ответ).
    Дорогие LLM (Sonnet/Opus) спрятаны — они генерили убыток.
    Юзер всё равно может писать в чат, но без выбора."""
    rows = [
        [InlineKeyboardButton.model_construct(
            text=f'DeepSeek — {MODEL_COSTS["free"]} кр / ответ (активна)',
            callback_data='set_model:free',
            icon_custom_emoji_id=PE_CHECK, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Назад', callback_data='settings_menu',
            icon_custom_emoji_id=PE_BACK, style='danger',
        )],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(callback: str = 'main_menu') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(text='Назад', callback_data=callback, icon_custom_emoji_id=PE_BACK, style='danger')],
    ])


def buy_or_invite_kb() -> InlineKeyboardMarkup:
    """Клавиатура для экранов «недостаточно кредитов».

    Три пути решения:
    - Подписка (success — высший LTV)
    - Разовый пак (primary — быстро и низкий вход)
    - Привести друга → +150 кр (альтернатива для тех, кто не хочет платить)

    Реф-кнопка показывается ТОЛЬКО в этом контексте — юзер сам пришёл на
    экран «у меня нет кредитов», значит готов рассмотреть пути их получить.
    Не раздражает и не спамит — нет фонового пуша «приведи друга».
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(
            text='Оформить подписку',
            callback_data='subscription',
            icon_custom_emoji_id=PE_AU_GOLD, style='success',
        )],
        [InlineKeyboardButton.model_construct(
            text='Купить кредиты разово',
            callback_data='shop',
            icon_custom_emoji_id=PE_CARD, style='primary',
        )],
        [InlineKeyboardButton.model_construct(
            text='Привести друга — +150 кр (бесплатно)',
            callback_data='ref_menu',
            icon_custom_emoji_id=PE_HANDSHAKE,
        )],
    ])

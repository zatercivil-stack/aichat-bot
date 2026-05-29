# Константы для модели и текстов

MODEL_LABEL = {
    'free':  'DeepSeek V3.2',
    'basic': 'GPT-5 nano',
    'mid':   'GPT-5 mini',
    'pro':   'Claude Sonnet 4.6',
    'ultra': 'Claude Opus 4.7',
}

REFERRAL_BONUS = 150

WELCOME_TEXT = (
    "👋 <b>Привет! Я Нейра — делаю фото с твоим лицом в любой сцене.</b>\n\n"
    "🎁 <b>+500 кредитов в подарок</b> — хватит на 2-3 кадра.\n\n"
    "📸 <b>Пришли селфи прямо сейчас</b> — за минуту покажу тебя в готовых сценах, "
    "лицо будет <b>в точности твоё</b>. Или жми кнопку, чтобы сначала глянуть примеры 👇"
)

# Статический fallback. В рантайме лучше использовать build_help_text() — он
# берёт актуальные цены/скидку из BotConfig (promo может закончиться, цены
# поднимутся), а также подставляет реальное число сцен из EDIT_PRESETS.
HELP_TEXT = (
    "ℹ️ <b>Как пользоваться Нейрой</b>\n\n"
    "Я делаю с твоего селфи фото в любой сцене. Главная фишка.\n\n"
    "Подробности откроются после первого <b>/start</b>."
)


async def build_help_text() -> str:
    """Динамическая справка — собирается на лету из реальных констант и BotConfig.

    Зачем: цены меняются (запуск-promo заканчивается), число пресетов растёт,
    каналы и бонусы редактируются админкой. Статический текст в БД быстро
    устаревает и врёт юзеру.
    """
    from app.services.pricing import get_promo_info
    from app.database.repo.credits import (
        DAILY_CHANNEL_CREDITS, DAILY_FREE_CREDITS, MODEL_COSTS,
    )
    from app.database.base import AsyncSessionFactory
    from app.database.repo.bot_config import get_config_value
    from app.handlers.image_edit import EDIT_PRESETS

    info = await get_promo_info()
    eff = info['effective_costs']
    base = info['base_costs']
    # Реальное число настроений на сегодня (из EDIT_PRESETS).
    scenes_count = len(EDIT_PRESETS)
    # Подарок новичку
    async with AsyncSessionFactory() as session:
        welcome_bonus_raw = (await get_config_value(session, 'welcome_bonus_credits') or '300').strip()
    try:
        welcome_bonus = int(welcome_bonus_raw)
    except ValueError:
        welcome_bonus = 300
    # Цены с пометкой promo
    if info['active'] and info['discount'] > 0:
        until_str = info['until']
        # Перевод ISO даты → ДД.ММ
        try:
            from datetime import date as _date
            d = _date.fromisoformat(until_str)
            until_human = d.strftime('%d.%m')
        except Exception:
            until_human = until_str
        promo_line = (
            f"\n🔥 <b>Запуск-акция до {until_human}</b>: "
            f"-{info['discount']} кр на правки и face-swap. "
            f"После — базовые цены."
        )
        edit_q = eff['image_edit']
        face_swap = eff['face_swap']
    else:
        promo_line = ''
        edit_q = base['image_edit']
        face_swap = base['face_swap']
    img_gen = MODEL_COSTS['image']

    return (
        "ℹ️ <b>Как пользоваться Нейрой</b>\n\n"
        "Шлёшь селфи → за минуту получаешь фото с твоим лицом в любой сцене. "
        "Лицо <b>в точности твоё</b>, не похожее.\n\n"
        "<b>━━━ Главное ━━━</b>\n"
        "<b>1.</b> Жми «<b>Сделать фото с моим лицом</b>» в главном меню\n"
        "<b>2.</b> Выбери сцену из каталога\n"
        f"<b>3.</b> Пришли селфи → жди ~1 минуту → готово\n\n"
        f"В каталоге <b>{scenes_count} сцен</b> по 3 разделам: "
        f"«Стиль и портрет», «Сцены и города», «Дерзкие и яркие». "
        f"Новые сцены — всегда первыми в каждом разделе.\n\n"
        "<b>━━━ Бесплатно ━━━</b>\n"
        f"🎁 <b>+{welcome_bonus} кр в подарок</b> новичку\n"
        f"➕ <b>{DAILY_FREE_CREDITS} кр каждый день</b> "
        f"({DAILY_CHANNEL_CREDITS} если подписан на @conturD)\n"
        f"🔥 <b>Серия дней подряд</b> → бонус на 3, 7, 14, 30 день\n"
        f"🎉 <b>Бонус за фото</b> → +50/+150/+400 кр на 3, 7, 15-м снимке\n"
        f"🎲 <b>Сюрприз дня</b> — один пресет каждый день бесплатно\n"
        f"🎡 <b>Колесо удачи</b> — крути раз в сутки\n"
        f"🤝 <b>Пригласи друга</b> — +150 кр за каждого\n\n"
        "<b>━━━ Дополнительно ━━━</b>\n"
        "🎭 <b>Подставить лицо в чужое фото</b> (команда /faceswap)\n"
        "  → пришли кадр-сцену → пришли селфи → получишь себя в кадре\n"
        "🎨 <b>Нарисовать картинку с нуля</b> (команда /img)\n"
        "  → опиши текстом, что нарисовать\n\n"
        "<b>━━━ Цены ━━━</b>\n"
        f"💎 Фото с твоим лицом в любой сцене — <b>{edit_q} кр</b>\n"
        f"🎭 Подставить лицо в чужое фото — <b>{face_swap} кр</b>\n"
        f"🎨 Картинка с нуля — <b>{img_gen} кр</b>"
        f"{promo_line}\n\n"
        "<b>━━━ Команды ━━━</b>\n"
        "/start · /gallery · /status · /sub · /promo · /ref · /faceswap · /img"
    )

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.repo.credits import BURN_PACKS, CREDIT_PACKS, DAILY_FREE_CREDITS, MODEL_COSTS
from app.keyboards.main import (
    PE_AU_GOLD, PE_BACK, PE_CARD, PE_FIRE, PE_LIGHTNING, PE_ROCKET,
)

# Маппинг fallback-эмодзи пака → premium-аналог.
# Если в pack['emoji'] стоит что-то не из карты — кнопка просто без premium-иконки.
_PACK_PE = {
    '🔥': PE_FIRE,
    '⚡': PE_LIGHTNING,
    '🚀': PE_ROCKET,
    '💎': PE_AU_GOLD,
}


def shop_kb() -> InlineKeyboardMarkup:
    """Магазин кредитов: срочные паки (×2-3 кр за ту же цену, сгорают) +
    вечные паки (дороже на кредит, без срока). Тексты максимально короткие —
    кнопки под фото-меню режутся в ширине.
    24.05: убраны длинные tag'и из текста (СТАРТЕР/ПОПУЛЯРНЫЙ ×3 выгоднее)
    — они теперь живут в описании пака, не в кнопке."""
    rows: list[list[InlineKeyboardButton]] = []
    # ── СРОЧНЫЕ паки (раньше «BURN», акцент на выгоде) ──
    for pack in BURN_PACKS:
        days = pack.get('expires_days') or 7
        label = f"{pack['credits']:,} кр / {pack['rub']}₽ · {days} дн"
        rows.append([InlineKeyboardButton.model_construct(
            text=label,
            callback_data=f"buy_pack:{pack['id']}",
            icon_custom_emoji_id=PE_FIRE, style='success',
        )])
    # ── ВЕЧНЫЕ паки ──
    for pack in CREDIT_PACKS:
        is_best = pack['credits'] == 6000
        marker = '🏆 ' if is_best else ''
        label = f"{marker}{pack['credits']:,} кр / {pack['rub']}₽ · вечно"
        pe_id = _PACK_PE.get(pack['emoji'])
        if pe_id:
            rows.append([InlineKeyboardButton.model_construct(
                text=label,
                callback_data=f"buy_pack:{pack['id']}",
                icon_custom_emoji_id=pe_id,
                style='primary' if is_best else None,
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=f"{pack['emoji']} {label}",
                callback_data=f"buy_pack:{pack['id']}",
                style='primary' if is_best else None,
            )])
    rows.append([InlineKeyboardButton.model_construct(
        text='Подписка — обычно выгоднее',
        callback_data='subscription', icon_custom_emoji_id=PE_AU_GOLD,
    )])
    rows.append([InlineKeyboardButton.model_construct(
        text='В главное меню', callback_data='main_menu',
        icon_custom_emoji_id=PE_BACK, style='danger',
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_pack_kb(pack_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton.model_construct(text='Оплатить картой', callback_data=f'pay_rub:{pack_id}', icon_custom_emoji_id=PE_CARD, style='success')],
        [InlineKeyboardButton.model_construct(text='Выбрать другой пакет', callback_data='shop', icon_custom_emoji_id=PE_BACK, style='danger')],
    ])


def credits_info_text(credits: int) -> str:
    return (
        f'💳 <b>Магазин кредитов</b>\n\n'
        f'Твой баланс: <b>{credits} кр</b>\n'
        '━━━━━━━━━━━━━━━━━━━━\n'
        '<b>Что можно сделать в Нейре:</b>\n'
        f'💎 Фото с твоим лицом в любой сцене — <b>{MODEL_COSTS["image_edit"]} кр</b>\n'
        f'   <i>(Балет, старинное авто, Париж, неон, кино-нуар…)</i>\n'
        f'🎭 Подставить лицо в чужое фото — <b>{MODEL_COSTS["face_swap"]} кр</b>\n'
        f'🎨 Нарисовать картинку с нуля — <b>{MODEL_COSTS["image"]} кр</b>\n'
        '━━━━━━━━━━━━━━━━━━━━\n'
        '<b>🔥 Срочные паки</b> — в 2-3 раза БОЛЬШЕ кредитов за ту же цену.\n'
        'Срок 7-30 дней. Бери если планируешь использовать сразу.\n\n'
        '<b>♾ Вечные паки</b> — без срока, лежат годами. Слегка дороже на кредит, '
        'зато спокойно — не торопишься.\n'
        '━━━━━━━━━━━━━━━━━━━━\n'
        f'✓ <b>Без водяного знака</b> на фото 30 дней после оплаты\n'
        f'✓ <b>Без автосписаний</b> — платишь один раз\n'
        f'✓ Каждый день <b>+{DAILY_FREE_CREDITS} кр бесплатно</b> сверху\n\n'
        '<b>Выбери пакет:</b>'
    )

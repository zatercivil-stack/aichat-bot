from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.repo.credits import BURN_PACKS, CREDIT_PACKS, DAILY_FREE_CREDITS, MODEL_COSTS
from app.keyboards.main import (
    PE_AU_GOLD,
    PE_BACK,
    PE_CARD,
    PE_FIRE,
    PE_LIGHTNING,
    PE_ROCKET,
)

# Маппинг fallback-эмодзи пака → premium-аналог.
# Если в pack['emoji'] стоит что-то не из карты — кнопка просто без premium-иконки.
_PACK_PE = {
    "🔥": PE_FIRE,
    "⚡": PE_LIGHTNING,
    "🚀": PE_ROCKET,
    "💎": PE_AU_GOLD,
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
        days = pack.get("expires_days") or 7
        photos = pack["credits"] // 200
        label = f"{photos} фото — {pack['rub']}₽ · {days} дн"
        rows.append(
            [
                InlineKeyboardButton.model_construct(
                    text=label,
                    callback_data=f"buy_pack:{pack['id']}",
                    icon_custom_emoji_id=PE_FIRE,
                    style="success",
                )
            ]
        )
    # ── ВЕЧНЫЕ паки ──
    for pack in CREDIT_PACKS:
        is_best = pack["credits"] == 6000
        marker = "🏆 " if is_best else ""
        photos = pack["credits"] // 200
        label = f"{marker}{photos} фото — {pack['rub']}₽ · вечно"
        pe_id = _PACK_PE.get(pack["emoji"])
        if pe_id:
            rows.append(
                [
                    InlineKeyboardButton.model_construct(
                        text=label,
                        callback_data=f"buy_pack:{pack['id']}",
                        icon_custom_emoji_id=pe_id,
                        style="primary" if is_best else None,
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{pack['emoji']} {label}",
                        callback_data=f"buy_pack:{pack['id']}",
                        style="primary" if is_best else None,
                    )
                ]
            )
    rows.append(
        [
            InlineKeyboardButton.model_construct(
                text="Подписка — обычно выгоднее",
                callback_data="subscription",
                icon_custom_emoji_id=PE_AU_GOLD,
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton.model_construct(
                text="В главное меню",
                callback_data="main_menu",
                icon_custom_emoji_id=PE_BACK,
                style="danger",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_pack_kb(pack_id: str, card_discount: int = 0) -> InlineKeyboardMarkup:
    # 3 способа оплаты ПРЯМО на карточке (без промежуточного экрана). Один
    # премиум-эмодзи на кнопку (без дублей). Stars-цена показывается.
    # Реф-кэшбэк применяется ТОЛЬКО к карте → итоговую цену показываем на самой
    # кнопке карты, чтобы Stars/Crypto не вводили в заблуждение «общей» скидкой.
    from app.services.payments_alt import product_info

    _info = product_info(pack_id)
    _stars = _info["stars"] if _info else None
    _base = int(_info["rub"]) if _info else 0
    _card_text = "Карта/СБП/СберПэй/Т-Пэй"
    if card_discount > 0 and _base:
        _card_text = f"Картой · {_base - int(card_discount)}₽ (−{int(card_discount)}₽ скидка)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text=_card_text,
                    callback_data=f"pay_rub:{pack_id}",
                    icon_custom_emoji_id=PE_CARD,
                    style="success",
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text=f"⭐ Stars{f' · {_stars}' if _stars else ''}",
                    callback_data=f"payst:{pack_id}",
                    style="primary",
                ),
                InlineKeyboardButton.model_construct(
                    text="💎 Cryptobot", callback_data=f"paycr:{pack_id}", style="primary"
                ),
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Выбрать другой пакет",
                    callback_data="shop",
                    icon_custom_emoji_id=PE_BACK,
                    style="danger",
                )
            ],
        ]
    )


def credits_info_text(credits: int) -> str:
    return (
        f"💳 <b>Магазин кредитов</b>\n\n"
        f"Твой баланс: <b>{credits} кр</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Что можно сделать в Нейре:</b>\n"
        f"💎 Фото с твоим лицом в любой сцене — <b>{MODEL_COSTS['image_edit']} кр</b>\n"
        f"   <i>(Балет, старинное авто, Париж, неон, кино-нуар…)</i>\n"
        f"🎭 Подставить лицо в чужое фото — <b>{MODEL_COSTS['face_swap']} кр</b>\n"
        f"🎨 Нарисовать картинку с нуля — <b>{MODEL_COSTS['image']} кр</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔥 99₽</b> — выгодный вход (кредиты на 7 дней).\n"
        "<b>♾ Вечные паки</b> — без срока, платишь один раз.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✓ <b>Без водяного знака</b> на фото 30 дней после оплаты\n"
        f"✓ <b>Без автосписаний</b> — платишь один раз\n"
        f"✓ Каждый день <b>+{DAILY_FREE_CREDITS} кр бесплатно</b> сверху\n\n"
        "<b>Выбери пакет:</b>"
    )

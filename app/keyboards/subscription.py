from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.main import PE_BACK, PE_CARD, PE_FIRE, PE_GIFT, PE_LIGHTNING, PE_ROCKET


def plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text="Оформить СТАРТ — 149₽/мес",
                    callback_data="choose_plan:starter",
                    icon_custom_emoji_id=PE_LIGHTNING,
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Оформить ПРО — 399₽/мес",
                    callback_data="choose_plan:pro",
                    icon_custom_emoji_id=PE_ROCKET,
                    style="success",
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Оформить МАКСИМУМ — 890₽/мес",
                    callback_data="choose_plan:max",
                    icon_custom_emoji_id=PE_FIRE,
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Не нужна подписка — купить разово",
                    callback_data="shop",
                    icon_custom_emoji_id=PE_CARD,
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="В главное меню",
                    callback_data="main_menu",
                    icon_custom_emoji_id=PE_BACK,
                    style="danger",
                )
            ],
        ]
    )


def payment_method_kb(plan: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton.model_construct(
                    text="Купить кредиты разово",
                    callback_data="shop",
                    icon_custom_emoji_id=PE_CARD,
                    style="success",
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="У меня есть промокод",
                    callback_data="enter_promo",
                    icon_custom_emoji_id=PE_GIFT,
                )
            ],
            [
                InlineKeyboardButton.model_construct(
                    text="Назад",
                    callback_data="subscription",
                    icon_custom_emoji_id=PE_BACK,
                    style="danger",
                )
            ],
        ]
    )

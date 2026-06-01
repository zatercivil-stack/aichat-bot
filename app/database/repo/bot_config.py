from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.database.models import BotConfig
from app.constants import WELCOME_TEXT, HELP_TEXT

# Default config values — synced with credits.py
DEFAULT_CONFIG: dict[str, tuple[str, str]] = {
    'model_cost_free':  ('1',  'DeepSeek V3.2 cost per message (credits)'),
    'model_cost_basic': ('3',  'GPT-5 nano cost per message (credits)'),
    'model_cost_mid':   ('8',  'GPT-5 mini cost per message (credits)'),
    'model_cost_pro':   ('15', 'Claude Sonnet 4.6 cost per message (credits)'),
    'model_cost_ultra': ('60', 'Claude Opus 4.7 cost per message (credits)'),
    'model_cost_image': ('60', 'Image generation cost (credits)'),
    'model_cost_voice': ('2',  'Voice transcription cost (credits)'),
    'model_cost_vision':('10', 'Photo analysis cost (credits)'),
    'daily_free_credits': ('15', 'Daily credit bonus for free plan users'),
    'referral_bonus':     ('150', 'Credits awarded per referred user'),
    # Подарок новичкам без рефера и промо. 23.05 поднят 300→500: при 300 кр
    # = 1.5 quality-сцены, и 80% юзеров уходили без единой генерации (страх
    # «потрачу всё на одну»). 500 = 2.5 топ-сцены = больше пространства для
    # экспериментов, ниже воспринимаемый риск первой попытки.
    'welcome_bonus_credits': ('500', 'Credits granted to brand-new users on first /start (no referrer, no promo)'),
    'test_broadcast_username': ('grelok', 'Username (without @) for "test user" audience in broadcasts'),
    # Запуск-цена на фото-фичи: до 19 мая 2026 включительно цена снижена на discount кр
    'image_promo_until': ('2026-05-19', 'Last day of launch-price promo (ISO date, empty=disabled)'),
    'image_promo_discount': ('100', 'Discount in кр applied to image_edit and face_swap during promo'),
    # YooKassa автоплатежи. Требует отдельной активации в кабинете YooKassa
    # (запрос менеджеру про «автоматические платежи»). Без этого save_payment_method
    # возвращает 403 ForbiddenError. Поставь '1' когда YooKassa подключит recurring.
    'yookassa_recurring_enabled': ('0', 'Enable save_payment_method=true on subscription payments (1/0)'),
    # Credit packs (price + amount) — справочно; источник правды CREDIT_PACKS
    'pack_pack_2000_credits':  ('2000',  'Pack 2000 — credit count'),
    'pack_pack_2000_rub':      ('290',   'Pack 2000 — RUB price'),
    'pack_pack_6000_credits':  ('6000',  'Pack 6000 — credit count'),
    'pack_pack_6000_rub':      ('690',   'Pack 6000 — RUB price'),
    'pack_pack_20000_credits': ('20000', 'Pack 20000 — credit count'),
    'pack_pack_20000_rub':     ('1790',  'Pack 20000 — RUB price'),
    # Editable bot texts
    'text_welcome': (WELCOME_TEXT, 'Welcome message shown on /start (HTML)'),
    'text_help':    (HELP_TEXT,    'Help message shown on /help (HTML)'),
    # Social proof — короткая строка для повышения доверия в welcome.
    # Редактируется в /admin/texts. Пустая строка отключает её.
    'text_social_proof': (
        '🌟 <b>Уже пользуются 13 000+ человек</b> — присоединяйся',
        'Social proof line shown above welcome (HTML, empty to hide)',
    ),
}


async def get_all_config(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(select(BotConfig))
    return {row.key: row.value for row in rows.scalars().all()}


async def get_config_value(session: AsyncSession, key: str) -> str | None:
    row = await session.scalar(select(BotConfig).where(BotConfig.key == key))
    return row.value if row else None


async def set_config_value(session: AsyncSession, key: str, value: str) -> None:
    stmt = insert(BotConfig).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=['key'], set_={'value': value})
    await session.execute(stmt)


async def ensure_defaults(session: AsyncSession) -> None:
    """Insert default values only if key doesn't exist yet."""
    existing = await get_all_config(session)
    for key, (default_value, description) in DEFAULT_CONFIG.items():
        if key not in existing:
            stmt = insert(BotConfig).values(key=key, value=default_value, description=description)
            stmt = stmt.on_conflict_do_nothing()
            await session.execute(stmt)
    await session.commit()


async def get_text(session: AsyncSession, key: str, default: str = '') -> str:
    """Get an editable text from BotConfig, falling back to provided default."""
    val = await get_config_value(session, key)
    return val if val is not None else default

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, User

DAILY_FREE_CREDITS = 15       # без подписки на канал — позволяет 1 fast edit раз в 3 дня
DAILY_CHANNEL_CREDITS = 40  # при подписке на @conturD — 1 fast edit КАЖДЫЙ день, ощутимая разница

MODEL_COSTS: dict[str, int] = {
    'free':  1,   # DeepSeek V3.2
    'basic': 3,   # GPT-5 nano
    'mid':   8,   # GPT-5 mini
    'pro':   30,  # Claude Sonnet 4.6 (поднято 15→30 для здоровой маржи на МАКСИМУМ-плане)
    'ultra': 80,  # Claude Opus 4.7 (поднято 60→80 для здоровой маржи)
    'image': 60,             # генерация с нуля (FLUX.2 Klein)
    'image_edit_fast': 40,   # быстрая правка (gpt-5-image-mini, $0.005) — стиль/фон/цвет
    'image_edit': 200,       # качественная правка (nano-banana-2/edit @2K = $0.12, verified 2026-05-22) — лицо ~80%
    'image_edit_precise': 300,  # edit + auto face-swap: лицо 1в1 (cost ~$0.15). Для «сцен».
    'face_swap': 200,        # подставить лицо отдельным шагом (fal-ai/face-swap)
    'voice': 2,         # транскрипция voice (плюс к стоимости текстового ответа)
    'vision': 10,       # анализ входящей картинки
}

# ── ВЕЧНЫЕ паки (не сгорают, копятся годами) ──
# Юзеры с psychology «куплю один раз, использую когда понадобится».
# Маржа выше потому что нет burn-rate revenue.
# Витрина (01.06): всё в ФОТО (1 фото = 200 кр), 4 опции, один якорь 🏆.
# pack_500 и burn_7500 убраны (мёртвый/дубль). Цены прежние, 1890→1790.
CREDIT_PACKS = [
    {'id': 'pack_2000',  'credits': 2000,  'rub': 290,  'name': '10 фото (вечный)',   'emoji': '🔥', 'expires_days': None},
    {'id': 'pack_6000',  'credits': 6000,  'rub': 690,  'name': '30 фото (вечный)',   'emoji': '🚀', 'expires_days': None},
    {'id': 'pack_20000', 'credits': 20000, 'rub': 1790, 'name': '100 фото (вечный)',  'emoji': '💎', 'expires_days': None},
]

# ── BURN-пак (сгорит через N дней) — единственный, как tripwire за 99₽ ──
# Кредитов больше за ту же цену; если не использует за срок — сгорают.
# Расход тратится из burn-pool ПЕРВЫМ (см. deduct_credits).
BURN_PACKS = [
    {
        'id': 'burn_1500_7d', 'credits': 1500, 'rub': 99,
        'name': '7 фото на 7 дней', 'emoji': '🔥', 'expires_days': 7,
        'tag': 'СТАРТЕР — выгодный вход',
    },
]

# Объединённый список для get_pack_by_id и совместимости
ALL_PACKS = CREDIT_PACKS + BURN_PACKS

SUBSCRIPTION_PLANS = [
    {
        'id': 'starter', 'name': 'СТАРТ', 'emoji': '⚡',
        'rub': 149, 'credits_monthly': 1000, 'daily_bonus': 30, 'images_monthly': 10,
        'plan_enum': 'basic',
    },
    {
        'id': 'pro', 'name': 'ПРО', 'emoji': '🚀',
        'rub': 399, 'credits_monthly': 5000, 'daily_bonus': 50, 'images_monthly': 50,
        'plan_enum': 'pro',
    },
    {
        'id': 'max', 'name': 'МАКСИМУМ', 'emoji': '💎',
        'rub': 890, 'credits_monthly': 15000, 'daily_bonus': 100, 'images_monthly': 200,
        'plan_enum': 'max',
    },
]


def get_subscription_plan(plan_id: str) -> dict | None:
    return next((p for p in SUBSCRIPTION_PLANS if p['id'] == plan_id), None)


def _plan_daily_bonus(plan_val: str) -> int | None:
    """Возвращает daily_bonus подписочного плана из SUBSCRIPTION_PLANS, или None
    если plan_val — не подписка (free/unknown). Чтобы grant_daily не дрейфовал
    от хардкода при изменении тарифа в /admin/pricing будущем."""
    plan = next((p for p in SUBSCRIPTION_PLANS if p.get('plan_enum') == plan_val), None)
    return int(plan['daily_bonus']) if plan else None


async def grant_daily_if_needed(
    session: AsyncSession, user: User, is_channel_sub: bool = False
) -> int:
    """Атомарно начисляет ежедневный бонус (idempotent в сутки по МСК).

    Защита от race: SELECT FOR UPDATE блокирует строку юзера до commit,
    параллельный вызов ждёт и видит уже выставленный daily_credits_granted_at.

    Время: МСК (utils.today_msk) — юзеры в России, иначе бонус «съезжает»
    на 3 часа ночи. Размер: подписка → daily_bonus из SUBSCRIPTION_PLANS;
    иначе DAILY_CHANNEL_CREDITS / DAILY_FREE_CREDITS.

    Флаг ставится ПОСЛЕ user.credits += bonus — если падает между этим,
    в БД ничего не записано (rollback), и следующая попытка переначислит."""
    from app.utils import today_msk, now_msk
    today = today_msk()

    # Берём row-lock на user — параллельный grant_daily на той же строке ждёт.
    # populate_existing=True заставляет SQLAlchemy реально сходить в БД
    # (а не вернуть кэшированный из identity map) — иначе FOR UPDATE не работает,
    # когда caller уже держит user в session (типичный случай /start).
    locked = await session.scalar(
        select(User).where(User.id == user.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None or locked.daily_credits_granted_at == today:
        return 0

    sub = await session.scalar(
        select(Subscription).where(Subscription.user_id == locked.id)
    )
    bonus: int | None = None
    if sub and sub.is_active and sub.expires_at:
        sub_expires = sub.expires_at
        if sub_expires.tzinfo is None:
            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
        if sub_expires > now_msk().astimezone(timezone.utc):
            plan_val = sub.plan.value if hasattr(sub.plan, 'value') else str(sub.plan)
            bonus = _plan_daily_bonus(plan_val)
    if bonus is None:
        bonus = DAILY_CHANNEL_CREDITS if is_channel_sub else DAILY_FREE_CREDITS

    # _clean_expired_burn перед добавлением — если burn сгорел, обнулим
    # до начисления (юзер не должен видеть «вчерашний» сгоревший пул в total).
    _clean_expired_burn(locked)
    locked.credits = (locked.credits or 0) + bonus
    # Флаг — последним: при ошибке выше rollback не закроет день, юзер
    # переначислится при следующем заходе.
    locked.daily_credits_granted_at = today
    # Синхронизируем in-memory user, переданный caller'ом (чтобы UI ниже видел
    # новый баланс без второго fetch).
    user.credits = locked.credits
    user.daily_credits_granted_at = locked.daily_credits_granted_at
    user.burn_credits = locked.burn_credits
    user.burn_expires_at = locked.burn_expires_at
    await session.flush()
    return bonus


# Награды за серию заходов подряд. Чистые кредиты (не burn), начисляются один
# раз при достижении дня. Растут — чтобы было жалко прервать серию (loss aversion).
STREAK_MILESTONES: dict[int, int] = {
    3: 20,
    7: 50,
    14: 75,
    30: 120,
    60: 150,
    100: 200,
}


def next_streak_milestone(day: int) -> tuple[int, int] | None:
    """(день следующей награды, размер в кредитах) или None если все позади."""
    for d in sorted(STREAK_MILESTONES):
        if d > day:
            return d, STREAK_MILESTONES[d]
    return None


async def update_streak(session: AsyncSession, user: User) -> tuple[int, int]:
    """Атомарно отмечает заход в серию (idempotent в сутки по МСК).
    Возвращает (текущая серия, milestone-бонус или 0).

    Защита от race: SELECT FOR UPDATE на User row → параллельный update_streak
    ждёт и видит уже выставленный streak_last_date.

    Время: МСК. Иначе streak ломается в ночные 3 часа (UTC ≠ МСК)."""
    from app.utils import today_msk, yesterday_msk
    today = today_msk()
    yesterday = yesterday_msk()

    # populate_existing=True — реальный поход в БД с lock (см. grant_daily_if_needed).
    locked = await session.scalar(
        select(User).where(User.id == user.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return user.streak_days or 0, 0
    if locked.streak_last_date == today:
        # синхронизируем in-memory user — caller возьмёт правильные значения
        user.streak_days = locked.streak_days
        user.streak_last_date = locked.streak_last_date
        return locked.streak_days, 0

    if locked.streak_last_date == yesterday:
        locked.streak_days = (locked.streak_days or 0) + 1
    else:
        locked.streak_days = 1
    locked.streak_last_date = today

    bonus = STREAK_MILESTONES.get(locked.streak_days, 0)
    if bonus:
        # Через add_credits — единый путь, чтобы корректно проводился
        # _clean_expired_burn перед мутацией.
        await add_credits(session, locked, bonus)

    # Синхронизируем in-memory user (caller использует его для UI/celebrate).
    user.streak_days = locked.streak_days
    user.streak_last_date = locked.streak_last_date
    user.credits = locked.credits
    user.burn_credits = locked.burn_credits
    user.burn_expires_at = locked.burn_expires_at
    await session.flush()
    return locked.streak_days, bonus


def _clean_expired_burn(user: User) -> None:
    """Если burn_expires_at в прошлом — обнуляем burn_credits. Вызываем перед
    любым чтением balance и перед deduct."""
    if user.burn_expires_at:
        exp = user.burn_expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            user.burn_credits = 0
            user.burn_expires_at = None
            user.burn_reminded_at = None


def total_balance(user: User) -> int:
    """Сумма burn (если не сгорел) + обычных кредитов. Используется для UI."""
    _clean_expired_burn(user)
    return (user.burn_credits or 0) + (user.credits or 0)


async def deduct_credits(session: AsyncSession, user: User, amount: int) -> bool:
    """Списывает amount кр. Тратятся СНАЧАЛА burn-кредиты (если ещё не сгорели),
    потом обычные. Это критично — burn-кредиты ограничены по сроку,
    обычные лежат вечно. Юзеру выгодно тратить burn первыми, и мы это
    автоматизируем.

    24.05: добавлен SELECT FOR UPDATE — без него два параллельных запроса
    (быстрый дабл-тап, album из 2 фото) могли пройти оба, и юзер уходил
    в минус. populate_existing=True заставляет SQLAlchemy реально сходить
    в БД, иначе identity-cache отдаёт старый объект и lock игнорируется."""
    from sqlalchemy import select as _sel
    # Перечитываем юзера под row-lock; параллельный deduct ждёт commit.
    locked = await session.scalar(
        _sel(User).where(User.id == user.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return False
    _clean_expired_burn(locked)
    total = (locked.burn_credits or 0) + locked.credits
    if total < amount:
        # Синхронизируем поля у входного объекта на случай, что caller
        # потом обращается к user.credits — пусть видит актуальное.
        user.credits = locked.credits
        user.burn_credits = locked.burn_credits
        user.burn_expires_at = locked.burn_expires_at
        return False
    burn_avail = locked.burn_credits or 0
    burn_to_spend = min(burn_avail, amount)
    if burn_to_spend > 0:
        locked.burn_credits = burn_avail - burn_to_spend
    remaining = amount - burn_to_spend
    if remaining > 0:
        locked.credits -= remaining
    await session.flush()
    # Прокидываем новые значения в исходный объект.
    user.credits = locked.credits
    user.burn_credits = locked.burn_credits
    user.burn_expires_at = locked.burn_expires_at
    return True


async def add_credits(
    session: AsyncSession, user: User, amount: int,
    burn_expires_at: 'datetime | None' = None,
) -> None:
    """Добавляет amount кр. Если задан burn_expires_at — кладём в burn-pool с
    указанным сроком. Иначе — в обычные (без срока).

    Если у юзера уже есть активный burn-pool и приходит новый burn — суммируем
    кредиты и продлеваем срок до МАКСИМУМА из старого и нового (юзер не должен
    потерять кредиты из-за того что купил ещё пак)."""
    _clean_expired_burn(user)
    if burn_expires_at is not None:
        user.burn_credits = (user.burn_credits or 0) + amount
        existing = user.burn_expires_at
        if existing is not None and existing.tzinfo is None:
            existing = existing.replace(tzinfo=timezone.utc)
        if existing is None or burn_expires_at > existing:
            user.burn_expires_at = burn_expires_at
            user.burn_reminded_at = None  # сброс reminder-cooldown для нового срока
    else:
        user.credits += amount
    await session.flush()


async def grant_welcome(session: AsyncSession, user: User) -> int:
    """Начисляет приветственный бонус (сгораемый) — та же логика, что в cmd_start.
    Вызывать ТОЛЬКО для новичков (is_new_user) — идемпотентность обеспечивает
    вызывающий гейт (is_new_user=True ровно на первом апдейте). Возвращает сумму."""
    from datetime import datetime, timedelta, timezone

    from app.database.repo.bot_config import get_config_value
    raw = (await get_config_value(session, 'welcome_bonus_credits') or '300').strip()
    try:
        amount = max(0, int(raw))
    except ValueError:
        amount = 300
    if not amount:
        return 0
    burn_raw = (await get_config_value(session, 'welcome_burn_days') or '7').strip()
    try:
        burn_days = max(0, int(burn_raw))
    except ValueError:
        burn_days = 7
    burn_until = (datetime.now(timezone.utc) + timedelta(days=burn_days)
                  if burn_days > 0 else None)
    await add_credits(session, user, amount, burn_expires_at=burn_until)
    return amount


def get_pack_by_id(pack_id: str) -> dict | None:
    """Ищет в обоих списках — вечные и burn."""
    return next((p for p in ALL_PACKS if p['id'] == pack_id), None)

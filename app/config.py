from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str
    BOT_USERNAME: str = ''
    ADMIN_IDS: str = ''

    # Database
    POSTGRES_HOST: str = 'postgres'
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = 'aichat_bot'
    POSTGRES_USER: str = 'aichat_user'
    POSTGRES_PASSWORD: str

    # Redis
    REDIS_URL: str = 'redis://redis:6379/0'

    # OpenRouter
    OPENROUTER_API_KEY: str

    # fal.ai — Gemini Flash Image для identity-preserving edits и face swap
    # Если пусто — фолбэчимся на OpenRouter (gpt-5-image)
    FAL_AI_API_KEY: str = ''
    # Отдельный admin-scoped ключ для опроса РЕАЛЬНОГО баланса fal.ai
    # (GET /v1/account/billing). Создаётся в дашборде, отличается от
    # модельного FAL_AI_API_KEY. Если пусто — авто-синк баланса не работает,
    # используется только локальный baseline-tracker.
    FAL_BILLING_API_KEY: str = ''

    # Brave Search API — для актуальных фактов в постах канала.
    # Используется в channel_publisher: AI может попросить свежие данные,
    # чтобы не писать «по памяти» (где галлюцинации цифр и дат).
    BRAVE_SEARCH_API_KEY: str = ''

    # YooKassa
    YOOKASSA_SHOP_ID: str = ''
    YOOKASSA_SECRET_KEY: str = ''
    YOOKASSA_RETURN_URL: str = ''

    # Webhook
    WEBHOOK_BASE_URL: str = ''
    WEBHOOK_PORT: int = 8080

    # Stars prices
    STARS_BASIC_MONTHLY: int = 250
    STARS_PRO_MONTHLY: int = 600

    # Admin dashboard
    ADMIN_TOKEN: str = 'change_me_in_env'
    ADMIN_SECRET_KEY: str = 'admin_sessions_secret'

    # Misc
    TIMEZONE: str = 'Europe/Moscow'

    class Config:
        env_file = '.env'
        extra = 'ignore'

    @property
    def admin_ids_list(self) -> list[int]:
        if not self.ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(',') if x.strip()]

    @property
    def database_url(self) -> str:
        return (
            f'postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}'
            f'@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}'
        )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)

    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.YOOKASSA_SHOP_ID and self.YOOKASSA_SECRET_KEY)

    @property
    def yookassa_return_url(self) -> str:
        return self.YOOKASSA_RETURN_URL or f'https://t.me/{self.BOT_USERNAME}'

settings = Settings()

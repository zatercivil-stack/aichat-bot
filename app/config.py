from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str
    BOT_USERNAME: str = ""
    ADMIN_IDS: str = ""

    # Database
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "aichat_bot"
    POSTGRES_USER: str = "aichat_user"
    POSTGRES_PASSWORD: str

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # OpenRouter
    OPENROUTER_API_KEY: str

    # OpenAI (gpt-5.4-mini, бесплатные токены data-sharing) — ТОЛЬКО для
    # генерации текстов канала @conturD. Клиентских данных не касается.
    # Пусто → автогенератор постов отключён.
    OPENAI_API_KEY: str = ""

    # fal.ai — Gemini Flash Image для identity-preserving edits и face swap
    # Если пусто — фолбэчимся на OpenRouter (gpt-5-image)
    FAL_AI_API_KEY: str = ""
    # Отдельный admin-scoped ключ для опроса РЕАЛЬНОГО баланса fal.ai
    # (GET /v1/account/billing). Создаётся в дашборде, отличается от
    # модельного FAL_AI_API_KEY. Если пусто — авто-синк баланса не работает,
    # используется только локальный baseline-tracker.
    FAL_BILLING_API_KEY: str = ""

    # Brave Search API — для актуальных фактов в постах канала.
    # Используется в channel_publisher: AI может попросить свежие данные,
    # чтобы не писать «по памяти» (где галлюцинации цифр и дат).
    BRAVE_SEARCH_API_KEY: str = ""

    # YooKassa
    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""
    YOOKASSA_RETURN_URL: str = ""

    # Webhook
    WEBHOOK_BASE_URL: str = ""
    WEBHOOK_PORT: int = 8080

    # Stars prices
    STARS_BASIC_MONTHLY: int = 250
    STARS_PRO_MONTHLY: int = 600

    # CryptoBot (Crypto Pay API) — оплата криптой. Токен из @CryptoBot → My Apps.
    CRYPTOBOT_TOKEN: str = ""

    # Environment: 'development' disables strict secret checks for local runs.
    ENV: str = "production"

    # Admin dashboard — MUST be set via env; empty default disables admin access.
    ADMIN_TOKEN: str = ""
    ADMIN_SECRET_KEY: str = ""

    # Public base URL of the admin dashboard, used to build links in Telegram alerts.
    # Must be HTTPS in production. Empty => admin links are omitted.
    ADMIN_BASE_URL: str = ""

    # Misc
    TIMEZONE: str = "Europe/Moscow"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def admin_ids_list(self) -> list[int]:
        if not self.ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)

    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.YOOKASSA_SHOP_ID and self.YOOKASSA_SECRET_KEY)

    @property
    def yookassa_return_url(self) -> str:
        return self.YOOKASSA_RETURN_URL or f"https://t.me/{self.BOT_USERNAME}"


_WEAK_ADMIN_TOKENS = {"", "change_me_in_env", "admin", "admin_token", "token"}
_WEAK_ADMIN_SECRETS = {"", "admin_sessions_secret", "secret", "change_me"}


def _validate_settings(s: Settings) -> None:
    if s.ENV.lower() in ("development", "dev", "local", "test"):
        return
    if s.ADMIN_TOKEN in _WEAK_ADMIN_TOKENS or len(s.ADMIN_TOKEN) < 16:
        raise RuntimeError(
            "ADMIN_TOKEN is missing or too weak. Set a strong ADMIN_TOKEN in .env "
            '(generate with: python -c "import secrets; print(secrets.token_urlsafe(32))").'
        )
    if s.ADMIN_SECRET_KEY in _WEAK_ADMIN_SECRETS or len(s.ADMIN_SECRET_KEY) < 16:
        raise RuntimeError(
            "ADMIN_SECRET_KEY is missing or too weak. Set a strong ADMIN_SECRET_KEY in .env "
            '(generate with: python -c "import secrets; print(secrets.token_urlsafe(32))").'
        )
    if s.ADMIN_BASE_URL and not s.ADMIN_BASE_URL.startswith("https://"):
        raise RuntimeError("ADMIN_BASE_URL must use https:// in production.")


settings = Settings()
_validate_settings(settings)

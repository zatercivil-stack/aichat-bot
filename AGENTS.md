# AGENTS.md

Technical guide for contributors. No production secrets, IPs, or internal operational data.

## Architecture

Async Telegram bot (aiogram 3) + FastAPI admin dashboard + PostgreSQL + Redis.
Entry point: `main.py` (runs Alembic migrations, starts polling + webhook server).
Dispatcher/routers/middlewares: `app/bot.py`.

Layers:
- `app/handlers/` — Telegram update handlers (FSM).
- `app/services/` — business logic and external API integrations.
- `app/database/` — SQLAlchemy async models + repository modules.
- `app/webserver/` — FastAPI admin dashboard and YooKassa webhook.
- `app/middlewares/` — rate limit, user resolution, command reset.
- `migrations/` — Alembic migrations.

## Local run

1. Copy `.env.example` to `.env`, fill required values (`ENV=development` for local).
2. Start PostgreSQL + Redis: `docker compose up -d postgres redis`.
3. Install deps: `pip install -r requirements.txt -r requirements-dev.txt`.
4. Run: `python main.py` (migrations apply automatically).
5. Tests: `pytest` (see pytest.ini).

## Development rules

- Secrets only via environment variables; never commit `.env`.
- Admin secrets must be strong (>=16 chars); weak defaults are rejected in production.
- Financial operations (payments, credits, subscriptions) must be idempotent and use row locks.
- Do not suppress exceptions with bare `except: pass`.
- Use `secrets.compare_digest` for secret comparisons.
- Ruff for lint/format; run before commits.

## Testing

- `pytest` runs unit tests; integration tests need PostgreSQL + Redis.
- Never use production data or real API keys in tests.

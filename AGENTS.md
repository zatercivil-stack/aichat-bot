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

1. Copy `.env.example` to `.env` and fill real service credentials.
2. Full app: `docker compose up --build -d` (migrations apply automatically).
3. Host Python 3.12: use `compose.dev.yml` and loopback connection settings; see `docs/DEVELOPMENT.md`.
4. Tests: `docker compose -f compose.test.yml run --build --rm test`.

## Development rules

- Secrets only via environment variables; never commit `.env`.
- Admin secrets must be strong (>=16 chars); weak defaults are rejected in production.
- Financial operations (payments, credits, subscriptions) must be idempotent and use row locks.
- Do not suppress exceptions with bare `except: pass`.
- Use `secrets.compare_digest` for secret comparisons.
- Ruff for lint/format; run before commits.

## Testing

- Tests require explicit `ENV=test` and `POSTGRES_DB` ending in `_test`.
- Use the standalone `compose.test.yml`; never run tests in the production bot container.
- Never use production data or real API keys in tests.

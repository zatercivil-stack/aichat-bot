# Architecture

Async Telegram bot built on aiogram 3 with a FastAPI sidecar (admin dashboard + YooKassa webhook) in one process.

## Components

- `main.py`: runs Alembic migrations, creates Redis/bot/dispatcher, starts polling and webhook server task.
- `app/bot.py`: dispatcher, routers, middlewares.
- `app/handlers/`: Telegram update handlers with FSM.
- `app/services/`: business logic and external API clients.
- `app/database/`: SQLAlchemy async models and repository modules (one per entity).
- `app/webserver/`: FastAPI admin dashboard and payment webhook.
- `app/middlewares/`: rate limiting, user resolution, command reset.

## Data flow (payment)

1. User initiates payment -> `create_payment` (PENDING).
2. YooKassa webhook or polling checker -> `verify_yookassa_payment` (server-side API check).
3. `process_succeeded_payment` acquires row locks on payment and user, idempotency check, adds credits, activates subscription, marks SUCCEEDED, commits.
4. Idempotent: repeated webhook returns True without re-applying.

## Credits

`deduct_credits` uses SELECT FOR UPDATE and spends burn credits first. `add_credits` also reloads the user under SELECT FOR UPDATE and supports expiring burn pools.

Both operations reject negative amounts. Locks are held until the caller commits or rolls back. See `tests/test_credits_payments.py` for concurrent and duplicate-payment regressions.

## Operational boundaries

See [case study](CASE_STUDY.md#ограничения-и-дальнейшее-развитие) for single-process scheduling, post-commit delivery, AI quality and monitoring limitations. Migration 026 fills missing promo automation fields and the channel publishing table. It preserves existing objects; its downgrade intentionally retains these objects to avoid removing pre-existing data.

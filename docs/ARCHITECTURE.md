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
3. `process_succeeded_payment` acquires row lock on payment, idempotency check, adds credits, activates subscription, marks SUCCEEDED, commits.
4. Idempotent: repeated webhook returns True without re-applying.

## Credits

`deduct_credits` uses SELECT FOR UPDATE and spends burn credits first. `add_credits` supports expiring burn pools.

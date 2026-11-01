# Разработка и проверка

## Тесты без рабочих ключей

Требуются Docker и Docker Compose v2+. Из корня репозитория:

```bash
docker compose -f compose.test.yml run --build --rm test
# После завершения удалить только тестовые контейнеры и сеть:
docker compose -f compose.test.yml down
```

`compose.test.yml` — самостоятельная конфигурация с отдельным именем проекта, PostgreSQL 16, Redis 7 и Python 3.12. Данные PostgreSQL находятся в tmpfs, порты и рабочие volumes не подключены. Миграции применяются до pytest. Telegram, AI и платёжные ключи фиктивные; внешние вызовы в проверяемых сценариях заменены.

Тесты требуют явно заданных `ENV=test` и `POSTGRES_DB` с суффиксом `_test`. Не запускайте их в контейнере рабочего бота: тесты меняют конфигурацию админки и создают пользователей и платежи.

```bash
docker compose -f compose.test.yml run --rm test ruff check .
docker compose -f compose.test.yml run --rm test ruff format --check .
```

CI выполняет те же проверки с применением миграций на пустой БД; отдельная job собирает production-образ.

## Python на хосте

Требуется Python 3.12. Скопируйте `.env.example` в `.env`, заполните значения, затем:

```bash
docker compose -f docker-compose.yml -f compose.dev.yml up -d postgres redis
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
POSTGRES_HOST=127.0.0.1 REDIS_URL=redis://127.0.0.1:6379/0 python main.py
```

Override `compose.dev.yml` публикует PostgreSQL и Redis только на loopback. Если порты заняты другими сервисами, используйте полный контейнерный запуск из README или измените локальное сопоставление портов. Реальный Telegram-токен нужен для polling; `ENV=development` ослабляет только проверку секретов, но не имитирует внешние API.

При запуске Python на хосте `main.py` использует `WEBHOOK_PORT` (по умолчанию 8080); публикация Docker на 8181 в этом случае не применяется. Для входа в админку нужен локальный HTTPS reverse proxy, поскольку cookie имеет флаг Secure.

## Форматирование и миграции

```bash
ruff check .
ruff format .
alembic upgrade head
```

Миграции также применяются при старте приложения. Для ручного запуска с хоста задайте `POSTGRES_HOST=127.0.0.1`. В SQLAlchemy используйте `.is_(False)` и `.is_(None)` для SQL-условий, а не Python `not` или `is None` над атрибутами моделей.

## Демонстрация интерфейса без инфраструктуры

```bash
python3.12 -m pip install jinja2 fastapi
python3.12 scripts/render_demo.py
```

Откройте `docs/demo/finance.html` в браузере. Генератор использует настоящий шаблон финансовой панели и фиксированные синтетические данные. Он не импортирует конфигурацию приложения, не читает `.env`, не подключается к БД или API. Действия и переходы отключены. Стили и библиотека графиков загружаются с CDN, поэтому для полного оформления нужен интернет.

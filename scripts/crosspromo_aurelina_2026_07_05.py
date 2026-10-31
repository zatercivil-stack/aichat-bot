"""Тестовая рассылка по аудитории Нейры с промо Аурелины (Таро+Астро бот).

Механика: карточный креатив «выбери карту» (как в кампании Аурелины «Таро с
Алисой» — карта 2 «тайна» взяла 85% выборов). Кнопки ведут ГЛУБОКОЙ ссылкой в
@aurelina_bot с ?start=src_neyra_c<N>[m] — Аурелина сама парсит номер карты
(app/handlers/start.py::parse_start_param) и показывает персональный hook,
дальше воронка целиком её собственная. Суффикс m = читатель мужчина (хуки
карт в Аурелине меняют род/местоимение партнёра — деплоено 05.07.2026, см.
_CARD_HOOKS_MALE/_PICK_REVEAL_MALE). Со стороны Нейры цена рассылки = 0₽
(свой трафик, без fal-генераций).

Аудитория: реальные (не забаненные, не тест-id, не владелец) юзеры Нейры,
ПРОШЕДШИЕ онбординг (gender IS NOT NULL) — сознательно НЕ трогаем zero-gen
когорту (122 чел., НЕ указали пол), она уже даёт high block-rate на любых
пушах. Ссылка различается по gender: female/any -> src_neyra_cN, male ->
src_neyra_cNm. На 05.07.2026 сегмент ~32 чел. (33 минус владелец 5343526043).

Запуск из контейнера:
  python -m scripts.crosspromo_aurelina_2026_07_05 --dry-run   # превью админу
  python -m scripts.crosspromo_aurelina_2026_07_05 --send      # реальная отправка
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.config import settings
from app.database.base import AsyncSessionFactory
from app.database.models import Broadcast
from app.database.repo.events import log_event

logger = structlog.get_logger(__name__)

SOURCE = 'neyra_crosspromo'
PROMO_CODE = 'AURELINA_CROSSPROMO_2026_07_05'
EXCLUDE_TEST_IDS = (1, 2, 3, 6)          # тестовые users.id
EXCLUDE_TELEGRAM_IDS = (5343526043,)     # владелец/др. telegram_id, исключить явно
AURELINA_DEEPLINK = 'https://t.me/aurelina_bot?start=src_neyra_c{card}{suffix}'
HERO_IMG = 'data/img/aurelina_cards.jpg'

TEXT = (
    '🔮 <b>Одна карта знает то, что ты давно хочешь услышать.</b>\n\n'
    'Есть человек, который не выходит из головы. Три карты, три ответа, '
    'но твой ответ только один.\n\n'
    '👉 <b>Жми на карту, которая тянет сильнее всего — она уже знает.</b>'
)


def _build_kb(male_reader: bool) -> InlineKeyboardMarkup:
    suffix = 'm' if male_reader else ''
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='1️⃣', url=AURELINA_DEEPLINK.format(card=1, suffix=suffix)),
        InlineKeyboardButton(text='2️⃣', url=AURELINA_DEEPLINK.format(card=2, suffix=suffix)),
        InlineKeyboardButton(text='3️⃣', url=AURELINA_DEEPLINK.format(card=3, suffix=suffix)),
    ]])


async def _fetch_recipients() -> list[tuple[int, int, bool]]:
    """(user_id, telegram_id, male_reader) — реальные онбордившиеся юзеры Нейры."""
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(sa_text(f"""
            SELECT id, telegram_id, gender
            FROM users
            WHERE is_banned = false
              AND id NOT IN ({','.join(str(i) for i in EXCLUDE_TEST_IDS)})
              AND telegram_id NOT IN ({','.join(str(i) for i in EXCLUDE_TELEGRAM_IDS)})
              AND gender IS NOT NULL
            ORDER BY id
        """))).all()
    return [(uid, tg_id, gender == 'male') for uid, tg_id, gender in rows]


async def _send_one(bot: Bot, tg_id: int, male_reader: bool) -> str:
    """'sent' | 'blocked' | 'error'."""
    kb = _build_kb(male_reader)
    try:
        await bot.send_photo(tg_id, photo=FSInputFile(HERO_IMG), caption=TEXT,
                             parse_mode='HTML', reply_markup=kb)
        return 'sent'
    except Exception as exc:
        err = str(exc).lower()
        if any(s in err for s in ('forbidden', 'blocked', 'deactivated')):
            return 'blocked'
        logger.warning('send failed', tg_id=tg_id, error=str(exc)[:200])
        return 'error'


async def _create_broadcast_row(total: int) -> int:
    async with AsyncSessionFactory() as session:
        b = Broadcast(source=SOURCE, promo_code=PROMO_CODE,
                      total_recipients=total,
                      started_at=datetime.now(UTC), status='running')
        session.add(b)
        await session.commit()
        return b.id


async def _finalize(broadcast_id: int, sent: int, blocked: int, errors: int) -> None:
    async with AsyncSessionFactory() as session:
        b = await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
        if not b:
            return
        b.sent_count, b.blocked_count, b.error_count = sent, blocked, errors
        b.completed_at = datetime.now(UTC)
        if b.started_at:
            started = b.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            b.duration_seconds = int((b.completed_at - started).total_seconds())
        b.status = 'completed'
        await session.commit()


async def run(dry_run: bool) -> None:
    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        recipients = await _fetch_recipients()
        total = len(recipients)
        n_male = sum(1 for _, _, m in recipients if m)

        print('\n=== AURELINA CROSSPROMO PLAN ===')
        print(f'Mode:       {"DRY-RUN" if dry_run else "REAL SEND"}')
        print(f'Recipients: {total} (male-linked: {n_male}, female/any-linked: {total - n_male})')
        print(f'Hero img:   {HERO_IMG}')
        print('=================================\n')

        if dry_run:
            admin_ids = settings.admin_ids_list
            print(f'Sending preview to admins: {admin_ids} (обе версии: female-link + male-link)')
            for aid in admin_ids:
                await bot.send_message(aid, '<i>📋 DRY-RUN — версия female/any (кнопки без суффикса m):</i>',
                                       parse_mode='HTML')
                await asyncio.sleep(0.2)
                status = await _send_one(bot, aid, male_reader=False)
                print(f'  admin {aid} (female-link): {status}')
                await asyncio.sleep(0.4)
                await bot.send_message(aid, '<i>📋 DRY-RUN — версия male (кнопки с суффиксом m):</i>',
                                       parse_mode='HTML')
                await asyncio.sleep(0.2)
                status = await _send_one(bot, aid, male_reader=True)
                print(f'  admin {aid} (male-link): {status}')
                await asyncio.sleep(0.3)
            print('✓ Dry-run complete. Проверь Telegram (кнопки ведут в @aurelina_bot).')
            return

        broadcast_id = await _create_broadcast_row(total)
        print(f'Started broadcast #{broadcast_id}')
        sent = blocked = errors = 0
        for uid, tg_id, male_reader in recipients:
            status = await _send_one(bot, tg_id, male_reader)
            if status == 'sent':
                sent += 1
                try:
                    await log_event(user_id=uid, event_type='crosspromo_sent',
                                    model_key=None, credits_spent=0,
                                    source=SOURCE, code=PROMO_CODE)
                except Exception as exc:
                    logger.warning('event log failed', error=str(exc)[:200])
            elif status == 'blocked':
                blocked += 1
            else:
                errors += 1
            await asyncio.sleep(0.06)

        await _finalize(broadcast_id, sent, blocked, errors)
        print('\n=== DONE ===')
        print(f'Sent:    {sent}')
        print(f'Blocked: {blocked}')
        print(f'Errors:  {errors}')
        print('============\n')
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='превью админам')
    g.add_argument('--send', action='store_true', help='реальная отправка по сегменту')
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == '__main__':
    main()

"""Трекинг трафика @conturD по invite-ссылкам — два update-типа в паре.

Почему два хендлера:
  • chat_member  → всегда срабатывает на любом join/leave, но для ПУБЛИЧНЫХ
    каналов TG системно НЕ кладёт invite_link в апдейт (проверено сырым
    дампом 25.05): любой джойн через t.me/+hash приходит как 'органика'.
    Так что chat_member один не даёт атрибуции.
  • chat_join_request → срабатывает только для invite-ссылок созданных с
    флагом creates_join_request=True. Зато ВСЕГДА содержит invite_link.
    Бот авто-аппрувит мгновенно (UX-задержка sub-second).

Координация через Redis:
  • on_join_request: SETEX jr_seen:{uid} 60 = '1'  (флаг "я обработал этот join")
  • on_chat_member 'join': если флаг есть → пропустить (joi_request уже
    записал в БД с правильной атрибуцией). Иначе пишем как 'organic'.
Левы пишем всегда из chat_member (через join_request не проходят).
"""
from __future__ import annotations

import json

from aiogram import Bot, Router
from aiogram.types import ChatJoinRequest, ChatMemberUpdated
from redis.asyncio import Redis
import structlog

from app.database.base import AsyncSessionFactory
from app.database.models import ChannelTraffic

logger = structlog.get_logger(__name__)

router = Router(name='channel_traffic')

# Numeric id @conturD — получен через bot.get_chat. По id фильтр строже, чем
# по @username (username можно переименовать).
CHANNEL_ID = -1003304830567

_INSIDE = {'member', 'restricted', 'administrator', 'creator'}
_OUTSIDE = {'left', 'kicked'}

_JR_SEEN_TTL = 60  # сек: окно, в котором ждём chat_member после join_request


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot, redis: Redis) -> None:
    """Авто-аппрув + лог с правильной атрибуцией invite-ссылки."""
    if req.chat.id != CHANNEL_ID:
        return
    inv = req.invite_link
    user = req.from_user

    # 1. Лог в БД С АТРИБУЦИЕЙ — это главная цель всего пайплайна
    try:
        async with AsyncSessionFactory() as session:
            session.add(ChannelTraffic(
                user_tg_id=user.id,
                event='join',
                invite_link_url=inv.invite_link if inv else None,
                invite_link_name=(inv.name if inv else None),
                via_chat_folder=False,
                old_status='left',
                new_status='member',
            ))
            await session.commit()
    except Exception as exc:
        logger.error('join_request_log_failed',
                     user=user.id, error=str(exc)[:200])

    # 2. Флаг в Redis: chat_member-хендлер увидит и не запишет дубль
    try:
        await redis.setex(f'jr_seen:{user.id}', _JR_SEEN_TTL, '1')
    except Exception as exc:
        logger.warning('jr_redis_flag_failed', user=user.id, error=str(exc)[:120])

    # 3. Мгновенный аппрув — UX-задержка sub-second
    try:
        await bot.approve_chat_join_request(chat_id=CHANNEL_ID, user_id=user.id)
        logger.info('join_request_approved',
                    user=user.id, username=user.username,
                    link=(inv.invite_link if inv else None),
                    name=(inv.name if inv else None))
    except Exception as exc:
        logger.error('join_request_approve_failed',
                     user=user.id, error=str(exc)[:200])


@router.chat_member()
async def on_chat_member(update: ChatMemberUpdated, redis: Redis) -> None:
    if update.chat.id != CHANNEL_ID:
        return

    old = update.old_chat_member.status if update.old_chat_member else None
    new = update.new_chat_member.status if update.new_chat_member else None

    if old in _OUTSIDE and new in _INSIDE:
        kind = 'join'
    elif old in _INSIDE and new in _OUTSIDE:
        kind = 'leave'
    else:
        return  # промоут/демоут — игнор

    user = update.new_chat_member.user

    # Дедуп для joi'нов через join_request flow: если флаг есть — join_request
    # уже записал атрибутированную запись, второй раз не пишем.
    if kind == 'join':
        try:
            seen = await redis.get(f'jr_seen:{user.id}')
        except Exception:
            seen = None
        if seen:
            try:
                await redis.delete(f'jr_seen:{user.id}')
            except Exception:
                pass
            logger.info('chat_member_dedup_skip',
                        user=user.id, reason='join_request_flow')
            return

    inv = update.invite_link
    try:
        async with AsyncSessionFactory() as session:
            session.add(ChannelTraffic(
                user_tg_id=user.id,
                event=kind,
                invite_link_url=inv.invite_link if inv else None,
                invite_link_name=(inv.name if inv else None),
                via_chat_folder=bool(update.via_chat_folder_invite_link),
                old_status=old,
                new_status=new,
            ))
            await session.commit()
    except Exception as exc:
        logger.error('channel_traffic_insert_failed',
                     kind=kind, user=user.id, error=str(exc)[:200])
        return

    logger.info('channel_traffic_logged',
                kind=kind, user=user.id, username=user.username,
                link=(inv.invite_link if inv else None),
                name=(inv.name if inv else None),
                via_folder=bool(update.via_chat_folder_invite_link))
